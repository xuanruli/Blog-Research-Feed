"""SourceFetcher ABC — base class for per-type source fetchers.

See BRF_FETCHER_DESIGN.md §3.3 for the full design rationale.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime

from brf.feed_item import FeedItem


class SourceFetcher(ABC):
    """Base for per-type source fetchers.

    Concurrency contract (nested-pool model): each fetcher owns its
    internal parallelism — e.g., ``RssFetcher`` uses a
    ``ThreadPoolExecutor`` over the OPML feed list, ``XFetcher`` fans out
    per handle, etc. The aggregator's job is *only* to run different
    fetchers in parallel; it must NOT manage workers inside any one
    fetcher. A typical run is::

        aggregator pool (5 fetchers in parallel)
          ├── RssFetcher              (10 workers, one per OPML feed)
          ├── XFetcher                ( 5 workers, one per handle, rate-limit capped)
          ├── YouTubeFetcher          (10 workers, one per channel RSS)
          ├── PodcastFetcher          (10 workers, one per podcast RSS)
          └── FirecrawlIndexFetcher   (sequential — firecrawl rate limit)

    Total ~40-45 threads at peak. Negligible at our scale.

    Subclasses MUST override the ``source_type`` class attribute with one
    of the values in ``SourceType`` (see ``brf.feed_item``).
    """

    source_type: str  # class attribute — subclass MUST override

    @abstractmethod
    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Bulk pull items newer than ``since``.

        Implementation owns its internal concurrency (see class docstring).
        Returns an iterable of normalized ``FeedItem`` objects.
        """

    @abstractmethod
    def fetch_full(self, item: FeedItem) -> bytes | None:
        """On-demand drill-down: get the full body / transcript / page.

        Returns ``bytes`` (NOT ``str``) so the aggregator can write to a
        file with the appropriate extension (``.html``, ``.txt``, ``.md``,
        ``.json``) without re-encoding.

        Returns ``None`` if the content is unavailable (e.g., transcript
        disabled, video deleted, scrape failed). Implementations MUST NOT
        raise on unavailability — return ``None`` so the aggregator can
        continue processing other items.
        """
