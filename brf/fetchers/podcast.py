"""PodcastFetcher — Phase 3c of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4 (PodcastFetcher row). Responsible for:

* Concurrent (10 workers) httpx fetch of every enabled podcast RSS feed.
* Per-episode normalize -> ``FeedItem`` with show-notes summary and
  ``extra={audio_url, duration_seconds, feed_url}``.
* ``fetch_full`` drill-down: download the audio enclosure and transcribe
  via OpenAI Whisper. Returns ``None`` if no audio enclosure is available.
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from .base import SourceFetcher
from ._rss_parsing import parse_feed

DEFAULT_TIMEOUT_SECS = 15
DEFAULT_MAX_WORKERS = 10
SUMMARY_MAX_CHARS = 500

# Namespaces for itunes:duration / media:content extraction.
_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_MEDIA_NS = "http://search.yahoo.com/mrss/"


def _parse_duration(raw: str) -> Optional[int]:
    """Parse itunes:duration text into seconds.

    Supports ``"3782"`` (seconds), ``"43:21"`` (MM:SS), ``"1:02:15"``
    (HH:MM:SS). Returns ``None`` if unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            m, s = nums
            return m * 60 + s
        if len(nums) == 3:
            h, m, s = nums
            return h * 3600 + m * 60 + s
        return None
    try:
        # plain integer seconds (may be "3782" or "3782.5")
        return int(float(raw))
    except ValueError:
        return None


def _extract_enclosures(xml_bytes: bytes) -> dict[str, dict]:
    """Walk RSS XML and pull per-item ``<enclosure>`` / ``<media:content>``.

    Returns a dict keyed by ``<link>`` (or by audio URL if no link) mapping
    to ``{audio_url, duration_seconds}``. ``parse_feed`` doesn't surface
    enclosures, so this is a small secondary parse over the same bytes.
    """
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out

    # Only RSS 2.0 carries <enclosure> on <item>; Atom is rare for podcasts.
    channel = root.find("channel")
    if channel is None:
        return out

    for item in channel.findall("item"):
        link_el = item.find("link")
        link = (link_el.text or "").strip() if link_el is not None else ""

        audio_url: Optional[str] = None
        enc = item.find("enclosure")
        if enc is not None:
            audio_url = enc.get("url") or None
        if audio_url is None:
            media = item.find(f"{{{_MEDIA_NS}}}content")
            if media is not None:
                audio_url = media.get("url") or None

        duration: Optional[int] = None
        dur_el = item.find(f"{{{_ITUNES_NS}}}duration")
        if dur_el is not None:
            duration = _parse_duration(dur_el.text or "")

        key = link or (audio_url or "")
        if not key:
            continue
        # Don't overwrite an earlier entry that has the same key.
        out.setdefault(key, {"audio_url": audio_url, "duration_seconds": duration})

    return out


class PodcastFetcher(SourceFetcher):
    """Fetcher for podcast RSS feeds. See module docstring + design §3.4."""

    source_type = "podcast"

    def __init__(self, feeds: list[dict], max_workers: int = DEFAULT_MAX_WORKERS):
        """Initialize.

        ``feeds`` shape (from ``sources.yaml`` ``podcasts:`` block, typically
        filtered through :func:`brf.sources_config.active_podcast_feeds`)::

            [{name: str, url: str, enabled: bool = True,
              reason: str = ""}, ...]

        Disabled feeds are skipped defensively here too (in case the caller
        passed the raw list).
        """
        self.max_workers = max(1, int(max_workers))
        self._feeds: list[dict] = [
            f for f in feeds if f.get("enabled", True) is not False
        ]

    # -- bulk fetch ----------------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Concurrent fetch across all enabled podcast feeds."""
        all_items: list[FeedItem] = []
        if not self._feeds:
            return all_items
        workers = min(self.max_workers, len(self._feeds))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._fetch_one, feed_meta, since): feed_meta
                for feed_meta in self._feeds
            }
            for fut in as_completed(futures):
                feed_meta = futures[fut]
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"[podcast] worker crashed for {feed_meta.get('url')}: {exc}",
                        file=sys.stderr,
                    )
        return all_items

    def _fetch_one(
        self, feed_meta: dict, since: Optional[datetime]
    ) -> list[FeedItem]:
        url = feed_meta["url"]
        items: list[FeedItem] = []
        try:
            r = httpx.get(
                url,
                timeout=DEFAULT_TIMEOUT_SECS,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 BlogResearchFeed/1.0"},
            )
            r.raise_for_status()
            xml_bytes = r.content
            parsed = parse_feed(xml_bytes)
        except Exception as exc:
            print(f"[podcast] FAILED {url}: {exc}", file=sys.stderr)
            return items

        enclosures = _extract_enclosures(xml_bytes)
        source_title = feed_meta.get("name") or parsed.get("title") or url

        since_cmp = since
        if since_cmp is not None and since_cmp.tzinfo is None:
            since_cmp = since_cmp.replace(tzinfo=timezone.utc)

        for entry in parsed["entries"]:
            published_iso = entry.get("published_iso") or None

            if since_cmp is not None and published_iso:
                try:
                    pub_dt = datetime.fromisoformat(published_iso)
                    if pub_dt < since_cmp:
                        continue
                except ValueError:
                    pass

            link = entry.get("link") or ""
            enc_info = enclosures.get(link) or {}
            audio_url = enc_info.get("audio_url")
            duration = enc_info.get("duration_seconds")

            # Use the page URL when present, else audio URL — but we need
            # *some* URL for the id.
            item_url = link or audio_url or ""
            if not item_url:
                continue

            summary = _truncate(
                _strip_html(entry.get("summary") or ""), SUMMARY_MAX_CHARS
            )

            items.append(
                FeedItem(
                    id=make_id("podcast", item_url),
                    source_type="podcast",
                    source=source_title,
                    title=entry.get("title") or "",
                    url=item_url,
                    published=published_iso,
                    summary=summary,
                    has_full=False,
                    needs_firecrawl=False,
                    extra={
                        "audio_url": audio_url,
                        "duration_seconds": duration,
                        "feed_url": url,
                    },
                )
            )

        return items

    # -- drill-down ----------------------------------------------------------

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Download the episode audio and Whisper-transcribe it.

        Returns ``None`` (and logs to stderr) on any failure; never raises.
        Returns ``None`` immediately when no ``audio_url`` is on the item.
        """
        audio_url = (item.extra or {}).get("audio_url")
        if not audio_url:
            print(
                f"[podcast] no audio_url on item {item.id}; cannot transcribe",
                file=sys.stderr,
            )
            return None

        try:
            # Late import: keeps stdlib-only test paths free of heavy deps,
            # and lets tests patch these names on the brf.podcast module.
            from brf import podcast as _podcast_mod
            from brf.config import get_env
        except Exception as exc:
            print(f"[podcast] dependency import failed: {exc}", file=sys.stderr)
            return None

        api_key = get_env("OPENAI_API_KEY")
        if not api_key:
            print(
                f"[podcast] OPENAI_API_KEY not set; cannot transcribe {item.id}",
                file=sys.stderr,
            )
            return None

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            ok, status, err = _podcast_mod._download_audio(audio_url, tmp_path)
            if not ok:
                print(
                    f"[podcast] download failed for {audio_url}: {status} {err}",
                    file=sys.stderr,
                )
                return None

            text, status, err = _podcast_mod._transcribe_whisper(tmp_path, api_key)
            if text is None:
                print(
                    f"[podcast] whisper failed for {audio_url}: {status} {err}",
                    file=sys.stderr,
                )
                return None
            return text.encode("utf-8")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
