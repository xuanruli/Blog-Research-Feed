"""RSS / Atom fetch module for the brf CLI.

Parses ``sources.opml`` with stdlib ``xml.etree.ElementTree``, then pulls each
live feed in parallel via ``feedparser`` and normalizes entries into the dict
schema documented on :func:`fetch_recent`.

Known-broken and summary-only feeds are hardcoded based on the audit in
``SOURCES_HEALTH.md`` (see §1 and §2). Update those sets when the health
check is re-run.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import feedparser
import httpx

# ---------------------------------------------------------------------------
# Health-check derived feed lists. Source: SOURCES_HEALTH.md §1 and §2.
# Keep these in sync with that doc; otherwise broken feeds will spam stderr
# on every cron run and summary-only feeds won't get Firecrawl follow-up.
# ---------------------------------------------------------------------------

# §1: RSS dead links / must-replace. Skip outright until URL is fixed.
SKIP_FEEDS: set[str] = {
    "https://raw.githubusercontent.com/conoro/anthropic-engineering-rss-feed/main/feed.xml",
    "https://jxnl.co/feeds/feed.xml",
    "https://gwern.net/index.xml",
    "https://davidstarsilver.wordpress.com/feed/",
    "https://blog.langchain.com/rss/",
    "https://www.braintrust.dev/blog/rss.xml",
    "https://blog.vllm.ai/feed.xml",
    "https://www.deeplearning.ai/the-batch/feed/",
    "https://jamesg.blog/hf-papers.xml",
    "https://www.reddit.com/r/LocalLLaMA/.rss",
    "https://aiera.com.cn/feed",
    "https://zhidx.com/feed",
    "https://www.geekpark.net/rss",
    "https://feed.infoq.cn",
    "https://www.jiqizhixin.com/rss",
    "https://api.substack.com/feed/podcast/68003.rss",
    "https://feeds.transistor.fm/the-cognitive-revolution",
    "https://feeds.megaphone.fm/CHTH3437994392",
}

# §2: RSS alive but summary-only. needs_firecrawl=True so downstream can
# follow each item's link to fetch full text.
SUMMARY_ONLY_FEEDS: set[str] = {
    "https://eugeneyan.com/rss/",
    "https://lilianweng.github.io/index.xml",
    "https://simonwillison.net/atom/everything/",
    "https://embracethered.com/blog/index.xml",
    "https://zed.dev/blog.rss",
    "https://tldr.tech/api/rss/ai",
    "https://news.ycombinator.com/rss",
    "https://stratechery.com/feed/",
    "https://www.qbitai.com/feed",
}

SUMMARY_MAX_CHARS = 500
DEFAULT_TIMEOUT_SECS = 15
MAX_WORKERS = 10


def _norm(u: str) -> str:
    return u.rstrip("/").lower()


_SKIP_NORM = {_norm(u) for u in SKIP_FEEDS}
_SUMMARY_NORM = {_norm(u) for u in SUMMARY_ONLY_FEEDS}


def _default_opml_path() -> Path:
    """Repo root's ``sources.opml`` (one dir up from this package)."""
    return Path(__file__).resolve().parent.parent / "sources.opml"


def _parse_opml(opml_path: Path) -> list[dict]:
    """Return ``[{title, xml_url, html_url}, ...]`` for every ``type="rss"`` outline."""
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds: list[dict] = []
    # Walk every outline; the OPML has nested category outlines.
    for outline in root.iter("outline"):
        if outline.get("type") != "rss":
            continue
        xml_url = outline.get("xmlUrl")
        if not xml_url:
            continue
        feeds.append({
            "title": outline.get("text") or outline.get("title") or xml_url,
            "xml_url": xml_url,
            "html_url": outline.get("htmlUrl") or "",
        })
    return feeds


def _struct_time_to_iso(st) -> Optional[str]:
    """Convert a ``time.struct_time`` (assumed UTC from feedparser) to ISO8601."""
    if not st:
        return None
    try:
        dt = datetime(*st[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fetch_one(feed_meta: dict, since: Optional[datetime]) -> list[dict]:
    """Fetch a single feed and return normalized items. Logs to stderr on error."""
    xml_url = feed_meta["xml_url"]
    items: list[dict] = []
    needs_firecrawl = _norm(xml_url) in _SUMMARY_NORM

    try:
        r = httpx.get(
            xml_url,
            timeout=DEFAULT_TIMEOUT_SECS,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 BlogResearchFeed/1.0"},
        )
        parsed = feedparser.parse(r.content)
    except Exception as exc:  # network, parse, anything
        print(f"[rss] FAILED {xml_url}: {exc}", file=sys.stderr)
        return items

    if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", None):
        bozo_exc = getattr(parsed, "bozo_exception", None)
        print(f"[rss] bozo {xml_url}: {bozo_exc}", file=sys.stderr)
        return items

    source_title = (
        (parsed.feed.get("title") if hasattr(parsed, "feed") else None)
        or feed_meta["title"]
    )

    for entry in parsed.entries:
        published_iso = (
            _struct_time_to_iso(entry.get("published_parsed"))
            or _struct_time_to_iso(entry.get("updated_parsed"))
        )

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

        # full_text from <content:encoded> if available.
        full_text: Optional[str] = None
        contents = entry.get("content")
        if contents:
            try:
                value = contents[0].get("value")
                if value:
                    full_text = value
            except (AttributeError, IndexError, KeyError):
                full_text = None

        summary = entry.get("summary", "") or entry.get("description", "") or ""

        items.append({
            "source": source_title,
            "source_url": feed_meta["html_url"],
            "title": entry.get("title", "") or "",
            "url": entry.get("link", "") or "",
            "published": published_iso or "",
            "summary": _truncate(summary),
            "full_text": full_text,
            "needs_firecrawl": needs_firecrawl,
        })

    return items


def fetch_recent(
    since: datetime | None = None,
    opml_path: Path | None = None,
) -> list[dict]:
    """Fetch all live feeds in ``sources.opml`` and return normalized items.

    Each item dict contains: ``source``, ``source_url``, ``title``, ``url``,
    ``published`` (ISO8601), ``summary`` (<=500 chars), ``full_text``
    (content:encoded if present, else None), and ``needs_firecrawl`` (True
    for summary-only feeds per ``SOURCES_HEALTH.md`` §2).

    Feeds listed in ``SKIP_FEEDS`` (``SOURCES_HEALTH.md`` §1) are skipped.
    Per-feed errors are logged to stderr; this function never raises.
    """
    opml_path = opml_path or _default_opml_path()
    feeds = _parse_opml(opml_path)
    live_feeds = [f for f in feeds if _norm(f["xml_url"]) not in _SKIP_NORM]

    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, f, since): f for f in live_feeds}
        for fut in as_completed(futures):
            feed_meta = futures[fut]
            try:
                all_items.extend(fut.result())
            except Exception as exc:
                print(
                    f"[rss] worker crashed for {feed_meta['xml_url']}: {exc}",
                    file=sys.stderr,
                )

    return all_items


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    results = fetch_recent()
    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    print(f"\n[rss] {len(results)} items", file=sys.stderr)
