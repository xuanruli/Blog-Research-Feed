"""RSS / Atom fetch module for the brf CLI.

Parses ``sources.opml`` with stdlib ``xml.etree.ElementTree``, fetches each
live feed in parallel via ``httpx``, and parses the response with a small
stdlib RSS/Atom parser (replaces ``feedparser`` to avoid its sdist-only
``sgmllib3k`` transitive dep — that build silently fails inside Anthropic's
Managed Agent env builder, so we keep brf install pure-PyPI-wheel).

Known-broken and summary-only feeds are hardcoded from ``SOURCES_HEALTH.md``
(see §1 and §2). Update those sets when the health check is re-run.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

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

# XML namespaces. RSS 2.0 itself has no default namespace on its tags;
# content:encoded uses the content namespace; Atom uses its own.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _norm(u: str) -> str:
    return u.rstrip("/").lower()


_SKIP_NORM = {_norm(u) for u in SKIP_FEEDS}
_SUMMARY_NORM = {_norm(u) for u in SUMMARY_ONLY_FEEDS}


def _default_opml_path() -> Path:
    """Find ``sources.opml`` — explicit override, mounted resource, or bundled.

    Priority:

    1. ``$BRF_SOURCES_OPML`` env var (explicit path)
    2. ``/workspace/sources.opml`` (orchestrator-mounted, if present)
    3. Package data: ``importlib.resources.files('brf') / 'sources.opml'``
       (always present in a normal pip install)
    """
    explicit = os.environ.get("BRF_SOURCES_OPML")
    if explicit:
        return Path(explicit)
    mounted = Path("/workspace/sources.opml")
    if mounted.is_file():
        return mounted
    # importlib.resources path → may be a real Path on disk, or a Traversable
    # for zip-installed packages. Both work with ET.parse via str().
    from importlib.resources import files

    return Path(str(files("brf") / "sources.opml"))


def _parse_opml(opml_path: Path) -> list[dict]:
    """Return ``[{title, xml_url, html_url}, ...]`` for every ``type="rss"`` outline."""
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds: list[dict] = []
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


# ---------------------------------------------------------------------------
# Minimal RSS/Atom parser (stdlib only)
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    """Strip ElementTree namespace prefix: ``{http://...}title`` → ``title``."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_pub_date(text: str) -> str:
    """Accept RFC 822 (RSS pubDate) or ISO 8601 (Atom) and return ISO 8601 UTC."""
    text = (text or "").strip()
    if not text:
        return ""
    # ISO 8601 first (Atom + many RSS variants do this too).
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
    # RFC 822 (RSS pubDate canonical).
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return ""


def _parse_rss_item(item) -> dict:
    """One ``<item>`` from RSS 2.0."""
    full_text = _text(item.find("content:encoded", NS))
    return {
        "title": _text(item.find("title")),
        "link": _text(item.find("link")),
        "summary": _text(item.find("description")),
        "full_text": full_text or None,
        "published_iso": _parse_pub_date(
            _text(item.find("pubDate"))
            or _text(item.find("dc:date", NS))
        ),
    }


def _parse_atom_entry(entry) -> dict:
    """One ``<entry>`` from Atom 1.0."""
    # Prefer rel="alternate" link, fall back to first href-bearing link.
    link = ""
    for link_el in entry.findall("atom:link", NS):
        rel = link_el.get("rel", "alternate")
        href = link_el.get("href")
        if href and rel == "alternate":
            link = href
            break
    if not link:
        first = entry.find("atom:link", NS)
        if first is not None:
            link = first.get("href") or ""

    # Atom <content> may be plain text, escaped HTML, or inline xhtml.
    full_text: Optional[str] = None
    content_el = entry.find("atom:content", NS)
    if content_el is not None:
        if content_el.text:
            full_text = content_el.text
        elif len(content_el) > 0:
            # type="xhtml" — serialize children
            full_text = "".join(
                ET.tostring(child, encoding="unicode", method="html")
                for child in content_el
            )

    return {
        "title": _text(entry.find("atom:title", NS)),
        "link": link,
        "summary": _text(entry.find("atom:summary", NS)),
        "full_text": full_text,
        "published_iso": _parse_pub_date(
            _text(entry.find("atom:published", NS))
            or _text(entry.find("atom:updated", NS))
        ),
    }


def _parse_feed(content: bytes) -> dict:
    """Parse RSS 2.0 or Atom 1.0 bytes into ``{title, entries: [...]}``.

    Each entry dict: ``{title, link, summary, full_text, published_iso}``.
    Raises ``ValueError`` on unrecognized root tag or XML parse failure.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    local = _local(root.tag).lower()
    if local == "rss":
        channel = root.find("channel")
        if channel is None:
            return {"title": "", "entries": []}
        return {
            "title": _text(channel.find("title")),
            "entries": [_parse_rss_item(item) for item in channel.findall("item")],
        }
    if local == "feed":
        return {
            "title": _text(root.find("atom:title", NS)),
            "entries": [
                _parse_atom_entry(entry)
                for entry in root.findall("atom:entry", NS)
            ],
        }
    raise ValueError(f"Unknown feed root tag: {local!r}")


def _truncate(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Network fetch + normalize
# ---------------------------------------------------------------------------
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
        r.raise_for_status()
        parsed = _parse_feed(r.content)
    except Exception as exc:  # network, parse, anything
        print(f"[rss] FAILED {xml_url}: {exc}", file=sys.stderr)
        return items

    source_title = parsed.get("title") or feed_meta["title"]

    for entry in parsed["entries"]:
        published_iso = entry["published_iso"]

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

        items.append({
            "source": source_title,
            "source_url": feed_meta["html_url"],
            "title": entry["title"],
            "url": entry["link"],
            "published": published_iso,
            "summary": _truncate(entry["summary"]),
            "full_text": entry["full_text"],
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
