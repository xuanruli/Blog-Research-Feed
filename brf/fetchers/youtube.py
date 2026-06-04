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

The bulk fetch/parse/since-filter scaffolding lives in
:class:`brf.fetchers.feed_fetcher.FeedFetcher`.
"""

from __future__ import annotations

import sys
from typing import Optional

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from .feed_fetcher import SUMMARY_MAX_CHARS, SUMMARY_MIN_CHARS, FeedFetcher

CHANNEL_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


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


class YouTubeFetcher(FeedFetcher):
    """Fetcher for YouTube channels via their Atom channel feeds.

    Concurrency: ``ThreadPoolExecutor(max_workers=10)`` — one worker per
    channel feed. Channel RSS is light HTTP (a few KB per channel), so
    10 in parallel is comfortably under YouTube's polite-client budget.
    """

    source_type = "youtube"
    log_prefix = "[youtube]"

    def __init__(self, channels: list[dict], max_workers: int = 10):
        """Initialize.

        ``channels`` shape (from ``sources.yaml`` ``youtube.channels``)::

            [{name: str, channel_id: str}, ...]
        """
        self.channels = list(channels)
        self.max_workers = max_workers

    # -- FeedFetcher hooks ---------------------------------------------------

    def _feed_units(self) -> list[tuple[str, dict]]:
        return [(CHANNEL_FEED_URL.format(channel_id=ch["channel_id"]), ch) for ch in self.channels]

    def _source_title(self, meta: dict, parsed: dict, url: str) -> str:
        return meta.get("name") or parsed.get("title") or meta.get("channel_id") or url

    def _normalize(self, entry: dict, meta: dict, source_title: str) -> Optional[FeedItem]:
        entry_url = entry.get("link") or ""
        if not entry_url:
            return None

        summary, duration_seconds = self._normalize_summary(entry)
        return FeedItem(
            id=make_id("youtube", entry_url),
            source_type="youtube",
            source=source_title,
            title=entry.get("title") or "",
            url=entry_url,
            published=entry.get("published_iso") or None,
            summary=summary,
            has_full=False,
            needs_firecrawl=False,
            extra={
                "channel_id": meta["channel_id"],
                "duration_seconds": duration_seconds,
            },
        )

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
