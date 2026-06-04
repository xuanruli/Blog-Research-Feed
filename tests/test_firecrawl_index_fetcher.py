"""Tests for brf.fetchers.firecrawl_index.FirecrawlIndexFetcher."""
from __future__ import annotations

from datetime import datetime, timezone

from brf.fetchers.base import SourceFetcher
from brf.fetchers.firecrawl_index import (
    FirecrawlIndexFetcher,
    _parse_index_date,
    _parse_iso_date,
    _slug_to_title,
    _url_slug,
)

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _resp(articles=None, markdown="") -> dict:
    """Shape returned by brf.clients.firecrawl.scrape_index."""
    return {"articles": articles or [], "markdown": markdown}


def _article(url, published="2026-05-20", title="A post"):
    return {"title": title, "url": url, "published": published}


def _patch_index(monkeypatch, fn):
    monkeypatch.setattr("brf.clients.firecrawl.scrape_index", fn, raising=False)


# ---------------------------------------------------------------------------
# ABC compliance + init
# ---------------------------------------------------------------------------

def test_subclass_of_source_fetcher():
    assert issubclass(FirecrawlIndexFetcher, SourceFetcher)
    assert FirecrawlIndexFetcher.source_type == "firecrawl_index"


def test_init_filters_disabled_entries():
    f = FirecrawlIndexFetcher([_entry(), _entry(name="Disabled", enabled=False)])
    assert len(f._entries) == 1
    assert f._entries[0]["name"] == "Anthropic News"


def test_init_skips_bad_regex(capsys):
    f = FirecrawlIndexFetcher([_entry(), _entry(name="Bad", article_url_regex=r"[")])
    assert "bad regex" in capsys.readouterr().err
    assert len(f._entries) == 1


def test_init_skips_missing_regex(capsys):
    f = FirecrawlIndexFetcher([_entry(name="NoRegex", article_url_regex=None)])
    assert "missing article_url_regex" in capsys.readouterr().err
    assert f._entries == []


# ---------------------------------------------------------------------------
# Date parsers
# ---------------------------------------------------------------------------

def test_parse_index_date_strptime_ok():
    assert _parse_index_date("2026-05-20", "%Y-%m-%d") == datetime(2026, 5, 20, tzinfo=timezone.utc)


def test_parse_index_date_strptime_bad():
    assert _parse_index_date("not-a-date", "%Y-%m-%d") is None


def test_parse_index_date_yymm_ok():
    assert _parse_index_date("2401.12345", "yymm") == datetime(2024, 1, 31, tzinfo=timezone.utc)


def test_parse_index_date_yymm_admits_same_month_since():
    dt = _parse_index_date("2605.16403", "yymm")
    assert dt is not None and dt >= datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_parse_index_date_yymm_bad_month():
    assert _parse_index_date("2413.12345", "yymm") is None


def test_parse_iso_date_variants():
    assert _parse_iso_date("2026-05-20") == datetime(2026, 5, 20, tzinfo=timezone.utc)
    assert _parse_iso_date("2026-05-20T14:00:00Z") == datetime(2026, 5, 20, 14, tzinfo=timezone.utc)
    assert _parse_iso_date("") is None
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("May 20, 2026") is None


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def test_url_slug_basic():
    assert _url_slug("https://example.com/news/some-post/") == "some-post"
    assert _url_slug("https://example.com/news/some-post?ref=x") == "some-post"


def test_slug_to_title_fallback():
    assert _slug_to_title("https://example.com/news/cool-new-model") == "Cool New Model"


# ---------------------------------------------------------------------------
# fetch() — empty / unavailable
# ---------------------------------------------------------------------------

def test_fetch_empty_entries():
    f = FirecrawlIndexFetcher([])
    assert list(f.fetch(SINCE)) == []


