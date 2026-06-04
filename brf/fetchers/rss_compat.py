"""Legacy ``brf fetch rss`` shim: ``fetch_recent()`` over RssFetcher, frozen dict schema."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# Deprecated back-compat re-exports; the shim itself does not consult them.
SKIP_FEEDS: set[str] = set()
SUMMARY_ONLY_FEEDS: set[str] = set()
FIRECRAWL_FALLBACK_FEEDS: dict[str, dict] = {}


def _default_opml_path() -> Path:
    """Resolve ``sources.opml``: env override, mounted resource, or bundled."""
    explicit = os.environ.get("BRF_SOURCES_OPML")
    if explicit:
        return Path(explicit)
    mounted = Path("/workspace/sources.opml")
    if mounted.is_file():
        return mounted
    from importlib.resources import files

    return Path(str(files("brf.sources") / "feeds.opml"))


def _parse_opml(opml_path: Path) -> list[dict]:
    """Return ``[{name, url, html_url}, ...]`` for every ``type="rss"`` OPML outline."""
    tree = ET.parse(opml_path)
    root = tree.getroot()
    feeds: list[dict] = []
    for outline in root.iter("outline"):
        if outline.get("type") != "rss":
            continue
        xml_url = outline.get("xmlUrl")
        if not xml_url:
            continue
        feeds.append(
            {
                "name": outline.get("text") or outline.get("title") or xml_url,
                "url": xml_url,
                "html_url": outline.get("htmlUrl") or "",
            }
        )
    return feeds


def _resolve_feeds(opml_path: Path | None) -> list[dict]:
    """Pick OPML (explicit or fallback file) vs sources.yaml as the feed list."""
    if opml_path is not None:
        return _parse_opml(opml_path)
    if os.environ.get("BRF_SOURCES_OPML") or Path("/workspace/sources.opml").is_file():
        return _parse_opml(_default_opml_path())
    from ..sources.config import active_rss_feeds, load_sources

    return active_rss_feeds(load_sources())


def fetch_recent(
    since: datetime | None = None,
    opml_path: Path | None = None,
) -> list[dict]:
    """Fetch all live feeds and return the frozen legacy-schema dicts."""
    from .rss import RssFetcher

    feeds = _resolve_feeds(opml_path)
    output_dir = Path(os.environ.get("BRF_RSS_OUTPUT_DIR", "/tmp/brf-rss"))
    output_dir.mkdir(parents=True, exist_ok=True)
    full_dir = output_dir / "full"

    fetcher = RssFetcher(feeds=feeds, output_dir=output_dir)
    items_iter = fetcher.fetch(since)  # type: ignore[arg-type]

    results: list[dict] = []
    for item in items_iter:
        full_text: str | None = None
        if item.has_full:
            full_path = full_dir / f"{item.id}.html"
            if full_path.is_file():
                try:
                    full_text = full_path.read_text(encoding="utf-8")
                except OSError:
                    full_text = None
        results.append(
            {
                "source": item.source,
                "source_url": item.extra.get("source_url", "") if item.extra else "",
                "title": item.title,
                "url": item.url,
                "published": item.published or "",
                "summary": item.summary or "",
                "full_text": full_text,
                "needs_firecrawl": bool(item.needs_firecrawl),
            }
        )
    return results


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    results = fetch_recent()
    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    print(f"\n[rss] {len(results)} items", file=sys.stderr)
