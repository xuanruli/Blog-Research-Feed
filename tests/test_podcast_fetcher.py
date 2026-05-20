"""Tests for brf.fetchers.podcast.PodcastFetcher (Phase 3c)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from brf.feed_item import make_id
from brf.fetchers.base import SourceFetcher
from brf.fetchers.podcast import PodcastFetcher, _parse_duration


# ---------------------------------------------------------------------------
# Synthetic feed XML helpers
# ---------------------------------------------------------------------------

def _rss(items_xml: str, title: str = "Test Podcast") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>{title}</title>{items_xml}</channel></rss>"
    ).encode()


def _ep(
    title: str,
    link: str,
    description: str = "show notes",
    audio_url: str | None = "https://cdn.example.com/ep.mp3",
    duration: str | None = None,
    pubdate: str = "Mon, 19 May 2026 12:00:00 +0000",
    use_media: bool = False,
) -> str:
    parts = [
        "<item>",
        f"<title>{title}</title>",
        f"<link>{link}</link>",
        f"<description>{description}</description>",
        f"<pubDate>{pubdate}</pubDate>",
    ]
    if audio_url and not use_media:
        parts.append(
            f'<enclosure url="{audio_url}" type="audio/mpeg" length="12345"/>'
        )
    if audio_url and use_media:
        parts.append(f'<media:content url="{audio_url}" type="audio/mpeg"/>')
    if duration is not None:
        parts.append(f"<itunes:duration>{duration}</itunes:duration>")
    parts.append("</item>")
    return "".join(parts)


def _mock_response(content: bytes, status: int = 200) -> httpx.Response:
    req = httpx.Request("GET", "https://example.com/podcast.rss")
    return httpx.Response(status, content=content, request=req)


# ---------------------------------------------------------------------------
# Class shape
# ---------------------------------------------------------------------------

def test_subclass_of_source_fetcher():
    assert issubclass(PodcastFetcher, SourceFetcher)
    assert PodcastFetcher.source_type == "podcast"


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------

def test_parse_duration_seconds():
    assert _parse_duration("3600") == 3600
    assert _parse_duration("3782") == 3782


def test_parse_duration_mmss():
    assert _parse_duration("43:21") == 2601


def test_parse_duration_hhmmss():
    assert _parse_duration("1:02:15") == 3735


def test_parse_duration_unparseable():
    assert _parse_duration("") is None
    assert _parse_duration("nope") is None
    assert _parse_duration(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------

def test_three_episodes_yield_three_feed_items():
    xml = _rss(
        _ep("Ep 1", "https://pod.example/1", "first show notes content")
        + _ep("Ep 2", "https://pod.example/2", "second show notes content")
        + _ep("Ep 3", "https://pod.example/3", "third show notes content")
    )
    feeds = [{"name": "Demo Pod", "url": "https://example.com/podcast.rss"}]
    f = PodcastFetcher(feeds=feeds)

    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert len(results) == 3
    for item in results:
        assert item.source_type == "podcast"
        assert item.source == "Demo Pod"
        assert "show notes" in item.summary
        assert item.has_full is False
        assert item.needs_firecrawl is False
        assert item.extra["feed_url"] == "https://example.com/podcast.rss"


def test_enclosure_audio_url_populated():
    xml = _rss(_ep("Ep", "https://pod.example/1",
                   audio_url="https://cdn.example.com/ep1.mp3"))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(results) == 1
    assert results[0].extra["audio_url"] == "https://cdn.example.com/ep1.mp3"


def test_media_content_fallback():
    xml = _rss(_ep("Ep", "https://pod.example/1",
                   audio_url="https://cdn.example.com/ep1.mp3",
                   use_media=True))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert results[0].extra["audio_url"] == "https://cdn.example.com/ep1.mp3"


def test_episode_without_enclosure_still_emitted():
    xml = _rss(_ep("Ep", "https://pod.example/1", audio_url=None))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert len(results) == 1
    assert results[0].extra["audio_url"] is None
    assert results[0].url == "https://pod.example/1"


def test_itunes_duration_seconds():
    xml = _rss(_ep("Ep", "https://pod.example/1", duration="3600"))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert results[0].extra["duration_seconds"] == 3600


def test_itunes_duration_mmss():
    xml = _rss(_ep("Ep", "https://pod.example/1", duration="43:21"))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert results[0].extra["duration_seconds"] == 2601


def test_itunes_duration_unparseable_is_none():
    xml = _rss(_ep("Ep", "https://pod.example/1", duration="nope"))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert results[0].extra["duration_seconds"] is None


def test_make_id_deterministic_for_podcast():
    url = "https://pod.example/ep-42"
    assert make_id("podcast", url) == make_id("podcast", url)
    # Differs from other source_type for the same url
    assert make_id("podcast", url) != make_id("rss", url)
    xml = _rss(_ep("Ep", url))
    f = PodcastFetcher(feeds=[{"name": "P", "url": "https://x/rss"}])
    with patch("httpx.get", return_value=_mock_response(xml)):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert results[0].id == make_id("podcast", url)


def test_network_error_logged_does_not_crash(capsys):
    feeds = [
        {"name": "Broken", "url": "https://broken.example/rss"},
        {"name": "Good", "url": "https://good.example/rss"},
    ]
    good_xml = _rss(_ep("Hi", "https://pod.example/hi"))

    def fake_get(url, *args, **kwargs):
        if "broken" in url:
            raise httpx.ConnectError("boom")
        return _mock_response(good_xml)

    f = PodcastFetcher(feeds=feeds)
    with patch("httpx.get", side_effect=fake_get):
        results = list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    assert len(results) == 1
    err = capsys.readouterr().err
    assert "broken.example" in err


def test_disabled_feeds_filtered_defensively():
    """PodcastFetcher should iterate whatever it's given, but defensively
    honor `enabled=false` in case caller passes the raw list."""
    feeds = [
        {"name": "On",  "url": "https://on.example/rss"},
        {"name": "Off", "url": "https://off.example/rss", "enabled": False,
         "reason": "dead"},
    ]
    xml = _rss(_ep("Ep", "https://pod.example/1"))
    seen_urls: list[str] = []

    def fake_get(url, *args, **kwargs):
        seen_urls.append(url)
        return _mock_response(xml)

    f = PodcastFetcher(feeds=feeds)
    with patch("httpx.get", side_effect=fake_get):
        list(f.fetch(since=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert seen_urls == ["https://on.example/rss"]


# ---------------------------------------------------------------------------
# fetch_full()
# ---------------------------------------------------------------------------

def _item_with(audio_url):
    from brf.feed_item import FeedItem
    return FeedItem(
        id="abc1234567890def",
        source_type="podcast",
        source="P",
        title="Ep",
        url="https://pod.example/1",
        published="2026-05-19T12:00:00+00:00",
        summary="notes",
        has_full=False,
        needs_firecrawl=False,
        extra={"audio_url": audio_url, "duration_seconds": 60,
               "feed_url": "https://x/rss"},
    )


def test_fetch_full_returns_none_when_no_audio_url():
    f = PodcastFetcher(feeds=[])
    assert f.fetch_full(_item_with(None)) is None


def test_fetch_full_success(tmp_path):
    item = _item_with("https://cdn.example.com/ep.mp3")
    f = PodcastFetcher(feeds=[])

    def fake_download(url, dest):
        with open(dest, "wb") as fh:
            fh.write(b"fake mp3")
        return True, None, None

    def fake_transcribe(path, api_key):
        return "hello world transcript", "ok", None

    with patch("brf.podcast._download_audio", side_effect=fake_download), \
         patch("brf.podcast._transcribe_whisper", side_effect=fake_transcribe), \
         patch("brf.config.get_env", return_value="sk-test"):
        result = f.fetch_full(item)

    assert result == b"hello world transcript"


def test_fetch_full_download_failure_returns_none(capsys):
    item = _item_with("https://cdn.example.com/ep.mp3")
    f = PodcastFetcher(feeds=[])

    with patch("brf.podcast._download_audio",
               return_value=(False, "download_failed", "http 404")), \
         patch("brf.podcast._transcribe_whisper") as tx, \
         patch("brf.config.get_env", return_value="sk-test"):
        assert f.fetch_full(item) is None
        tx.assert_not_called()
    assert "download failed" in capsys.readouterr().err


def test_fetch_full_no_api_key_returns_none():
    item = _item_with("https://cdn.example.com/ep.mp3")
    f = PodcastFetcher(feeds=[])
    with patch("brf.config.get_env", return_value=None):
        assert f.fetch_full(item) is None


def test_fetch_full_whisper_failure_returns_none():
    item = _item_with("https://cdn.example.com/ep.mp3")
    f = PodcastFetcher(feeds=[])

    def fake_download(url, dest):
        with open(dest, "wb") as fh:
            fh.write(b"x")
        return True, None, None

    with patch("brf.podcast._download_audio", side_effect=fake_download), \
         patch("brf.podcast._transcribe_whisper",
               return_value=(None, "transcription_failed", "http 500")), \
         patch("brf.config.get_env", return_value="sk-test"):
        assert f.fetch_full(item) is None
