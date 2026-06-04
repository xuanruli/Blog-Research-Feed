"""Shared scaffolding for HTTP-feed-backed fetchers (pool + GET + parse + filter)."""
from __future__ import annotations

import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import httpx

from brf.feed_item import FeedItem

from ._rss_parsing import parse_feed
from .base import SourceFetcher

DEFAULT_TIMEOUT_SECS = 15
DEFAULT_MAX_WORKERS = 10
USER_AGENT = "Mozilla/5.0 BlogResearchFeed/1.0"
SUMMARY_MIN_CHARS = 80  # threshold for "summary is substantive"
SUMMARY_MAX_CHARS = 500


def as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as UTC-aware; ``None`` passes through."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def passes_since(published_iso: Optional[str], since: Optional[datetime]) -> bool:
    """True if new enough to keep; missing/unparseable dates are kept, not dropped."""
    if since is None or not published_iso:
        return True
    try:
        return datetime.fromisoformat(published_iso) >= since
    except ValueError:
        return True


def http_get_feed(url: str, timeout_secs: int = DEFAULT_TIMEOUT_SECS) -> bytes:
    """GET ``url`` with shared polite-client headers; return bytes, raise on failure."""
    r = httpx.get(
        url,
        timeout=timeout_secs,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    r.raise_for_status()
    return r.content


class FeedFetcher(SourceFetcher):
    """Template-method base for RSS/Atom-feed-backed fetchers."""

    log_prefix: str = "[feed]"
    timeout_secs: int = DEFAULT_TIMEOUT_SECS
    max_workers: int = DEFAULT_MAX_WORKERS

    # -- subclass hooks ------------------------------------------------------

    def _feed_units(self) -> list[tuple[str, dict]]:
        """Return the ``[(feed_url, meta), ...]`` to fetch in parallel."""
        raise NotImplementedError

    def _normalize(
        self, entry: dict, meta: dict, source_title: str
    ) -> Optional[FeedItem]:
        """Turn one parsed feed entry into a FeedItem (or ``None`` to drop)."""
        raise NotImplementedError

    def _source_title(self, meta: dict, parsed: dict, url: str) -> str:
        """Display name for a feed: configured name, else feed title, else URL."""
        return meta.get("name") or parsed.get("title") or url

    # -- shared bulk fetch ---------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Concurrent fetch across all feed units; per-unit failures logged and skipped."""
        units = self._feed_units()
        if not units:
            return []

        since_cmp = as_aware(since)
        workers = min(self.max_workers, len(units))
        all_items: list[FeedItem] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._fetch_unit, url, meta, since_cmp): url
                for url, meta in units
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"{self.log_prefix} worker crashed for {url}: {exc}",
                        file=sys.stderr,
                    )
        return all_items

    def _fetch_unit(
        self, url: str, meta: dict, since_cmp: Optional[datetime]
    ) -> list[FeedItem]:
        """Fetch + parse one feed, since-filter, normalize entries."""
        try:
            content = http_get_feed(url, self.timeout_secs)
            parsed = parse_feed(content)
        except Exception as exc:
            print(f"{self.log_prefix} FAILED {url}: {exc}", file=sys.stderr)
            return []

        source_title = self._source_title(meta, parsed, url)
        items: list[FeedItem] = []
        for entry in parsed["entries"]:
            if not passes_since(entry.get("published_iso"), since_cmp):
                continue
            item = self._normalize(entry, meta, source_title)
            if item is not None:
                items.append(item)
        return items