def test_fetch_firecrawl_import_fails(monkeypatch, capsys):
    import builtins

    f = FirecrawlIndexFetcher([_entry()])
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "brf.clients.firecrawl":
            raise ImportError("firecrawl missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert list(f.fetch(SINCE)) == []
    assert "firecrawl unavailable" in capsys.readouterr().err


def test_fetch_empty_response(monkeypatch):
    _patch_index(monkeypatch, lambda url: _resp())
    f = FirecrawlIndexFetcher([_entry()])
    assert list(f.fetch(SINCE)) == []


# ---------------------------------------------------------------------------
# fetch() — extracted dated articles
# ---------------------------------------------------------------------------

def test_fetch_uses_extracted_dates_and_filters_by_since(monkeypatch):
    articles = [
        _article("https://www.anthropic.com/news/claude-4-7", "2026-05-28"),
        _article("https://www.anthropic.com/research/x", "2026-05-28"),
        _article("https://www.anthropic.com/news/old-post", "2025-03-01"),
        _article("https://www.anthropic.com/news/claude-4-7", "2026-05-28"),
    ]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(SINCE))
    assert [it.url for it in items] == ["https://www.anthropic.com/news/claude-4-7"]
    assert items[0].published == "2026-05-28T00:00:00+00:00"
    assert items[0].source == "Anthropic News"
    assert items[0].needs_firecrawl is True
    assert items[0].extra == {"index_url": "https://www.anthropic.com/news"}


def test_fetch_drops_undated_items_under_since(monkeypatch):
    """The core fix: an item with no resolvable date is dropped, not re-surfaced."""
    articles = [
        _article("https://www.anthropic.com/news/dated", "2026-05-20"),
        _article("https://www.anthropic.com/news/undated", published=None),
    ]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(SINCE))
    assert [it.url for it in items] == ["https://www.anthropic.com/news/dated"]


def test_fetch_keeps_undated_when_no_since(monkeypatch):
    """With no cutoff, undated items pass through (published stays None)."""
    articles = [_article("https://www.anthropic.com/news/undated", published=None)]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(None))
    assert len(items) == 1
    assert items[0].published is None


def test_fetch_naive_since_normalized(monkeypatch):
    articles = [_article("https://www.anthropic.com/news/claude-4-7", "2026-05-20")]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(datetime(2026, 1, 1)))
    assert len(items) == 1


# ---------------------------------------------------------------------------
# fetch() — markdown fallback
# ---------------------------------------------------------------------------

ANTHROPIC_MD = """
[Announcing Claude 4.7](https://www.anthropic.com/news/claude-4-7)
[Some research post](https://www.anthropic.com/research/something-else)
[Other model release](https://www.anthropic.com/news/new-model)
"""


def test_fetch_markdown_fallback_no_since(monkeypatch):
    _patch_index(monkeypatch, lambda url: _resp(markdown=ANTHROPIC_MD))
    f = FirecrawlIndexFetcher([_entry()])
    items = list(f.fetch(None))
    assert {it.url for it in items} == {
        "https://www.anthropic.com/news/claude-4-7",
        "https://www.anthropic.com/news/new-model",
    }
    assert all(it.published is None for it in items)


def test_fetch_markdown_fallback_dropped_under_since(monkeypatch):
    """Markdown links are undated → all dropped when a since cutoff is set."""
    _patch_index(monkeypatch, lambda url: _resp(markdown=ANTHROPIC_MD))
    f = FirecrawlIndexFetcher([_entry()])
    assert list(f.fetch(SINCE)) == []


# ---------------------------------------------------------------------------
# fetch() — URL-encoded date fallback
# ---------------------------------------------------------------------------

def test_fetch_yymm_url_date_fallback(monkeypatch):
    """When extraction gives no date, fall back to a date parsed from the URL."""
    articles = [
        {"title": "Foo", "url": "https://huggingface.co/papers/2401.12345", "published": None},
        {"title": "Bar", "url": "https://huggingface.co/papers/2611.00001", "published": None},
    ]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    entry = _entry(
        name="HF Daily Papers",
        url="https://huggingface.co/papers",
        article_url_regex=r"https?://huggingface\.co/papers/(\d{4})\.\d{4,5}",
        date_format="yymm",
        date_group=1,
    )
    f = FirecrawlIndexFetcher([entry])
    items = list(f.fetch(SINCE))
    assert [it.url for it in items] == ["https://huggingface.co/papers/2611.00001"]
    assert items[0].published == "2026-11-30T00:00:00+00:00"


