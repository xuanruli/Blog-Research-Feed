"""Tests for brf.fetchers.firecrawl_index.FirecrawlIndexFetcher."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from brf.fetchers.base import SourceFetcher
from brf.fetchers.firecrawl_index import (
    FirecrawlIndexFetcher,
    _parse_index_date,
    _slug_to_title,
    _url_slug,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(**overrides) -> dict:
    base = {
        "name": "Anthropic News",
        "url": "https://www.anthropic.com/news",
        "article_url_regex": r"https?://www\.anthropic\.com/news/[a-z0-9-]+",
        "date_format": None,
        "date_group": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ABC compliance + init
# ---------------------------------------------------------------------------

def test_subclass_of_source_fetcher():
    assert issubclass(FirecrawlIndexFetcher, SourceFetcher)
    assert FirecrawlIndexFetcher.source_type == "firecrawl_index"


def test_init_filters_disabled_entries():
    f = FirecrawlIndexFetcher([
        _entry(),
        _entry(name="Disabled", enabled=False),
    ])
    assert len(f._entries) == 1
    assert f._entries[0]["name"] == "Anthropic News"


def test_init_skips_bad_regex(capsys):
    f = FirecrawlIndexFetcher([
        _entry(),
        _entry(name="Bad", article_url_regex=r"["),
    ])
    captured = capsys.readouterr()
    assert "bad regex" in captured.err
    assert len(f._entries) == 1


def test_init_skips_missing_regex(capsys):
    f = FirecrawlIndexFetcher([
        _entry(name="NoRegex", article_url_regex=None),
    ])
    captured = capsys.readouterr()
    assert "missing article_url_regex" in captured.err
    assert f._entries == []


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------

def test_parse_index_date_strptime_ok():
    dt = _parse_index_date("2026-05-20", "%Y-%m-%d")
    assert dt == datetime(2026, 5, 20, tzinfo=timezone.utc)


def test_parse_index_date_strptime_bad():
    assert _parse_index_date("not-a-date", "%Y-%m-%d") is None


def test_parse_index_date_yymm_ok():
    # HF papers: arXiv id "2401.12345" → 2024-01-31 (last day of month;
    # see fix comment in _parse_index_date — yymm is month precision,
    # stamp at month-end so a same-month `since` cutoff still admits
    # the paper).
    dt = _parse_index_date("2401.12345", "yymm")
    assert dt == datetime(2024, 1, 31, tzinfo=timezone.utc)


def test_parse_index_date_yymm_admits_same_month_since():
    """Regression: yymm month must not filter out same-month items."""
    # Paper "2605.16403" (May 2026) with since=2026-05-17 → must pass.
    dt = _parse_index_date("2605.16403", "yymm")
    assert dt is not None
    assert dt >= datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_parse_index_date_yymm_bad_month():
    assert _parse_index_date("2413.12345", "yymm") is None


def test_parse_index_date_yymm_too_short():
    assert _parse_index_date("240", "yymm") is None


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def test_url_slug_basic():
    assert _url_slug("https://example.com/news/some-post/") == "some-post"
    assert _url_slug("https://example.com/news/some-post?ref=x") == "some-post"


def test_slug_to_title_fallback():
    assert _slug_to_title("https://example.com/news/cool-new-model") == "Cool New Model"


# ---------------------------------------------------------------------------
# fetch() — empty / firecrawl-unavailable
# ---------------------------------------------------------------------------

def test_fetch_empty_entries():
    f = FirecrawlIndexFetcher([])
    assert list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc))) == []


def test_fetch_firecrawl_import_fails(monkeypatch, capsys):
    """If firecrawl-py can't be imported, log + return [] (don't crash)."""
    import builtins

    f = FirecrawlIndexFetcher([_entry()])
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "brf.firecrawl_client":
            raise ImportError("firecrawl missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert items == []
    assert "firecrawl unavailable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# fetch() — happy path with mocked firecrawl
# ---------------------------------------------------------------------------

ANTHROPIC_MD = """
# News

