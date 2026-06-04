"""XFetcher: X (Twitter) user timelines; the tweet text is the body, no drill-down."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from brf.clients.x import fetch_user_recent
from brf.feed_item import FeedItem, make_id

from .base import SourceFetcher

DEFAULT_MAX_WORKERS = 5  # X API rate-limit friendly
LONG_TWEET_THRESHOLD = 270
_THREAD_SUFFIXES = ("🧵", "1/", "(1/")


def _is_thread(text: str) -> bool:
    """Heuristic flag for "this tweet kicks off a thread"."""
    if not text:
        return False
    if text.endswith(_THREAD_SUFFIXES):
        return True
    if text.startswith("1/"):
        return True
    if "/ " in text:
        return True
    return False


class XFetcher(SourceFetcher):
    """Fetcher for X (Twitter) user timelines."""

    source_type = "x"

    def __init__(self, handles: list[str], max_workers: int = DEFAULT_MAX_WORKERS):
        """Handles are X usernames without the leading ``@``."""
        self.handles = [h.lstrip("@") for h in handles]
        self.max_workers = max_workers

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Parallel per-handle fetch; non-ok handles are skipped, never raises."""
        items: list[FeedItem] = []
        if not self.handles:
            return items

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._fetch_one, handle, since): handle for handle in self.handles
            }
            for fut in as_completed(futures):
                handle = futures[fut]
                try:
                    items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"[x] worker crashed for @{handle}: {exc}",
                        file=sys.stderr,
                    )

        return items

    def _fetch_one(self, handle: str, since: datetime) -> list[FeedItem]:
        """Fetch tweets for one handle and normalize them to FeedItems."""
        try:
            resp = fetch_user_recent(handle, since=since)
        except Exception as exc:
            print(f"[x] fetch_user_recent crashed for @{handle}: {exc}", file=sys.stderr)
            return []

        status = resp.get("status")
        if status != "ok":
            print(
                f"[x] skipping @{handle}: status={status} error={resp.get('error_message')!r}",
                file=sys.stderr,
            )
            return []

        out: list[FeedItem] = []
        for tweet in resp.get("posts", []) or []:
            text = tweet.get("text") or ""
            url = tweet.get("url") or ""
            if not url:
                continue
            out.append(
                FeedItem(
                    id=make_id("x", url),
                    source_type="x",
                    source=f"@{handle}",
                    title="",
                    url=url,
                    published=tweet.get("created_at") or None,
                    summary=text,
                    has_full=True,
                    needs_firecrawl=False,
                    extra={
                        "like_count": tweet.get("like_count", 0),
                        "retweet_count": tweet.get("retweet_count", 0),
                        "has_thread": _is_thread(text),
                        "is_long": len(text) >= LONG_TWEET_THRESHOLD,
                    },
                )
            )
        return out

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """No-op: the tweet text is already the body."""
        return None
