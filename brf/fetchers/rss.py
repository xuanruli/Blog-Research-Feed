"""RssFetcher: RSS/Atom feeds with a 3-branch normalize and firecrawl-fallback lane."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from brf.feed_item import FeedItem, _strip_html, _truncate, make_id

from ._markdown import MD_LINK_RE as _MD_LINK_RE
from ._markdown import url_slug as _url_slug
from .feed_fetcher import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MIN_CHARS,
    FeedFetcher,
    as_aware,
)

FALLBACK_MAX_ITEMS_PER_FEED = 10

# Dormant: these feeds are disabled in feeds.yaml; FirecrawlIndexFetcher covers them.
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
        "article_url_regex": re.compile(r"https?://huggingface\.co/papers/\d{4}\.\d{4,5}"),
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
        "slug_blocklist": frozenset(
            {
                "about-us",
                "contact-us",
                "privacy-policy",
                "terms-of-service",
                "terms-of-use",
                "case-studies",
                "get-started",
                "sign-up",
                "sign-in",
                "log-in",
                "log-out",
                "blog-rss",
                "all-posts",
            }
        ),
    },
}


def _norm(u: str) -> str:
    return u.rstrip("/").lower()


def _parse_fallback_date(raw: str, fmt: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class RssFetcher(FeedFetcher):
    """Fetcher for RSS/Atom feeds."""

    source_type = "rss"
    log_prefix = "[rss]"
    max_workers = 10

    def __init__(
        self,
        feeds: list[dict],
        output_dir: Path,
        firecrawl_fallback: dict | None = None,
    ):
        """Split feeds into live (httpx) and firecrawl-fallback lanes."""
        self.output_dir = Path(output_dir)
        self.full_dir = self.output_dir / "full"
        self.full_dir.mkdir(parents=True, exist_ok=True)

        fallback = DEFAULT_FIRECRAWL_FALLBACK if firecrawl_fallback is None else firecrawl_fallback
        self._fallback_norm = {_norm(u): cfg for u, cfg in fallback.items()}

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

    def _feed_units(self) -> list[tuple[str, dict]]:
        return [(f["url"], f) for f in self._live_feeds]

    def _normalize(self, entry: dict, meta: dict, source_title: str) -> Optional[FeedItem]:
        """Normalize one entry via FULL / SUMMARY / TITLE-ONLY branching."""
        entry_url = entry.get("link") or ""
        if not entry_url:
            return None

        content_encoded = entry.get("full_text") or ""
        description = entry.get("summary") or ""
        summary_only_flag = bool(meta.get("summary_only", False))
        item_id = make_id("rss", entry_url)

        if content_encoded:
            plain = _strip_html(content_encoded)
            summary = _truncate(plain, SUMMARY_MAX_CHARS)
            has_full = True
            needs_firecrawl = False
            self._save_full(item_id, content_encoded)
        else:
            stripped_desc = _strip_html(description)
            if description and len(stripped_desc) >= SUMMARY_MIN_CHARS:
                summary = _truncate(stripped_desc, SUMMARY_MAX_CHARS)
                has_full = False
                needs_firecrawl = summary_only_flag
            else:
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
            extra={"source_url": meta.get("html_url", "")},
        )

    def _save_full(self, item_id: str, content: str) -> None:
        """Write raw content:encoded HTML to ``full/<id>.html``."""
        path = self.full_dir / f"{item_id}.html"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            print(f"[rss] failed to write {path}: {exc}", file=sys.stderr)

    def fetch(self, since: datetime):
        """Run the live lane (pooled) then the sequential firecrawl lane."""
        items = list(super().fetch(since))

        # Sequential: firecrawl-py thread safety is not guaranteed.
        since_cmp = as_aware(since)
        for feed_meta, cfg in self._fallback_feeds:
            try:
                items.extend(self._fetch_firecrawl_fallback(feed_meta, cfg, since_cmp))
            except Exception as exc:
                print(
                    f"[rss] fallback crashed for {feed_meta['url']}: {exc}",
                    file=sys.stderr,
                )
        return items

    def _fetch_firecrawl_fallback(
        self,
        feed_meta: dict,
        cfg: dict,
        since_cmp: Optional[datetime],
    ) -> list[FeedItem]:
        """Scrape ``cfg['html_url']`` via Firecrawl and emit article items."""
        try:
            from brf.clients.firecrawl import scrape as fc_scrape
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

            items.append(
                FeedItem(
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
                )
            )
            if len(items) >= FALLBACK_MAX_ITEMS_PER_FEED:
                break

        return items

    def fetch_full(self, item: FeedItem) -> bytes | None:
        """Firecrawl scrape ``item.url``, return markdown bytes or None on failure."""
        try:
            from brf.clients.firecrawl import scrape as fc_scrape
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
