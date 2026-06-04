"""Compose multiple SourceFetchers into one bulk fetch + drill-down."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from .feed_item import FeedItem
from .fetchers.base import SourceFetcher

# On a same-URL collision the lower number wins: youtube/podcast own their
# native URLs (they carry audio/transcript), rss beats the dateless sources.
_DEDUPE_PRIORITY: dict[str, int] = {
    "youtube": 0,
    "podcast": 1,
    "rss": 2,
    "x": 3,
    "firecrawl_index": 4,
}

_FULL_EXT: dict[str, str] = {
    "rss": "html",
    "youtube": "txt",
    "podcast": "txt",
    "x": "txt",
    "firecrawl_index": "md",
}


class FeedAggregator:
    """Run registered fetchers in parallel, dedupe, and dispatch drill-downs."""

    def __init__(self, fetchers: list[SourceFetcher], output_dir: Path) -> None:
        self.fetchers = list(fetchers)
        self.by_type: dict[str, SourceFetcher] = {f.source_type: f for f in self.fetchers}
        if len(self.by_type) != len(self.fetchers):
            seen = [f.source_type for f in self.fetchers]
            dupes = {st for st in seen if seen.count(st) > 1}
            raise ValueError(f"Duplicate source_type registered: {sorted(dupes)}")
        self.output_dir = Path(output_dir)
        (self.output_dir / "full").mkdir(parents=True, exist_ok=True)

    def fetch_all(self, since: datetime) -> list[FeedItem]:
        """Run all fetchers concurrently, dedupe, and write ``index.json``."""
        if not self.fetchers:
            (self.output_dir / "index.json").write_text("[]\n")
            return []

        all_items: list[FeedItem] = []
        with ThreadPoolExecutor(max_workers=len(self.fetchers)) as pool:
            futures = {
                pool.submit(self._fetch_one_safe, f, since): f.source_type for f in self.fetchers
            }
            for fut in as_completed(futures):
                all_items.extend(fut.result())

        deduped = self._dedupe(all_items)

        index_path = self.output_dir / "index.json"
        index_path.write_text(
            json.dumps(
                [it.to_dict() for it in deduped],
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
        except Exception as exc:  # noqa: BLE001
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

    def fetch_full(self, item_id: str, force: bool = False) -> Optional[Path]:
        """Drill one item down to ``full/<id>.<ext>``; idempotent unless ``force``."""
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
                f"[aggregator] fetch_full for {item.id} ({item.source_type}) crashed: {exc}",
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
