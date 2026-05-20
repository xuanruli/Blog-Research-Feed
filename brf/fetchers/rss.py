"""RssFetcher — Phase 2 of the brf fetcher refactor.

See BRF_FETCHER_DESIGN.md §3.4 ("RssFetcher", "Why RSS variants stay inside
RssFetcher"). Responsible for:

* Concurrent (10 workers) httpx fetch of every enabled feed in ``sources.yaml``.
* Per-entry 3-branch normalize -> ``FeedItem`` (FULL / SUMMARY / TITLE-ONLY).
* Pre-fetched-body storage: when an entry carries ``content:encoded``
  (RSS) or ``<content>`` (Atom), write the raw HTML to
  ``output_dir/full/<id>.html`` so the agent can ``cat`` it without
  paying for a firecrawl scrape.
* Firecrawl-fallback lane for the handful of feeds whose RSS is broken
  but whose HTML index is reachable (jiqizhixin, HF papers, LangChain).
* ``fetch_full`` drill-down: firecrawl-scrape the item URL on demand.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from .base import SourceFetcher
from ._rss_parsing import parse_feed

DEFAULT_TIMEOUT_SECS = 15
MAX_WORKERS = 10
SUMMARY_MIN_CHARS = 80  # threshold for "summary is substantive" branch
SUMMARY_MAX_CHARS = 500
FALLBACK_MAX_ITEMS_PER_FEED = 10

# Markdown link extraction for firecrawl-fallback feeds.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

# ---------------------------------------------------------------------------
# Firecrawl fallback config (ported from legacy brf/rss.py).
#
# TODO Phase 4: move this to FirecrawlIndexFetcher per design doc §12. The
# only reason it still lives here is that Phase 2 ships ahead of the
# sources.yaml `firecrawl_index:` block being wired up; once Phase 4
# ships, delete this dict and let FirecrawlIndexFetcher own it.
# ---------------------------------------------------------------------------
DEFAULT_FIRECRAWL_FALLBACK: dict[str, dict] = {
    "https://www.jiqizhixin.com/rss": {
        "html_url": "https://www.jiqizhixin.com",
        "article_url_regex": re.compile(
            r"https?://www\.jiqizhixin\.com/articles/(\d{4}-\d{2}-\d{2})-\d+"
        ),
        "date_format": "%Y-%m-%d",
        "date_group": 1,
        "source_title": "机器之心",
        "slug_blocklist": frozenset(),
    },
    "https://jamesg.blog/hf-papers.xml": {
        "html_url": "https://huggingface.co/papers",
        "article_url_regex": re.compile(
            r"https?://huggingface\.co/papers/\d{4}\.\d{4,5}"
        ),
        "date_format": None,
        "date_group": None,
        "source_title": "HF Daily Papers",
        "slug_blocklist": frozenset(),
    },
    "https://blog.langchain.com/rss/": {
        "html_url": "https://blog.langchain.com",
        "article_url_regex": re.compile(
            r"https?://blog\.langchain\.(?:com|dev)/"
            r"(?!category/|tag/|author/|page/|rss/?$)"
            r"([a-z0-9]+(?:-[a-z0-9]+)+)/?(?:\?.*)?$"
        ),
        "date_format": None,
        "date_group": None,
        "source_title": "LangChain Blog",
        "slug_blocklist": frozenset({
            "about-us", "contact-us", "privacy-policy",
            "terms-of-service", "terms-of-use", "case-studies",
            "get-started", "sign-up", "sign-in", "log-in", "log-out",
            "blog-rss", "all-posts",
        }),
    },
}


def _norm(u: str) -> str:
    return u.rstrip("/").lower()


def _url_slug(url: str) -> str:
    """Last non-empty path segment of ``url`` (no query, no trailing slash)."""
    no_query = url.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in no_query.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else ""


def _parse_fallback_date(raw: str, fmt: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class RssFetcher(SourceFetcher):
    """Fetcher for RSS/Atom feeds. See module docstring + design §3.4."""

    source_type = "rss"

    def __init__(
        self,
        feeds: list[dict],
        output_dir: Path,
        firecrawl_fallback: dict | None = None,
    ):
        """Initialize.

        ``feeds`` shape (from ``sources.yaml`` ``rss:`` block)::

            [{name: str, url: str, enabled: bool = True,
              summary_only: bool = False}, ...]

        Disabled feeds (``enabled=false``) are silently skipped per the
        sources.yaml convention. ``summary_only: true`` forces
        ``needs_firecrawl=True`` on emitted items even when the
        description is substantive (≥80 chars).

        ``firecrawl_fallback`` (optional): override the default mapping
        of broken-RSS URL -> firecrawl-scrape config. Pass ``None`` to
        use ``DEFAULT_FIRECRAWL_FALLBACK``; pass ``{}`` to disable.
        """
        self.output_dir = Path(output_dir)
        self.full_dir = self.output_dir / "full"
        self.full_dir.mkdir(parents=True, exist_ok=True)

        fallback = (
            DEFAULT_FIRECRAWL_FALLBACK
            if firecrawl_fallback is None
            else firecrawl_fallback
        )
        self._fallback_norm = {_norm(u): cfg for u, cfg in fallback.items()}

        # Split feeds into live (httpx) vs. firecrawl-fallback lanes.
        self._live_feeds: list[dict] = []
        self._fallback_feeds: list[tuple[dict, dict]] = []
        for f in feeds:
            if f.get("enabled", True) is False:
                continue
            n = _norm(f["url"])
            cfg = self._fallback_norm.get(n)
            if cfg is not None:
                self._fallback_feeds.append((f, cfg))
            else:
                self._live_feeds.append(f)

    # -- bulk fetch ----------------------------------------------------------

    def fetch(self, since: datetime) -> Iterable[FeedItem]:
        """Concurrent fetch across all enabled feeds. See class docstring."""
        all_items: list[FeedItem] = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_one, feed_meta, since): feed_meta
                for feed_meta in self._live_feeds
            }
            for fut in as_completed(futures):
                feed_meta = futures[fut]
                try:
                    all_items.extend(fut.result())
                except Exception as exc:
                    print(
                        f"[rss] worker crashed for {feed_meta['url']}: {exc}",
                        file=sys.stderr,
                    )

        # Firecrawl fallbacks: sequential (firecrawl-py thread safety not
        # guaranteed, and only ~3 entries here).
        for feed_meta, cfg in self._fallback_feeds:
            try:
                all_items.extend(
                    self._fetch_firecrawl_fallback(feed_meta, cfg, since)
                )
            except Exception as exc:
                print(
                    f"[rss] fallback crashed for {feed_meta['url']}: {exc}",
                    file=sys.stderr,
                )

        return all_items

    def _fetch_one(
        self, feed_meta: dict, since: Optional[datetime]
    ) -> list[FeedItem]:
        """Fetch one feed via httpx, normalize entries to FeedItem."""
        url = feed_meta["url"]
        items: list[FeedItem] = []
        try:
            r = httpx.get(
                url,
                timeout=DEFAULT_TIMEOUT_SECS,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 BlogResearchFeed/1.0"},
            )
            r.raise_for_status()
            parsed = parse_feed(r.content)
        except Exception as exc:
            print(f"[rss] FAILED {url}: {exc}", file=sys.stderr)
            return items

        source_title = feed_meta.get("name") or parsed.get("title") or url

        for entry in parsed["entries"]:
            published_iso = entry["published_iso"] or None

            if since is not None and published_iso:
                try:
                    pub_dt = datetime.fromisoformat(published_iso)
                    since_cmp = since
                    if since_cmp.tzinfo is None:
                        since_cmp = since_cmp.replace(tzinfo=timezone.utc)
                    if pub_dt < since_cmp:
                        continue
                except ValueError:
                    pass

            item = self._normalize_entry(entry, feed_meta, source_title)
            if item is not None:
                items.append(item)

        return items

    # -- 3-branch normalize --------------------------------------------------

    def _normalize_entry(
        self,
        entry: dict,
        feed_meta: dict,
        source_title: str,
    ) -> Optional[FeedItem]:
        """Apply FULL / SUMMARY / TITLE-ONLY branching (design §3.4)."""
        entry_url = entry.get("link") or ""
        if not entry_url:
            return None

        content_encoded = entry.get("full_text") or ""
        description = entry.get("summary") or ""
        summary_only_flag = bool(feed_meta.get("summary_only", False))
        item_id = make_id("rss", entry_url)

        if content_encoded:
            # FULL branch
            plain = _strip_html(content_encoded)
            summary = _truncate(plain, SUMMARY_MAX_CHARS)
            has_full = True
            needs_firecrawl = False
            self._save_full(item_id, content_encoded)
        else:
            stripped_desc = _strip_html(description)
            if description and len(stripped_desc) >= SUMMARY_MIN_CHARS:
                # SUMMARY branch
                summary = _truncate(stripped_desc, SUMMARY_MAX_CHARS)
                has_full = False
                needs_firecrawl = summary_only_flag
            else:
                # TITLE-ONLY branch
                summary = ""
                has_full = False
                needs_firecrawl = True

        return FeedItem(
            id=item_id,
            source_type="rss",
            source=source_title,
            title=entry.get("title") or "",
            url=entry_url,
            published=entry.get("published_iso") or None,
            summary=summary,
            has_full=has_full,
            needs_firecrawl=needs_firecrawl,
            extra={"source_url": feed_meta.get("html_url", "")},
        )

    def _save_full(self, item_id: str, content: str) -> None:
        """Write raw content:encoded HTML to ``full/<id>.html``."""
        path = self.full_dir / f"{item_id}.html"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            print(f"[rss] failed to write {path}: {exc}", file=sys.stderr)

    # -- firecrawl fallback (TODO Phase 4: move to FirecrawlIndexFetcher) ---

    def _fetch_firecrawl_fallback(
        self,
        feed_meta: dict,
        cfg: dict,
        since: Optional[datetime],
    ) -> list[FeedItem]:
        """Scrape ``cfg['html_url']`` via Firecrawl and emit article items.

        TODO Phase 4: move this to FirecrawlIndexFetcher per design §12.
        """
        try:
            from brf.firecrawl_client import scrape as fc_scrape
        except Exception as exc:
            print(
                f"[rss] firecrawl unavailable, dropping fallback {feed_meta['url']}: {exc}",
                file=sys.stderr,
            )
            return []

        html_url = cfg["html_url"]
        try:
            resp = fc_scrape(html_url)
        except Exception as exc:
            print(
                f"[rss] firecrawl scrape failed for {html_url}: {exc}",
                file=sys.stderr,
            )
            return []

        markdown = resp.get("markdown") or ""
        pattern: re.Pattern[str] = cfg["article_url_regex"]
        date_format: Optional[str] = cfg.get("date_format")
        date_group: Optional[int] = cfg.get("date_group")
        source_title: str = cfg.get("source_title") or feed_meta.get("name") or html_url
        slug_blocklist: frozenset[str] = cfg.get("slug_blocklist") or frozenset()

        since_cmp = since
        if since_cmp is not None and since_cmp.tzinfo is None:
            since_cmp = since_cmp.replace(tzinfo=timezone.utc)

        items: list[FeedItem] = []
        seen_urls: set[str] = set()
        for m in _MD_LINK_RE.finditer(markdown):
            title = m.group(1).strip()
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
                captured = article_m.group(date_group)
                dt = _parse_fallback_date(captured, date_format)
                if dt is not None:
                    if since_cmp is not None and dt < since_cmp:
                        continue
                    published_iso = dt.isoformat()

            items.append(FeedItem(
                id=make_id("rss", url),
                source_type="rss",
                source=source_title,
                title=title,
                url=url,
                published=published_iso,
                summary="",
                has_full=False,
                needs_firecrawl=True,
                extra={"source_url": html_url},
            ))
            if len(items) >= FALLBACK_MAX_ITEMS_PER_FEED:
                break

        return items

    # -- drill-down ----------------------------------------------------------

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Firecrawl scrape ``item.url``, return markdown as bytes.

        Returns ``None`` (and logs to stderr) on any failure; never raises.
        """
        try:
            from brf.firecrawl_client import scrape as fc_scrape
        except Exception as exc:
            print(
                f"[rss] firecrawl unavailable for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            resp = fc_scrape(item.url)
        except Exception as exc:
            print(
                f"[rss] fetch_full failed for {item.url}: {exc}",
                file=sys.stderr,
            )
            return None

        markdown = resp.get("markdown") or ""
        if not markdown:
            return None
        return markdown.encode("utf-8")