[Announcing Claude 4.7](https://www.anthropic.com/news/claude-4-7)
[Some research post](https://www.anthropic.com/research/something-else)
[Privacy](https://www.anthropic.com/privacy)
[Other model release](https://www.anthropic.com/news/new-model)
[Announcing Claude 4.7](https://www.anthropic.com/news/claude-4-7)   <!-- duplicate -->
"""


def test_fetch_happy_path_extracts_articles(monkeypatch):
    fake_scrape = lambda url: {"markdown": ANTHROPIC_MD, "metadata": {}}
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape", fake_scrape, raising=False
    )
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    # 2 unique news URLs (research filtered out by regex, duplicate dropped)
    assert len(items) == 2
    urls = {it.url for it in items}
    assert urls == {
        "https://www.anthropic.com/news/claude-4-7",
        "https://www.anthropic.com/news/new-model",
    }
    for it in items:
        assert it.source_type == "firecrawl_index"
        assert it.source == "Anthropic News"
        assert it.has_full is False
        assert it.needs_firecrawl is True
        assert it.extra == {"index_url": "https://www.anthropic.com/news"}
        # No date in the URL pattern → published stays None
        assert it.published is None


HF_MD = """
[Foo](https://huggingface.co/papers/2401.12345)
[Bar](https://huggingface.co/papers/2611.00001)
[Baz](https://huggingface.co/papers/2401.99999)
"""


def test_fetch_yymm_date_filter(monkeypatch):
    """`since` cutoff drops papers published before the threshold."""
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": HF_MD, "metadata": {}},
        raising=False,
    )
    entry = _entry(
        name="HF Daily Papers",
        url="https://huggingface.co/papers",
        article_url_regex=r"https?://huggingface\.co/papers/(\d{4})\.\d{4,5}",
        date_format="yymm",
        date_group=1,
    )
    f = FirecrawlIndexFetcher([entry])
    # Cutoff = 2026-01-01 → drop the 2401.* paper, keep the 2611.* one.
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    urls = {it.url for it in items}
    assert urls == {"https://huggingface.co/papers/2611.00001"}
    # yymm is month-precision, stamped at the last day of that month.
    assert items[0].published == "2026-11-30T00:00:00+00:00"


def test_fetch_scrape_error_isolated(monkeypatch, capsys):
    """One bad index page must not abort the whole run."""
    def scrape(url):
        if "anthropic" in url:
            raise RuntimeError("boom")
        return {"markdown": "[OpenAI thing](https://openai.com/index/cool-post)", "metadata": {}}

    monkeypatch.setattr("brf.firecrawl_client.scrape", scrape, raising=False)
    f = FirecrawlIndexFetcher([
        _entry(),
        _entry(
            name="OpenAI News",
            url="https://openai.com/news",
            article_url_regex=r"https?://openai\.com/(?:index/)?[a-z0-9-]+",
        ),
    ])
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    assert items[0].source == "OpenAI News"
    assert "scrape failed" in capsys.readouterr().err


def test_fetch_slug_blocklist(monkeypatch):
    """Blocklisted slugs are skipped even when they match the regex."""
    md = "[Privacy](https://www.anthropic.com/news/privacy-policy)\n[Real](https://www.anthropic.com/news/claude-4-7)"
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": md, "metadata": {}},
        raising=False,
    )
    entry = _entry(slug_blocklist=["privacy-policy"])
    f = FirecrawlIndexFetcher([entry])
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert [it.url for it in items] == ["https://www.anthropic.com/news/claude-4-7"]


def test_fetch_max_items_cap(monkeypatch):
    """Per-index emission caps at MAX_ITEMS_PER_INDEX (25)."""
    md = "\n".join(
        f"[post {i}](https://www.anthropic.com/news/post-{i})" for i in range(40)
    )
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": md, "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 25


def test_fetch_title_fallback_when_link_text_is_url(monkeypatch):
    """If the markdown link text is just the URL, derive a title from the slug."""
    md = "[https://www.anthropic.com/news/claude-4-7](https://www.anthropic.com/news/claude-4-7)"
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": md, "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert items[0].title == "Claude 4 7"


def test_fetch_strips_url_fragment_before_dedupe(monkeypatch):
    """Same article via /papers/X and /papers/X#community must dedupe."""
    md = (
        "[Paper](https://huggingface.co/papers/2401.12345)\n"
        "[1](https://huggingface.co/papers/2401.12345#community)"
    )
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": md, "metadata": {}},
        raising=False,
    )
    entry = _entry(
        name="HF Daily Papers",
        url="https://huggingface.co/papers",
        article_url_regex=r"https?://huggingface\.co/papers/\d{4}\.\d{4,5}",
    )
    f = FirecrawlIndexFetcher([entry])
    items = list(f.fetch(datetime(2024, 1, 1, tzinfo=timezone.utc)))
    assert len(items) == 1
    assert items[0].url == "https://huggingface.co/papers/2401.12345"


def test_fetch_empty_markdown(monkeypatch):
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": "", "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    assert list(f.fetch(datetime(2026, 1, 1, tzinfo=timezone.utc))) == []


def test_fetch_naive_since_normalized(monkeypatch):
    """A naive ``since`` datetime is coerced to UTC, no crash."""
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": ANTHROPIC_MD, "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(datetime(2026, 1, 1)))  # naive
    assert len(items) == 2


# ---------------------------------------------------------------------------
# fetch_full()
# ---------------------------------------------------------------------------

def test_fetch_full_returns_markdown_bytes(monkeypatch):
    from brf.feed_item import FeedItem, make_id

    item = FeedItem(
        id=make_id("firecrawl_index", "https://www.anthropic.com/news/claude-4-7"),
        source_type="firecrawl_index",
        source="Anthropic News",
        title="Claude 4.7",
        url="https://www.anthropic.com/news/claude-4-7",
        published=None,
        summary="",
        has_full=False,
        needs_firecrawl=True,
        extra={"index_url": "https://www.anthropic.com/news"},
    )
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": "# Claude 4.7\n\nbody", "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    result = f.fetch_full(item)
    assert result == b"# Claude 4.7\n\nbody"


def test_fetch_full_empty_returns_none(monkeypatch):
    from brf.feed_item import FeedItem

    item = FeedItem(
        id="x", source_type="firecrawl_index", source="x", title="x",
        url="https://example.com/x", published=None, summary="",
        has_full=False, needs_firecrawl=True, extra={},
    )
    monkeypatch.setattr(
        "brf.firecrawl_client.scrape",
        lambda url: {"markdown": "", "metadata": {}},
        raising=False,
    )
    f = FirecrawlIndexFetcher([_entry()])
    assert f.fetch_full(item) is None


def test_fetch_full_scrape_error_returns_none(monkeypatch, capsys):
    from brf.feed_item import FeedItem

    item = FeedItem(
        id="x", source_type="firecrawl_index", source="x", title="x",
        url="https://example.com/x", published=None, summary="",
        has_full=False, needs_firecrawl=True, extra={},
    )

    def fail(url):
        raise RuntimeError("nope")

    monkeypatch.setattr("brf.firecrawl_client.scrape", fail, raising=False)
    f = FirecrawlIndexFetcher([_entry()])
    assert f.fetch_full(item) is None
    assert "fetch_full failed" in capsys.readouterr().err
