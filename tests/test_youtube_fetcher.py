"""Tests for brf.fetchers.youtube.YouTubeFetcher (Phase 3b)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from brf.feed_item import make_id
from brf.fetchers.base import SourceFetcher
from brf.fetchers.youtube import YouTubeFetcher


# ---------------------------------------------------------------------------
# Synthetic Atom feed helpers (YouTube channel RSS is Atom 1.0)
# ---------------------------------------------------------------------------

def _atom_entry(
    video_id: str,
    title: str,
    description: str,
    published: str = "2026-05-19T12:00:00+00:00",
) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return (
        "<entry>"
        f"<title>{title}</title>"
        f'<link rel="alternate" href="{url}"/>'
        f"<published>{published}</published>"
        f"<summary>{description}</summary>"
        "</entry>"
    )


def _channel_feed(entries_xml: str, title: str = "Some Channel") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{title}</title>"
        f"{entries_xml}"
        "</feed>"
    ).encode()


def _mock_response(content: bytes, status: int = 200) -> httpx.Response:
    req = httpx.Request("GET", "https://www.youtube.com/feeds/videos.xml")
    return httpx.Response(status, content=content, request=req)


SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Subclass / contract
# ---------------------------------------------------------------------------

def test_is_subclass_of_source_fetcher():
    assert issubclass(YouTubeFetcher, SourceFetcher)
    assert YouTubeFetcher.source_type == "youtube"


# ---------------------------------------------------------------------------
# fetch() — basic normalization
# ---------------------------------------------------------------------------

def test_rich_descriptions_do_not_call_ytdlp():
    rich_desc = "A thoughtful video about transformers and attention mechanisms. " * 3
    entries = (
        _atom_entry("abc12345678", "Video One", rich_desc)
        + _atom_entry("def98765432", "Video Two", rich_desc)
    )
    xml = _channel_feed(entries)
    channels = [{"name": "Test Chan", "channel_id": "UC_xyz"}]
    f = YouTubeFetcher(channels=channels)

    with patch("httpx.get", return_value=_mock_response(xml)) as mock_get, \
         patch("brf.fetchers.youtube._ytdlp_metadata") as mock_meta:
        results = list(f.fetch(since=SINCE))

    assert mock_get.call_count == 1
    assert mock_meta.call_count == 0  # rich desc => no yt-dlp call

    assert len(results) == 2
    for item in results:
        assert item.source_type == "youtube"
        assert item.source == "Test Chan"
        assert "transformers" in item.summary
        assert item.has_full is False
        assert item.needs_firecrawl is False
        assert item.extra["channel_id"] == "UC_xyz"
        assert item.extra["duration_seconds"] is None
        assert item.url.startswith("https://www.youtube.com/watch?v=")


def test_empty_description_triggers_ytdlp_fallback():
    entries = _atom_entry("vid_empty01", "Empty Desc Video", "")
    xml = _channel_feed(entries)
    channels = [{"name": "C", "channel_id": "UC_empty"}]
    f = YouTubeFetcher(channels=channels)

    fake_meta = {
        "description": "Recovered description text from yt-dlp metadata. " * 2,
        "duration": 1234,
    }
    with patch("httpx.get", return_value=_mock_response(xml)), \
         patch("brf.fetchers.youtube._ytdlp_metadata", return_value=fake_meta) as mock_meta:
        results = list(f.fetch(since=SINCE))

    assert mock_meta.call_count == 1
    assert len(results) == 1
    item = results[0]
    assert "Recovered description" in item.summary
    assert item.extra["duration_seconds"] == 1234
    assert item.needs_firecrawl is False
    assert item.has_full is False


def test_empty_description_and_ytdlp_fails_emits_empty_summary():
    entries = _atom_entry("nv12345abcd", "No Help Available", "")
    xml = _channel_feed(entries)
    channels = [{"name": "C", "channel_id": "UC_nada"}]
    f = YouTubeFetcher(channels=channels)

    with patch("httpx.get", return_value=_mock_response(xml)), \
         patch("brf.fetchers.youtube._ytdlp_metadata", return_value=None):
        results = list(f.fetch(since=SINCE))

    assert len(results) == 1
    item = results[0]
    assert item.summary == ""
    assert item.title == "No Help Available"
    assert item.extra["duration_seconds"] is None
    # Still no firecrawl on YT (React shell — useless)
    assert item.needs_firecrawl is False
    assert item.has_full is False


def test_make_id_is_deterministic_youtube():
    url = "https://www.youtube.com/watch?v=DEADBEEF000"
    entries = _atom_entry("DEADBEEF000", "T", "x" * 200)
    xml = _channel_feed(entries)
    f = YouTubeFetcher(channels=[{"name": "C", "channel_id": "UC_a"}])

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=SINCE))

    assert results[0].id == make_id("youtube", url)


def test_multiple_channels_all_emit():
    desc = "long description body content " * 5
    xml_a = _channel_feed(_atom_entry("vidAAAAAAAA", "A1", desc), title="Chan A")
    xml_b = _channel_feed(_atom_entry("vidBBBBBBBB", "B1", desc), title="Chan B")

    channels = [
        {"name": "Chan A", "channel_id": "UC_A"},
        {"name": "Chan B", "channel_id": "UC_B"},
    ]
    f = YouTubeFetcher(channels=channels)

    def fake_get(url, **kw):
        if "UC_A" in url:
            return _mock_response(xml_a)
        return _mock_response(xml_b)

    with patch("httpx.get", side_effect=fake_get):
        results = list(f.fetch(since=SINCE))

    sources = sorted(r.source for r in results)
    assert sources == ["Chan A", "Chan B"]
    assert len(results) == 2
    # All items: no firecrawl, no pre-fetched full
    for r in results:
        assert r.needs_firecrawl is False
        assert r.has_full is False


def test_http_failure_does_not_sink_other_channels():
    desc = "long description body content " * 5
    xml_b = _channel_feed(_atom_entry("vidBBBBBBBB", "B1", desc), title="Chan B")

    channels = [
        {"name": "Broken", "channel_id": "UC_broken"},
        {"name": "Chan B", "channel_id": "UC_B"},
    ]
    f = YouTubeFetcher(channels=channels)

    def fake_get(url, **kw):
        if "UC_broken" in url:
            raise httpx.ConnectError("boom")
        return _mock_response(xml_b)

    with patch("httpx.get", side_effect=fake_get):
        results = list(f.fetch(since=SINCE))

    assert len(results) == 1
    assert results[0].source == "Chan B"


# ---------------------------------------------------------------------------
# fetch_full() — transcript wrapper
# ---------------------------------------------------------------------------

def _make_item(url: str = "https://www.youtube.com/watch?v=abc"):
    from brf.feed_item import FeedItem
    return FeedItem(
        id=make_id("youtube", url),
        source_type="youtube",
        source="C",
        title="t",
        url=url,
        published=None,
        summary="",
        has_full=False,
        needs_firecrawl=False,
        extra={"channel_id": "UC_x", "duration_seconds": None},
    )


def test_fetch_full_returns_bytes_when_transcript_present():
    f = YouTubeFetcher(channels=[])
    item = _make_item()
    fake = {
        "transcript": "hello transcript text",
        "transcript_source": "captions",
        "status": "ok",
    }
    with patch("brf.youtube.get_transcript", return_value=fake):
        out = f.fetch_full(item)
    assert out == b"hello transcript text"


def test_fetch_full_returns_none_when_transcript_none():
    f = YouTubeFetcher(channels=[])
    item = _make_item()
    fake = {"transcript": None, "transcript_source": None, "status": "error"}
    with patch("brf.youtube.get_transcript", return_value=fake):
        out = f.fetch_full(item)
    assert out is None


def test_fetch_full_swallows_exceptions():
    f = YouTubeFetcher(channels=[])
    item = _make_item()
    with patch("brf.youtube.get_transcript", side_effect=RuntimeError("boom")):
        out = f.fetch_full(item)
    assert out is None
