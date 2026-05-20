"""FirecrawlIndexFetcher — Phase 4 of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4 (FirecrawlIndexFetcher row) + §3.3
(sequential concurrency note). Responsible for:

* Firecrawl-scrape each configured index page (e.g.,
  ``https://www.anthropic.com/news``), regex-extract article URLs from
  the returned markdown.
* Per-URL parse: optionally pull a date out of the URL via
  ``date_format`` / ``date_group`` (handles ``%Y-%m-%d`` slugs and the
  HF papers ``yymm`` arXiv-id convention).
* Emit ``FeedItem(source_type="firecrawl_index", has_full=False,
  needs_firecrawl=True)``. The agent drills down via ``fetch_full``,
  which firecrawl-scrapes the article URL and returns markdown bytes.

Concurrency: sequential by default (one firecrawl call at a time).
firecrawl-py thread safety isn't guaranteed and the index list is
small (~30 entries × 1 scrape/entry/day).
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Optional

from brf.feed_item import FeedItem, make_id

from .base import SourceFetcher

MAX_ITEMS_PER_INDEX = 25
SUMMARY_MAX_CHARS = 500

# Markdown link extraction from the firecrawl-returned index page.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _url_slug(url: str) -> str:
    """Last non-empty path segment of ``url`` (no query, no trailing slash)."""
    no_query = url.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in no_query.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else ""


def _slug_to_title(url: str) -> str:
    """Best-effort title from a URL slug when the markdown link text is unhelpful."""
    slug = _url_slug(url)
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _parse_index_date(raw: str, fmt: str) -> Optional[datetime]:
    """Parse ``raw`` according to ``fmt``.

    ``fmt`` is normally an ``strptime`` format (``"%Y-%m-%d"``). The
    special value ``"yymm"`` handles the HF papers arXiv-id convention:
    ``2401.12345`` → 2024-01-01. Returns ``None`` on any parse failure.
    """
    if fmt == "yymm":
        # arXiv id like "2401.12345" → year 2024, month 01
        s = raw.split(".", 1)[0]
        if len(s) != 4 or not s.isdigit():
            return None
        yy, mm = int(s[:2]), int(s[2:])
        if not 1 <= mm <= 12:
            return None
        # arXiv started 2007; everything <07 is 20xx-future, otherwise 20xx.
        year = 2000 + yy
        try:
            return datetime(year, mm, 1, tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class FirecrawlIndexFetcher(SourceFetcher):
    """Fetcher for no-feed sites scraped via Firecrawl. See module docstring."""

    source_type = "firecrawl_index"

    def __init__(self, entries: list[dict]):
        """Initialize.

        ``entries`` shape (from ``sources.yaml`` ``firecrawl_index:`` block)::

            [{name: str, url: str,
              article_url_regex: str,
              date_format: str | None,
              date_group: int | None,
              slug_blocklist: list[str] | None,  # optional
              enabled: bool = True}, ...]

        Disabled entries are filtered out. ``article_url_regex`` strings
        are compiled once here so per-entry fetches are zero-overhead.
        Entries with an invalid regex are dropped (logged to stderr) so a
        single typo can't sink the whole run.
        """
        self._entries: list[dict] = []
        for e in entries:
            if e.get("enabled", True) is False:
                continue
            pat = e.get("article_url_regex")
            if not pat:
                print(
                    f"[firecrawl_index] missing article_url_regex on "
                    f"{e.get('name') or e.get('url')!r}; skipping",
                    file=sys.stderr,
                )
                continue
            try:
                compiled = re.compile(pat)
            except re.error as exc:
                print(
                    f"[firecrawl_index] bad regex on "
                    f"{e.get('name') or e.get('url')!r}: {exc}; skipping",
                    file=sys.stderr,
                )
                continue
            self._entries.append({
                "name": e.get("name") or e["url"],
                "url": e["url"],
                "pattern": compiled,
                "date_format": e.get("date_format"),
                "date_group": e.get("date_group"),
                "slug_blocklist": frozenset(e.get("slug_blocklist") or ()),
            })

    # -- bulk fetch ----------------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Sequentially scrape every configured index page.

        Per-entry failures are logged to stderr and skipped — one bad
        firecrawl call must not abort the whole run.
        """
        if not self._entries:
            return []

        try:
            from brf.firecrawl_client import scrape as fc_scrape
        except Exception as exc:
            print(
                f"[firecrawl_index] firecrawl unavailable, skipping all "
                f"{len(self._entries)} index pages: {exc}",
                file=sys.stderr,
            )
            return []

        since_cmp = since
        if since_cmp is not None and since_cmp.tzinfo is None:
            since_cmp = since_cmp.replace(tzinfo=timezone.utc)

        all_items: list[FeedItem] = []
        for entry in self._entries:
            try:
                all_items.extend(self._fetch_one(entry, fc_scrape, since_cmp))
            except Exception as exc:
                print(
                    f"[firecrawl_index] crashed for {entry['url']}: {exc}",
                    file=sys.stderr,
                )
        return all_items

    def _fetch_one(
        self,
        entry: dict,
        fc_scrape,
        since_cmp: Optional[datetime],
    ) -> list[FeedItem]:
        index_url: str = entry["url"]
        try:
            resp = fc_scrape(index_url)
        except Exception as exc:
            print(
                f"[firecrawl_index] scrape failed for {index_url}: {exc}",
                file=sys.stderr,
            )
            return []

        markdown = (resp.get("markdown") or "") if isinstance(resp, dict) else ""
        if not markdown:
            return []

        pattern: re.Pattern[str] = entry["pattern"]
        date_format: Optional[str] = entry["date_format"]
        date_group: Optional[int] = entry["date_group"]
        slug_blocklist: frozenset[str] = entry["slug_blocklist"]
        source_title: str = entry["name"]

        items: list[FeedItem] = []
        seen_urls: set[str] = set()
        for m in _MD_LINK_RE.finditer(markdown):
            link_text = m.group(1).strip()
            url = m.group(2).strip().rstrip(".,;)")

            article_m = pattern.match(url)
            if not article_m:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if slug_blocklist and _url_slug(url) in slug_blocklist:
                continue

            published_iso: Optional[str] = None
            if date_group is not None and date_format:
                try:
                    captured = article_m.group(date_group)
                except (IndexError, re.error):
                    captured = None
                if captured:
                    dt = _parse_index_date(captured, date_format)
                    if dt is not None:
                        if since_cmp is not None and dt < since_cmp:
                            continue
                        published_iso = dt.isoformat()

            # Fall back to a slug-derived title when the markdown link
            # text is empty or looks like a bare URL.
            title = link_text
            if not title or title.startswith("http"):
                title = _slug_to_title(url)

            items.append(FeedItem(
                id=make_id("firecrawl_index", url),
                source_type="firecrawl_index",
                source=source_title,
                title=title[:SUMMARY_MAX_CHARS],
                url=url,
                published=published_iso,
                summary="",
                has_full=False,
                needs_firecrawl=True,
                extra={"index_url": index_url},
            ))
            if len(items) >= MAX_ITEMS_PER_INDEX:
                break

        return items

    # -- drill-down ----------------------------------------------------------

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Firecrawl scrape ``item.url`` and return markdown bytes.

        Returns ``None`` (logged to stderr) on any failure; never raises.
        """
        try:
            from brf.firecrawl_client import scrape as fc_scrape
        except Exception as exc:
            print(
                f"[firecrawl_index] firecrawl unavailable for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            resp = fc_scrape(item.url)
        except Exception as exc:
            print(
                f"[firecrawl_index] fetch_full failed for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        markdown = (resp.get("markdown") or "") if isinstance(resp, dict) else ""
        if not markdown:
            return None
        return markdown.encode("utf-8")
