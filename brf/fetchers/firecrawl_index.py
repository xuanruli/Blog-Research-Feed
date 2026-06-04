"""FirecrawlIndexFetcher: JSON-extract dated articles from no-feed index pages."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from brf.feed_item import FeedItem, make_id

from ._markdown import MD_LINK_RE as _MD_LINK_RE
from ._markdown import url_slug as _url_slug
from .base import SourceFetcher

MAX_ITEMS_PER_INDEX = 25
SUMMARY_MAX_CHARS = 500
MAX_WORKERS = 8


def _slug_to_title(url: str) -> str:
    """Best-effort title from a URL slug when the markdown link text is unhelpful."""
    slug = _url_slug(url)
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _parse_index_date(raw: str, fmt: str) -> Optional[datetime]:
    """Parse a URL-encoded date by ``fmt`` (strptime, or "yymm" for arXiv ids); None on failure."""
    if fmt == "yymm":
        # Month precision: stamp month-end so same-month items pass a since cutoff.
        import calendar

        s = raw.split(".", 1)[0]
        if len(s) != 4 or not s.isdigit():
            return None
        yy, mm = int(s[:2]), int(s[2:])
        if not 1 <= mm <= 12:
            return None
        year = 2000 + yy
        last_day = calendar.monthrange(year, mm)[1]
        try:
            return datetime(year, mm, last_day, tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse an extracted ``YYYY-MM-DD`` / ISO date to UTC-aware; ``None`` on failure."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class FirecrawlIndexFetcher(SourceFetcher):
    """Fetcher for no-feed sites scraped via Firecrawl."""

    source_type = "firecrawl_index"

    def __init__(self, entries: list[dict]):
        """Compile each entry's ``article_url_regex``; drop disabled or bad-regex entries."""
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
            self._entries.append(
                {
                    "name": e.get("name") or e["url"],
                    "url": e["url"],
                    "pattern": compiled,
                    "date_format": e.get("date_format"),
                    "date_group": e.get("date_group"),
                    "slug_blocklist": frozenset(e.get("slug_blocklist") or ()),
                }
            )

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Scrape every configured index page concurrently; per-entry failures are skipped."""
        if not self._entries:
            return []

        try:
            from brf.clients.firecrawl import scrape_index
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
        workers = min(MAX_WORKERS, len(self._entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._fetch_one, entry, scrape_index, since_cmp): entry
                for entry in self._entries
            }
            for fut in as_completed(futures):
                entry = futures[fut]
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"[firecrawl_index] crashed for {entry['url']}: {exc}",
                        file=sys.stderr,
                    )
        return all_items

    def _fetch_one(
        self,
        entry: dict,
        scrape_index,
        since_cmp: Optional[datetime],
    ) -> list[FeedItem]:
        index_url: str = entry["url"]
        try:
            resp = scrape_index(index_url)
        except Exception as exc:
            print(
                f"[firecrawl_index] scrape failed for {index_url}: {exc}",
                file=sys.stderr,
            )
            return []

        candidates = self._candidates(resp)
        if not candidates:
            return []

        pattern: re.Pattern[str] = entry["pattern"]
        date_format: Optional[str] = entry["date_format"]
        date_group: Optional[int] = entry["date_group"]
        slug_blocklist: frozenset[str] = entry["slug_blocklist"]
        source_title: str = entry["name"]

        items: list[FeedItem] = []
        seen_urls: set[str] = set()
        for link_text, raw_url, raw_date in candidates:
            url = (raw_url or "").split("#", 1)[0].rstrip(".,;)")
            if not url:
                continue
            article_m = pattern.match(url)
            if not article_m:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if slug_blocklist and _url_slug(url) in slug_blocklist:
                continue

            dt = _parse_iso_date(raw_date) or self._url_date(article_m, date_format, date_group)
            if since_cmp is not None and (dt is None or dt < since_cmp):
                continue
            published_iso = dt.isoformat() if dt is not None else None

            title = link_text
            if not title or title.startswith("http"):
                title = _slug_to_title(url)

            items.append(
                FeedItem(
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
                )
            )
            if len(items) >= MAX_ITEMS_PER_INDEX:
                break

        return items

    @staticmethod
    def _candidates(resp) -> list[tuple[str, str, Optional[str]]]:
        """Return ``[(title, url, raw_date), ...]`` from extracted articles, else markdown links."""
        if not isinstance(resp, dict):
            return []
        articles = resp.get("articles") or []
        if articles:
            return [
                (a.get("title") or "", a.get("url") or "", a.get("published")) for a in articles
            ]
        markdown = resp.get("markdown") or ""
        return [
            (m.group(1).strip(), m.group(2).strip(), None) for m in _MD_LINK_RE.finditer(markdown)
        ]

    @staticmethod
    def _url_date(
        article_m: "re.Match[str]", date_format: Optional[str], date_group: Optional[int]
    ) -> Optional[datetime]:
        """Date pulled from the article URL via ``date_format``/``date_group``, if configured."""
        if date_group is None or not date_format:
            return None
        try:
            captured = article_m.group(date_group)
        except (IndexError, re.error):
            return None
        return _parse_index_date(captured, date_format) if captured else None

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Firecrawl scrape ``item.url`` and return markdown bytes, or None on failure."""
        try:
            from brf.clients.firecrawl import scrape as fc_scrape
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
