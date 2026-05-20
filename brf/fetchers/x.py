"""XFetcher — Phase 3a of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4 (XFetcher row) and §10 Q2 (fetch_full no-op in v1).

Wraps :func:`brf.x_client.fetch_user_recent` for parallel per-handle fetch.
Tweets are summary-complete (≤280 chars), so:

* ``summary`` is the full tweet text.
* ``has_full=True``, ``needs_firecrawl=False``.
* ``fetch_full`` is a no-op (returns ``None``) — there is no extra body to drill into.
  Phase 3.5+ may extend this for thread context.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from brf.feed_item import FeedItem, make_id
from brf.x_client import fetch_user_recent

from .base import SourceFetcher

DEFAULT_MAX_WORKERS = 5  # X API rate-limit friendly per design §3.3
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
    """Fetcher for X (Twitter) user timelines. See module docstring + design §3.4."""

    source_type = "x"

    def __init__(self, handles: list[str], max_workers: int = DEFAULT_MAX_WORKERS):
        """Initialize.

        ``handles`` is a list of X usernames (without the leading ``@``).
        ``max_workers`` defaults to 5 to stay within X API rate limits
        (see design §3.3 nested-pool table).
        """
        # Normalize: strip stray @ defensively; preserve order for predictability.
        self.handles = [h.lstrip("@") for h in handles]
        self.max_workers = max_workers

    # -- bulk fetch ----------------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Parallel per-handle X API fetch. Handles with status != 'ok' are
        silently skipped (logged to stderr); never raises.
        """
        items: list[FeedItem] = []
        if not self.handles:
            return items

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._fetch_one, handle, since): handle
                for handle in self.handles
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
        """Fetch tweets for one handle and normalize them to FeedItems.

        ``fetch_user_recent`` already returns a structured ``status`` field
        rather than raising on the common error modes (no_credits,
        user_not_found, rate_limited, etc.). We respect that contract and
        just skip non-ok responses, logging to stderr.
        """
        try:
            resp = fetch_user_recent(handle, since=since)
        except Exception as exc:
            # Defensive: x_client catches its own httpx errors, but if
            # something unexpected slips through don't tear down the pool.
            print(f"[x] fetch_user_recent crashed for @{handle}: {exc}", file=sys.stderr)
            return []

        status = resp.get("status")
        if status != "ok":
            print(
                f"[x] skipping @{handle}: status={status} "
                f"error={resp.get('error_message')!r}",
                file=sys.stderr,
            )
            return []

        out: list[FeedItem] = []
        for tweet in resp.get("posts", []) or []:
            text = tweet.get("text") or ""
            url = tweet.get("url") or ""
            if not url:
                continue
            out.append(FeedItem(
                id=make_id("x", url),
                source_type="x",
                source=f"@{handle}",
                title="",  # X has no title
                url=url,
                published=tweet.get("created_at") or None,
                summary=text,
                has_full=True,           # tweet IS the body
                needs_firecrawl=False,   # firecrawl useless on X
                extra={
                    "like_count": tweet.get("like_count", 0),
                    "retweet_count": tweet.get("retweet_count", 0),
                    "has_thread": _is_thread(text),
                    "is_long": len(text) >= LONG_TWEET_THRESHOLD,
                },
            ))
        return out

    # -- drill-down ----------------------------------------------------------

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """No-op in v1 per design §3.4 / §10 Q2.

        Tweet text is already in ``summary``; there is no extra body to
        fetch. Phase 3.5+ may add X-thread context retrieval here.
        """
        return None
