"""Tests for brf.fetchers.rss.RssFetcher (Phase 2)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from brf.feed_item import make_id
from brf.fetchers.rss import RssFetcher


# ---------------------------------------------------------------------------
# Synthetic feed XML helpers
# ---------------------------------------------------------------------------

def _rss(items_xml: str, title: str = "Test Feed") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>{title}</title>{items_xml}</channel></rss>"
    ).encode()


def _atom(entries_xml: str, title: str = "Atom Feed") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{title}</title>{entries_xml}</feed>"
    ).encode()


def _mock_response(content: bytes, status: int = 200) -> httpx.Response:
    req = httpx.Request("GET", "https://example.com/feed")
    return httpx.Response(status, content=content, request=req)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "feed"


# ---------------------------------------------------------------------------
# 3-branch normalize (RSS)
# ---------------------------------------------------------------------------

def test_full_branch_writes_html_and_sets_flags(output_dir: Path):
    content_html = "<p>Hello world. " + ("body content here. " * 30) + "</p>"
    items_xml = (
        "<item>"
        "<title>Full Article</title>"
        "<link>https://example.com/full</link>"
        "<description>short blurb</description>"
        f"<content:encoded><![CDATA[{content_html}]]></content:encoded>"
        "<pubDate>Mon, 19 May 2026 12:00:00 +0000</pubDate>"
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{"name": "Demo", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert len(results) == 1
    item = results[0]
    assert item.source_type == "rss"
    assert item.source == "Demo"
    assert item.url == "https://example.com/full"
    assert item.has_full is True
    assert item.needs_firecrawl is False
    assert "Hello world" in item.summary
    assert "<p>" not in item.summary  # HTML stripped

    # Full HTML written to disk under deterministic id
    expected_id = make_id("rss", "https://example.com/full")
    assert item.id == expected_id
    path = output_dir / "full" / f"{expected_id}.html"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == content_html


def test_summary_branch_no_full_no_firecrawl(output_dir: Path):
    desc = "This is a substantive description, well over eighty characters long indeed and then some."
    assert len(desc) >= 80
    items_xml = (
        "<item>"
        "<title>Summary-style entry</title>"
        "<link>https://example.com/sum</link>"
        f"<description>{desc}</description>"
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{"name": "Demo", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    item = results[0]
    assert item.has_full is False
    assert item.needs_firecrawl is False
    assert desc in item.summary

    # No full file written
    full_files = list((output_dir / "full").glob("*"))
    assert full_files == []


def test_title_only_branch_needs_firecrawl(output_dir: Path):
    items_xml = (
        "<item>"
        "<title>Just a title</title>"
        "<link>https://example.com/title</link>"
        "<description>tiny</description>"  # <80 chars
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{"name": "Demo", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    item = results[0]
    assert item.has_full is False
    assert item.needs_firecrawl is True
    assert item.summary == ""


def test_summary_only_flag_forces_needs_firecrawl(output_dir: Path):
    """A feed marked summary_only=True forces needs_firecrawl even with
    a substantive description."""
    desc = "This is a substantive description, well over eighty characters long indeed and then some."
    items_xml = (
        "<item>"
        "<title>HN-ish</title>"
        "<link>https://example.com/hn</link>"
        f"<description>{desc}</description>"
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{
        "name": "HN",
        "url": "https://example.com/feed",
        "summary_only": True,
    }]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    item = results[0]
    assert item.has_full is False
    assert item.needs_firecrawl is True
    assert desc in item.summary  # summary still populated


# ---------------------------------------------------------------------------
# Atom variant
# ---------------------------------------------------------------------------

def test_atom_full_branch(output_dir: Path):
    body = "<div><p>Atom body content " + ("blah " * 50) + "</p></div>"
    entries = (
        "<entry>"
        "<title>Atom Post</title>"
        '<link rel="alternate" href="https://example.com/atom/1"/>'
        "<summary>brief</summary>"
        f'<content type="html"><![CDATA[{body}]]></content>'
        "<published>2026-05-19T10:00:00Z</published>"
        "</entry>"
    )
    xml = _atom(entries)
    feeds = [{"name": "AtomSrc", "url": "https://example.com/atom"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    item = results[0]
    assert item.url == "https://example.com/atom/1"
    assert item.has_full is True
    assert item.needs_firecrawl is False
    assert "Atom body" in item.summary

    path = output_dir / "full" / f"{item.id}.html"
    assert path.exists()
    assert "Atom body" in path.read_text(encoding="utf-8")


def test_atom_summary_branch(output_dir: Path):
    summary_text = "An atom summary that is comfortably longer than eighty characters to qualify as substantive."
    entries = (
        "<entry>"
        "<title>Atom Summary</title>"
        '<link rel="alternate" href="https://example.com/atom/2"/>'
        f"<summary>{summary_text}</summary>"
        "<published>2026-05-19T10:00:00Z</published>"
        "</entry>"
    )
    xml = _atom(entries)
    feeds = [{"name": "AtomSrc", "url": "https://example.com/atom"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    item = results[0]
    assert item.has_full is False
    assert item.needs_firecrawl is False
    assert summary_text in item.summary


# ---------------------------------------------------------------------------
# Disabled feeds + id determinism
# ---------------------------------------------------------------------------

def test_disabled_feeds_silently_skipped(output_dir: Path):
    feeds = [
        {"name": "Dead", "url": "https://dead.example.com/feed", "enabled": False},
        {"name": "Also dead", "url": "https://also.example.com/feed", "enabled": False},
    ]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    called = []

    def boom(*a, **kw):
        called.append(a)
        raise AssertionError("disabled feed should not be fetched")

    with patch("httpx.get", side_effect=boom):
        results = list(f.fetch(since=None))

    assert results == []
    assert called == []


def test_make_id_matches_full_html_filename(output_dir: Path):
    """The on-disk full/<id>.html filename uses make_id("rss", url)."""
    content_html = "<p>" + ("body " * 50) + "</p>"
    url = "https://example.com/specific-post"
    items_xml = (
        "<item>"
        "<title>X</title>"
        f"<link>{url}</link>"
        f"<content:encoded><![CDATA[{content_html}]]></content:encoded>"
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{"name": "S", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=None))

    assert len(results) == 1
    expected = make_id("rss", url)
    assert results[0].id == expected
    assert (output_dir / "full" / f"{expected}.html").exists()


# ---------------------------------------------------------------------------
# Firecrawl-fallback constructor handling
# ---------------------------------------------------------------------------

def test_firecrawl_fallback_empty_dict_disables(output_dir: Path):
    """Passing firecrawl_fallback={} routes all feeds through the live lane."""
    feeds = [
        {"name": "x", "url": "https://www.jiqizhixin.com/rss"},  # default fallback URL
    ]
    f = RssFetcher(feeds=feeds, output_dir=output_dir, firecrawl_fallback={})
    assert len(f._live_feeds) == 1
    assert f._fallback_feeds == []


def test_firecrawl_fallback_default_routes_known_url(output_dir: Path):
    feeds = [
        {"name": "x", "url": "https://www.jiqizhixin.com/rss"},
        {"name": "ok", "url": "https://example.com/feed"},
    ]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)  # default fallback
    assert len(f._live_feeds) == 1
    assert len(f._fallback_feeds) == 1
    assert f._fallback_feeds[0][0]["name"] == "x"


# ---------------------------------------------------------------------------
# fetch_full
# ---------------------------------------------------------------------------

def test_fetch_full_returns_bytes(output_dir: Path):
    from brf.feed_item import FeedItem

    item = FeedItem(
        id=make_id("rss", "https://example.com/p"),
        source_type="rss",
        source="S",
        title="T",
        url="https://example.com/p",
        published=None,
        summary="",
        has_full=False,
        needs_firecrawl=True,
    )
    f = RssFetcher(feeds=[], output_dir=output_dir)

    with patch("brf.firecrawl_client.scrape", return_value={"markdown": "# Hi\n"}):
        result = f.fetch_full(item)

    assert result == b"# Hi\n"


def test_fetch_full_returns_none_on_failure(output_dir: Path):
    from brf.feed_item import FeedItem

    item = FeedItem(
        id="x",
        source_type="rss",
        source="S",
        title="T",
        url="https://example.com/p",
        published=None,
        summary="",
        has_full=False,
        needs_firecrawl=True,
    )
    f = RssFetcher(feeds=[], output_dir=output_dir)

    with patch("brf.firecrawl_client.scrape", side_effect=RuntimeError("boom")):
        assert f.fetch_full(item) is None


# ---------------------------------------------------------------------------
# since-filter
# ---------------------------------------------------------------------------

def test_since_filter_drops_old_items(output_dir: Path):
    items_xml = (
        "<item>"
        "<title>Old</title>"
        "<link>https://example.com/old</link>"
        "<description>" + ("desc " * 30) + "</description>"
        "<pubDate>Mon, 01 Jan 2020 00:00:00 +0000</pubDate>"
        "</item>"
        "<item>"
        "<title>New</title>"
        "<link>https://example.com/new</link>"
        "<description>" + ("desc " * 30) + "</description>"
        "<pubDate>Mon, 19 May 2026 12:00:00 +0000</pubDate>"
        "</item>"
    )
    xml = _rss(items_xml)
    feeds = [{"name": "Demo", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    urls = {r.url for r in results}
    assert urls == {"https://example.com/new"}


# ---------------------------------------------------------------------------
# Network error handling — fetch() does not raise
# ---------------------------------------------------------------------------

def test_network_error_does_not_raise(output_dir: Path):
    feeds = [{"name": "x", "url": "https://example.com/feed"}]
    f = RssFetcher(feeds=feeds, output_dir=output_dir)

    with patch("httpx.get", side_effect=httpx.ConnectError("nope")):
        results = list(f.fetch(since=None))

    assert results == []
