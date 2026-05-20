"""Composer over multiple :class:`SourceFetcher` instances.

Phase 1: skeleton only — no fetcher subclasses are registered yet.
Phase 2 onwards wires up RssFetcher / XFetcher / YouTubeFetcher /
PodcastFetcher / FirecrawlIndexFetcher.

Output layout under ``output_dir``:
    index.json                  — list[FeedItem.to_dict()] (light, agent reads)
    full/<id>.{html,txt,md,json} — per-item bodies (drilled-down on demand)

See :doc:`/BRF_FETCHER_DESIGN.md` §3.5 for the design rationale.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .feed_item import FeedItem
from .fetchers.base import SourceFetcher

# When the same URL surfaces from multiple fetchers, higher priority
# (lower number) wins. Rationale per design doc §3.5:
# - rss has the richest metadata (published date, content:encoded body)
# - youtube/podcast carry duration + transcript drill-down
# - x is summary-complete but has no title
# - firecrawl_index is the weakest (regex on HTML, may have stale dates
#   or wrong titles) — only used when nothing else covers the source.
_DEDUPE_PRIORITY: dict[str, int] = {
    "rss": 0,
    "youtube": 1,
    "podcast": 2,
    "x": 3,
    "firecrawl_index": 4,
}

# Extension per source_type for the per-item body file under full/.
_FULL_EXT: dict[str, str] = {
    "rss": "html",            # content:encoded HTML or scraped article markdown
    "youtube": "txt",         # transcript text
    "podcast": "txt",         # Whisper transcript text
    "x": "txt",               # rarely used (tweet already in summary)
    "firecrawl_index": "md",  # firecrawl returns markdown
}


class FeedAggregator:
    """Compose multiple :class:`SourceFetcher` into one bulk operation.

    Skeleton class — `fetch_all` runs whatever fetchers are registered
    (zero in Phase 1), `fetch_full` dispatches by ``source_type`` to the
    appropriate registered fetcher. As subsequent phases add fetcher
    classes, the builder in :func:`brf.main` registers them here.

    Concurrency model (per design doc §3.3): aggregator runs each
    registered fetcher in its own thread (outer pool). Each fetcher
    owns its own internal parallelism (inner pool). Aggregator does
    NOT manage workers inside any one fetcher.
    """

    def __init__(self, fetchers: list[SourceFetcher], output_dir: Path) -> None:
        self.fetchers = list(fetchers)
        self.by_type: dict[str, SourceFetcher] = {f.source_type: f for f in self.fetchers}
        # Reject duplicate registrations — exposes config typos early.
        if len(self.by_type) != len(self.fetchers):
            seen = [f.source_type for f in self.fetchers]
            dupes = {st for st in seen if seen.count(st) > 1}
            raise ValueError(f"Duplicate source_type registered: {sorted(dupes)}")
        self.output_dir = Path(output_dir)
        (self.output_dir / "full").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Bulk fetch
    # ------------------------------------------------------------------
    def fetch_all(self, since: datetime) -> list[FeedItem]:
        """Run all fetchers concurrently, dedupe, write ``index.json``.

        Returns the deduped item list. Per-fetcher failures are logged
        to stderr and skipped — one bad fetcher doesn't sink the run.
        """
        if not self.fetchers:
            # Phase 1 scaffolding state: write an empty index so consumers
            # don't crash on file-missing.
            (self.output_dir / "index.json").write_text("[]\n")
            return []

        all_items: list[FeedItem] = []
        with ThreadPoolExecutor(max_workers=len(self.fetchers)) as pool:
            futures = {
                pool.submit(self._fetch_one_safe, f, since): f.source_type
                for f in self.fetchers
            }
            for fut in as_completed(futures):
                all_items.extend(fut.result())

        deduped = self._dedupe(all_items)

        index_path = self.output_dir / "index.json"
        index_path.write_text(
            json.dumps(
                [asdict(it) for it in deduped],
                ensure_ascii=False,
                indent=2,
            )
        )
        return deduped

    @staticmethod
    def _fetch_one_safe(fetcher: SourceFetcher, since: datetime) -> list[FeedItem]:
        """Run one fetcher's `fetch`, swallow exceptions to stderr."""
        try:
            return list(fetcher.fetch(since))
        except Exception as exc:  # noqa: BLE001 — fetcher failures must not abort the run
            print(
                f"[aggregator] fetcher {fetcher.source_type!r} crashed: {exc}",
                file=sys.stderr,
            )
            return []

    @staticmethod
    def _dedupe(items: list[FeedItem]) -> list[FeedItem]:
        """Collapse same-URL items, keeping the highest-priority source_type."""
        winner: dict[str, FeedItem] = {}
        for it in items:
            existing = winner.get(it.url)
            if existing is None:
                winner[it.url] = it
                continue
            if _DEDUPE_PRIORITY.get(it.source_type, 99) < _DEDUPE_PRIORITY.get(
                existing.source_type, 99
            ):
                winner[it.url] = it
        return list(winner.values())

    # ------------------------------------------------------------------
    # Drill-down
    # ------------------------------------------------------------------
    def fetch_full(self, item_id: str, force: bool = False) -> Optional[Path]:
        """Resolve ``item_id`` in ``index.json``, dispatch to the matching
        fetcher's ``fetch_full``, write the body to ``full/<id>.<ext>``.

        Idempotent by default: if the body file already exists, return its
        path without re-fetching. Pass ``force=True`` to re-fetch and
        overwrite.

        Returns the path to the written body, or ``None`` if the item id
        was not found OR the fetcher returned ``None`` (e.g., transcript
        unavailable).
        """
        item = self._load_item(item_id)
        if item is None:
            return None

        ext = _FULL_EXT.get(item.source_type, "bin")
        path = self.output_dir / "full" / f"{item.id}.{ext}"

        if path.exists() and not force:
            return path

        fetcher = self.by_type.get(item.source_type)
        if fetcher is None:
            print(
                f"[aggregator] no fetcher registered for source_type "
                f"{item.source_type!r}; item {item.id} cannot drill down",
                file=sys.stderr,
            )
            return None

        try:
            content = fetcher.fetch_full(item)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[aggregator] fetch_full for {item.id} ({item.source_type}) "
                f"crashed: {exc}",
                file=sys.stderr,
            )
            return None

        if content is None:
            return None

        path.write_bytes(content)
        return path

    def _load_item(self, item_id: str) -> Optional[FeedItem]:
        """Read ``index.json``, find item by id."""
        index_path = self.output_dir / "index.json"
        if not index_path.is_file():
            return None
        try:
            data = json.loads(index_path.read_text())
        except json.JSONDecodeError as exc:
            print(
                f"[aggregator] index.json malformed: {exc}",
                file=sys.stderr,
            )
            return None
        for d in data:
            if d.get("id") == item_id:
                return FeedItem.from_dict(d)
        return None
