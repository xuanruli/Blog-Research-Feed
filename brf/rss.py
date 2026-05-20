"""RSS / Atom fetch module for the brf CLI.

Parses ``sources.opml`` with stdlib ``xml.etree.ElementTree``, fetches each
live feed in parallel via ``httpx``, and parses the response with a small
stdlib RSS/Atom parser (replaces ``feedparser`` to avoid its sdist-only
``sgmllib3k`` transitive dep — that build silently fails inside Anthropic's
Managed Agent env builder, so we keep brf install pure-PyPI-wheel).

Known-broken and summary-only feeds are hardcoded from ``SOURCES_HEALTH.md``
(see §1, §1.5, and §2). Update those sets when the health check is re-run.

Three feed-handling lanes:

* ``SKIP_FEEDS`` — RSS dead and no good HTML fallback. Dropped silently.
* ``FIRECRAWL_FALLBACK_FEEDS`` — RSS dead but the publisher's HTML index is
  reachable. We scrape the index via Firecrawl and emit one item per
  article link matching the configured pattern. Costs ~$0.005/call.
* ``SUMMARY_ONLY_FEEDS`` — RSS live but only ships titles/short summaries.
  We tag items with ``needs_firecrawl=True`` so the agent knows to
  Firecrawl the article URL on its own.
"""
from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

# ---------------------------------------------------------------------------
# Health-check derived feed lists. Source: SOURCES_HEALTH.md §1, §1.5, §2.
# Keep these in sync with that doc; otherwise broken feeds will spam stderr
# on every cron run and summary-only feeds won't get Firecrawl follow-up.
# ---------------------------------------------------------------------------

# §1: RSS dead links / must-replace. Skip outright until URL is fixed.
SKIP_FEEDS: set[str] = {
    "https://raw.githubusercontent.com/conoro/anthropic-engineering-rss-feed/main/feed.xml",
    "https://jxnl.co/feeds/feed.xml",
    "https://gwern.net/index.xml",
    "https://davidstarsilver.wordpress.com/feed/",
    "https://www.braintrust.dev/blog/rss.xml",
    "https://blog.vllm.ai/feed.xml",
    "https://www.deeplearning.ai/the-batch/feed/",
    "https://www.reddit.com/r/LocalLLaMA/.rss",
    "https://aiera.com.cn/feed",
    "https://zhidx.com/feed",
    "https://www.geekpark.net/rss",
    "https://feed.infoq.cn",
    "https://api.substack.com/feed/podcast/68003.rss",
    "https://feeds.transistor.fm/the-cognitive-revolution",
    "https://feeds.megaphone.fm/CHTH3437994392",
}

# §1.5: RSS broken but HTML index is reachable. Scrape via Firecrawl and
# emit one item per article link matching the configured pattern. Each
# entry maps the (broken) RSS URL to:
#   - html_url:           the index page Firecrawl should scrape
#   - article_url_regex:  matched against every markdown link on the page;
#                         only matches are emitted as feed items
#   - date_format:        Python strptime format applied to the captured
#                         group, or the sentinel "yymm" for arXiv-style
#                         IDs, or None if the URL carries no date
#   - date_group:         which regex group holds the date (None if no
#                         date is extractable)
#   - source_title:       human-readable source name in the emitted items
FIRECRAWL_FALLBACK_FEEDS: dict[str, dict] = {
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
        # arXiv IDs encode YYMM but not the day. Deriving "2025-09-01"
        # from "2509" makes a `since=2025-09-15` filter let in the
        # entire month — false sense of freshness. Better to emit
        # whatever shows up on the index page (newest-first), capped
        # by FALLBACK_MAX_ITEMS_PER_FEED, and let the agent dedup.
        "date_format": None,
        "date_group": None,
        "source_title": "HF Daily Papers",
        "slug_blocklist": frozenset(),
    },
    "https://blog.langchain.com/rss/": {
        "html_url": "https://blog.langchain.com",
        # LangChain post slugs are kebab-case. The negative lookahead
        # drops obvious non-article paths (category / tag / author /
        # paginated index / the rss endpoint itself). slug_blocklist
        # below catches the boilerplate slugs we know about. The
        # ≥1-hyphen requirement filters single-word top-level pages
        # like /pricing — but a real post might still be ≥1 word; we
        # can't pre-empt every false positive from this sandbox
        # without sample markdown to test against.
        "article_url_regex": re.compile(
            r"https?://blog\.langchain\.(?:com|dev)/"
            r"(?!category/|tag/|author/|page/|rss/?$)"
            r"([a-z0-9]+(?:-[a-z0-9]+)+)/?(?:\?.*)?$"
        ),
        "date_format": None,
        "date_group": None,
        "source_title": "LangChain Blog",
        # Common boilerplate slugs that share the kebab-case shape but
        # aren't blog posts. Extend as field experience reveals more.
        "slug_blocklist": frozenset({
            "about-us", "contact-us", "privacy-policy",
            "terms-of-service", "terms-of-use", "case-studies",
            "get-started", "sign-up", "sign-in", "log-in", "log-out",
            "blog-rss", "all-posts",
        }),
    },
}

# Cap per-source items to bound cost-amplification and dedup risk: index
# pages can list dozens of stale links.
FALLBACK_MAX_ITEMS_PER_FEED = 10

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

