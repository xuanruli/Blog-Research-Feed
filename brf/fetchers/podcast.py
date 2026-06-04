"""PodcastFetcher — Phase 3c of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4 (PodcastFetcher row). Responsible for:

* Concurrent (10 workers) httpx fetch of every enabled podcast RSS feed.
* Per-episode normalize -> ``FeedItem`` with show-notes summary and
  ``extra={audio_url, duration_seconds, feed_url}``. The audio enclosure
  and itunes:duration are surfaced by ``parse_feed`` directly.
* ``fetch_full`` drill-down: download the audio enclosure and transcribe
  via OpenAI Whisper. Returns ``None`` if no audio enclosure is available.

The bulk fetch/parse/since-filter scaffolding lives in
:class:`brf.fetchers.feed_fetcher.FeedFetcher`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from ._rss_parsing import parse_duration as _parse_duration  # re-exported for tests
from .feed_fetcher import SUMMARY_MAX_CHARS, FeedFetcher

__all__ = ["PodcastFetcher", "_parse_duration"]


class PodcastFetcher(FeedFetcher):
    """Fetcher for podcast RSS feeds. See module docstring + design §3.4."""

    source_type = "podcast"
    log_prefix = "[podcast]"

    def __init__(self, feeds: list[dict], max_workers: int = 10):
        """Initialize.

        ``feeds`` shape (from ``feeds.yaml`` ``podcasts:`` block, typically
        filtered through :func:`brf.sources.config.active_podcast_feeds`)::

            [{name: str, url: str, enabled: bool = True,
              reason: str = ""}, ...]

        Disabled feeds are skipped defensively here too (in case the caller
        passed the raw list).
        """
        self.max_workers = max(1, int(max_workers))
        self._feeds: list[dict] = [f for f in feeds if f.get("enabled", True) is not False]

    # -- FeedFetcher hooks ---------------------------------------------------

    def _feed_units(self) -> list[tuple[str, dict]]:
        return [(f["url"], f) for f in self._feeds]

    def _normalize(self, entry: dict, meta: dict, source_title: str) -> Optional[FeedItem]:
        link = entry.get("link") or ""
        audio_url = entry.get("audio_url")
        # Use the page URL when present, else audio URL — but we need
        # *some* URL for the id.
        item_url = link or audio_url or ""
        if not item_url:
            return None

        summary = _truncate(_strip_html(entry.get("summary") or ""), SUMMARY_MAX_CHARS)
        return FeedItem(
            id=make_id("podcast", item_url),
            source_type="podcast",
            source=source_title,
            title=entry.get("title") or "",
            url=item_url,
            published=entry.get("published_iso") or None,
            summary=summary,
            has_full=False,
            needs_firecrawl=False,
            extra={
                "audio_url": audio_url,
                "duration_seconds": entry.get("duration_seconds"),
                "feed_url": meta["url"],
            },
        )

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
            # and lets tests patch these names on the brf.transcription.podcast module.
            from brf.config import get_env
            from brf.transcription import podcast as _podcast_mod
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
