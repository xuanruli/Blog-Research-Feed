"""YouTubeFetcher — Phase 3b of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4. Responsible for:

* Concurrent (10 workers) httpx fetch of every channel's Atom feed
  (``https://www.youtube.com/feeds/videos.xml?channel_id=<id>``).
* Per-entry normalize -> ``FeedItem`` with the "empty media:description"
  fallback (yt-dlp metadata-only) to pad the summary when the channel
  feed ships an empty entry-level description.
* ``fetch_full`` drill-down: ``brf.youtube.get_transcript`` (which
  internally does the youtube-transcript-api -> yt-dlp + Whisper
  two-leg fallback).
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import httpx

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from ._rss_parsing import parse_feed
from .base import SourceFetcher

DEFAULT_TIMEOUT_SECS = 15
SUMMARY_MIN_CHARS = 80
SUMMARY_MAX_CHARS = 500
CHANNEL_FEED_URL = (
    "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
)


def _ytdlp_metadata(url: str) -> Optional[dict]:
    """Single yt-dlp ``extract_info(url, download=False)`` call.

    Returns the info dict or ``None`` on any failure (no network,
    yt-dlp not installed, video private, etc.). NEVER raises — one
    failed metadata call must not sink the whole channel fetch.
    """
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return None

    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        print(f"[youtube] yt-dlp metadata failed for {url}: {exc}", file=sys.stderr)
        return None


class YouTubeFetcher(SourceFetcher):
    """Fetcher for YouTube channels via their Atom channel feeds.

    Concurrency: ``ThreadPoolExecutor(max_workers=10)`` — one worker per
    channel feed. Channel RSS is light HTTP (a few KB per channel), so
    10 in parallel is comfortably under YouTube's polite-client budget.
    """

    source_type = "youtube"

    def __init__(self, channels: list[dict], max_workers: int = 10):
        """Initialize.

        ``channels`` shape (from ``sources.yaml`` ``youtube.channels``)::

            [{name: str, channel_id: str}, ...]
        """
        self.channels = list(channels)
        self.max_workers = max_workers

    # -- bulk fetch ----------------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Concurrent fetch across all configured channels."""
        all_items: list[FeedItem] = []
        if not self.channels:
            return all_items

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._fetch_one, ch, since): ch
                for ch in self.channels
            }
            for fut in as_completed(futures):
                ch = futures[fut]
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"[youtube] worker crashed for {ch.get('channel_id')}: {exc}",
                        file=sys.stderr,
                    )
        return all_items

    def _fetch_one(
        self, channel: dict, since: Optional[datetime]
    ) -> list[FeedItem]:
        """Fetch one channel's Atom feed, normalize entries to FeedItem."""
        channel_id = channel["channel_id"]
        url = CHANNEL_FEED_URL.format(channel_id=channel_id)
        items: list[FeedItem] = []

        try:
            r = httpx.get(
                url,
                timeout=DEFAULT_TIMEOUT_SECS,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 BlogResearchFeed/1.0"},
            )
            r.raise_for_status()
            parsed = parse_feed(r.content)
        except Exception as exc:
            print(f"[youtube] FAILED {url}: {exc}", file=sys.stderr)
            return items

        source_name = channel.get("name") or parsed.get("title") or channel_id

        since_cmp = since
        if since_cmp is not None and since_cmp.tzinfo is None:
            since_cmp = since_cmp.replace(tzinfo=timezone.utc)

        for entry in parsed["entries"]:
            entry_url = entry.get("link") or ""
            if not entry_url:
                continue

            published_iso = entry.get("published_iso") or None
            if since_cmp is not None and published_iso:
                try:
                    pub_dt = datetime.fromisoformat(published_iso)
                    if pub_dt < since_cmp:
                        continue
                except ValueError:
                    pass

            summary, duration_seconds = self._normalize_summary(entry)

            items.append(FeedItem(
                id=make_id("youtube", entry_url),
                source_type="youtube",
                source=source_name,
                title=entry.get("title") or "",
                url=entry_url,
                published=published_iso,
                summary=summary,
                has_full=False,
                needs_firecrawl=False,
                extra={
                    "channel_id": channel_id,
                    "duration_seconds": duration_seconds,
                },
            ))
        return items

    # -- normalize: empty-description fallback (design §3.4 (a)) -------------

    def _normalize_summary(self, entry: dict) -> tuple[str, Optional[int]]:
        """Return ``(summary, duration_seconds)`` for one Atom entry.

        Three-tier fallback:
          1. entry-level ``<summary>`` (media:description in Atom): if
             ≥ ``SUMMARY_MIN_CHARS`` after HTML strip, use it.
          2. else yt-dlp ``extract_info(url, download=False)`` — single
             HTTP, no API key. Use its ``description`` if present.
          3. else ``""`` (title-only path).

        ``duration_seconds`` is taken from yt-dlp metadata when (2) ran.
        """
        raw = (entry.get("summary") or "").strip()
        stripped = _strip_html(raw)
        if len(stripped) >= SUMMARY_MIN_CHARS:
            return _truncate(stripped, SUMMARY_MAX_CHARS), None

        entry_url = entry.get("link") or ""
        if not entry_url:
            return "", None

        meta = _ytdlp_metadata(entry_url)
        if meta:
            desc = (meta.get("description") or "").strip()
            duration = meta.get("duration")
            duration_seconds = int(duration) if isinstance(duration, (int, float)) else None
            if desc:
                return _truncate(_strip_html(desc), SUMMARY_MAX_CHARS), duration_seconds
            return "", duration_seconds
        return "", None

    # -- drill-down ----------------------------------------------------------

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Fetch a video's transcript on demand.

        Wraps ``brf.youtube.get_transcript``, which already implements
        the captions -> Whisper two-leg fallback. Returns the transcript
        text as UTF-8 bytes, or ``None`` if both legs failed.

        NEVER raises — returns ``None`` on any error.
        """
        try:
            from brf.youtube import get_transcript
        except Exception as exc:
            print(
                f"[youtube] transcript module unavailable for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            result = get_transcript(item.url)
        except Exception as exc:
            print(
                f"[youtube] fetch_full crashed for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        text = result.get("transcript") if isinstance(result, dict) else None
        if not text:
            return None
        return text.encode("utf-8")