# Markdown link extraction for FIRECRAWL_FALLBACK_FEEDS. We deliberately
# disallow newlines inside the title so we don't span unrelated entries.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")

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
_FALLBACK_NORM = {_norm(u): cfg for u, cfg in FIRECRAWL_FALLBACK_FEEDS.items()}


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


def _parse_fallback_date(raw: str, fmt: str) -> Optional[datetime]:
    """Parse a date captured from an article URL. Returns None on failure."""
    try:
        return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _url_slug(url: str) -> str:
    """Last non-empty path segment of ``url`` (no trailing slash, no query)."""
    no_query = url.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in no_query.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else ""


def _fetch_firecrawl_fallback(
    feed_meta: dict,
    cfg: dict,
    since: Optional[datetime],
) -> list[dict]:
    """Scrape ``cfg['html_url']`` via Firecrawl and emit article items.

    Used for sources whose RSS endpoint is broken but whose HTML index is
    reachable (see ``FIRECRAWL_FALLBACK_FEEDS``). Returns at most
    ``FALLBACK_MAX_ITEMS_PER_FEED`` items, preserving the order they appear
    on the index page (which is typically newest-first).

    Errors (firecrawl unavailable, scrape failure, key missing) are logged
    to stderr; the function returns an empty list rather than raising.
    """
    try:
        from .firecrawl_client import scrape as fc_scrape
    except Exception as exc:
        print(
            f"[rss] firecrawl unavailable, dropping fallback {feed_meta['xml_url']}: {exc}",
            file=sys.stderr,
        )
        return []

    html_url = cfg["html_url"]
    try:
        resp = fc_scrape(html_url)
    except Exception as exc:
        print(f"[rss] firecrawl scrape failed for {html_url}: {exc}", file=sys.stderr)
        return []

    markdown = resp.get("markdown") or ""
    pattern: re.Pattern[str] = cfg["article_url_regex"]
    date_format: Optional[str] = cfg.get("date_format")
    date_group: Optional[int] = cfg.get("date_group")
    source_title: str = cfg["source_title"]
    slug_blocklist: frozenset[str] = cfg.get("slug_blocklist") or frozenset()

    since_cmp = since
    if since_cmp is not None and since_cmp.tzinfo is None:
        since_cmp = since_cmp.replace(tzinfo=timezone.utc)

    items: list[dict] = []
    seen_urls: set[str] = set()
    for m in _MD_LINK_RE.finditer(markdown):
        title = m.group(1).strip()
        # Markdown URLs sometimes inherit trailing punctuation from surrounding
        # prose; strip the common offenders.
        url = m.group(2).strip().rstrip(".,;)")
        article_m = pattern.match(url)
        if not article_m:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        if slug_blocklist and _url_slug(url) in slug_blocklist:
            continue

        published_iso = ""
        if date_group is not None and date_format:
            captured = article_m.group(date_group)
            dt = _parse_fallback_date(captured, date_format)
            if dt is not None:
                if since_cmp is not None and dt < since_cmp:
                    continue
                published_iso = dt.isoformat()

        items.append({
            "source": source_title,
            "source_url": html_url,
            "title": title,
            "url": url,
            "published": published_iso,
            "summary": "",
            "full_text": None,
            "needs_firecrawl": True,
        })
        if len(items) >= FALLBACK_MAX_ITEMS_PER_FEED:
            break

    return items


def fetch_recent(
    since: datetime | None = None,
    opml_path: Path | None = None,
) -> list[dict]:
    """Fetch all live feeds in ``sources.opml`` and return normalized items.

    Each item dict contains: ``source``, ``source_url``, ``title``, ``url``,
    ``published`` (ISO8601), ``summary`` (<=500 chars), ``full_text``
    (content:encoded if present, else None), and ``needs_firecrawl`` (True
    for summary-only feeds per ``SOURCES_HEALTH.md`` §2 and for all items
    emitted via the Firecrawl fallback).

    Feeds listed in ``SKIP_FEEDS`` (``SOURCES_HEALTH.md`` §1) are skipped.
    Feeds listed in ``FIRECRAWL_FALLBACK_FEEDS`` (§1.5) are routed through
    a Firecrawl scrape of the publisher's HTML index instead of httpx.
    Per-feed errors are logged to stderr; this function never raises.
    """
    opml_path = opml_path or _default_opml_path()
    feeds = _parse_opml(opml_path)

    live_feeds: list[dict] = []
    fallback_feeds: list[tuple[dict, dict]] = []
    for f in feeds:
        n = _norm(f["xml_url"])
        if n in _SKIP_NORM:
            continue
        fallback_cfg = _FALLBACK_NORM.get(n)
        if fallback_cfg is not None:
            fallback_feeds.append((f, fallback_cfg))
        else:
            live_feeds.append(f)

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

    # Firecrawl fallbacks run sequentially: firecrawl-py thread safety is
    # not guaranteed and there are only a handful (~3) of them per run.
    for feed_meta, cfg in fallback_feeds:
        try:
            all_items.extend(_fetch_firecrawl_fallback(feed_meta, cfg, since))
        except Exception as exc:
            print(
                f"[rss] fallback crashed for {feed_meta['xml_url']}: {exc}",
                file=sys.stderr,
            )

    return all_items


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    results = fetch_recent()
    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    print(f"\n[rss] {len(results)} items", file=sys.stderr)
