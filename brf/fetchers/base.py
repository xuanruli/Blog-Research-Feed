"""SourceFetcher: the ABC every per-type fetcher implements."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime

from brf.feed_item import FeedItem


class SourceFetcher(ABC):
    """Base for per-type source fetchers; each owns its own internal concurrency."""

    source_type: str  # subclass must override

    @abstractmethod
    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Bulk-pull normalized items newer than ``since``."""

    @abstractmethod
    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Drill one item down to its full body as bytes; return None if unavailable, never raise."""