# ---------------------------------------------------------------------------
# fetch() — filtering details
# ---------------------------------------------------------------------------

def test_fetch_scrape_error_isolated(monkeypatch, capsys):
    def scrape_index(url):
        if "anthropic" in url:
            raise RuntimeError("boom")
        return _resp(articles=[_article("https://openai.com/index/cool-post", "2026-05-20")])

    _patch_index(monkeypatch, scrape_index)
    f = FirecrawlIndexFetcher([
        _entry(),
        _entry(name="OpenAI News", url="https://openai.com/news",
               article_url_regex=r"https?://openai\.com/(?:index/)?[a-z0-9-]+"),
    ])
    items = list(f.fetch(SINCE))
    assert [it.source for it in items] == ["OpenAI News"]
    assert "scrape failed" in capsys.readouterr().err


def test_fetch_slug_blocklist(monkeypatch):
    articles = [
        _article("https://www.anthropic.com/news/privacy-policy"),
        _article("https://www.anthropic.com/news/claude-4-7"),
    ]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry(slug_blocklist=["privacy-policy"])])
    items = list(f.fetch(SINCE))
    assert [it.url for it in items] == ["https://www.anthropic.com/news/claude-4-7"]


def test_fetch_max_items_cap(monkeypatch):
    articles = [_article(f"https://www.anthropic.com/news/post-{i}") for i in range(40)]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    assert len(list(f.fetch(SINCE))) == 25


def test_fetch_title_fallback_when_link_text_is_url(monkeypatch):
    articles = [_article("https://www.anthropic.com/news/claude-4-7",
                         title="https://www.anthropic.com/news/claude-4-7")]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    f = FirecrawlIndexFetcher([_entry()])
    assert list(f.fetch(SINCE))[0].title == "Claude 4 7"


def test_fetch_strips_url_fragment_before_dedupe(monkeypatch):
    articles = [
        _article("https://huggingface.co/papers/2401.12345", "2026-05-20", title="Paper"),
        _article("https://huggingface.co/papers/2401.12345#community", "2026-05-20", title="1"),
    ]
    _patch_index(monkeypatch, lambda url: _resp(articles=articles))
    entry = _entry(
        name="HF Daily Papers",
        url="https://huggingface.co/papers",
        article_url_regex=r"https?://huggingface\.co/papers/\d{4}\.\d{4,5}",
    )
    f = FirecrawlIndexFetcher([entry])
    items = list(f.fetch(SINCE))
    assert [it.url for it in items] == ["https://huggingface.co/papers/2401.12345"]


# ---------------------------------------------------------------------------
# fetch_full() — drill-down
# ---------------------------------------------------------------------------

def _item(url="https://www.anthropic.com/news/claude-4-7"):
    from brf.feed_item import FeedItem, make_id
    return FeedItem(
        id=make_id("firecrawl_index", url),
        source_type="firecrawl_index",
        source="Anthropic News",
        title="Claude 4.7",
        url=url,
        published=None,
        summary="",
        has_full=False,
        needs_firecrawl=True,
        extra={"index_url": "https://www.anthropic.com/news"},
    )


def test_fetch_full_returns_markdown_bytes(monkeypatch):
    monkeypatch.setattr(
        "brf.clients.firecrawl.scrape",
        lambda url: {"markdown": "# Claude 4.7\n\nbody", "metadata": {}},
        raising=False,
    )
    assert FirecrawlIndexFetcher([_entry()]).fetch_full(_item()) == b"# Claude 4.7\n\nbody"


def test_fetch_full_empty_returns_none(monkeypatch):
    monkeypatch.setattr(
        "brf.clients.firecrawl.scrape",
        lambda url: {"markdown": "", "metadata": {}},
        raising=False,
    )
    assert FirecrawlIndexFetcher([_entry()]).fetch_full(_item()) is None


def test_fetch_full_scrape_error_returns_none(monkeypatch, capsys):
    def fail(url):
        raise RuntimeError("nope")

    monkeypatch.setattr("brf.clients.firecrawl.scrape", fail, raising=False)
    assert FirecrawlIndexFetcher([_entry()]).fetch_full(_item()) is None
    assert "fetch_full failed" in capsys.readouterr().err
