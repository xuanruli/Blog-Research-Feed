"""Legacy RSS shim — delegates to :class:`brf.fetchers.rss.RssFetcher`.

This module preserves the public ``fetch_recent()`` function and its dict
schema for back-compat with the deployed Managed Agent (which consumes the
list via ``jq``). All real parsing/fetching now lives in
``brf/fetchers/rss.py`` (see BRF_FETCHER_DESIGN.md §7 Phase 2). Fully
eliminating this module is a deferred Phase 2 task (§12).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# DEPRECATED: use sources.yaml flags instead.
#
# The constants below are retained ONLY as back-compat re-exports for any
# external code that may still import them. The shim itself does NOT
# consult them — RssFetcher reads `enabled` / `summary_only` /
# firecrawl_index entries directly from sources.yaml via sources_config.
# Remove once no importer remains (tracked in BRF_FETCHER_DESIGN.md §12).
# ---------------------------------------------------------------------------
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

    return Path(str(files("brf") / "sources.opml"))


def _parse_opml(opml_path: Path) -> list[dict]:
    """Return ``[{name, url, ...}, ...]`` for every ``type="rss"`` outline.

    Returns dicts shaped like sources.yaml RSS entries so they can be fed
    directly to ``RssFetcher``.
    """
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
            "name": outline.get("text") or outline.get("title") or xml_url,
            "url": xml_url,
            "html_url": outline.get("htmlUrl") or "",
        })
    return feeds


def _resolve_feeds(opml_path: Path | None) -> list[dict]:
    """Pick OPML (explicit or fallback file) vs sources.yaml as the feed list."""
    if opml_path is not None:
        return _parse_opml(opml_path)
    # Back-compat: if BRF_SOURCES_OPML is set or /workspace/sources.opml is
    # mounted, honor the legacy OPML path. Otherwise, the yaml is canonical.
    if os.environ.get("BRF_SOURCES_OPML") or Path("/workspace/sources.opml").is_file():
        return _parse_opml(_default_opml_path())
    from .sources_config import active_rss_feeds, load_sources

    return active_rss_feeds(load_sources())


def fetch_recent(
    since: datetime | None = None,
    opml_path: Path | None = None,
) -> list[dict]:
    """Fetch all live feeds and return legacy-schema dicts.

    Each item dict contains: ``source``, ``source_url``, ``title``, ``url``,
    ``published`` (ISO8601 or ""), ``summary`` (<=500 chars plain text),
    ``full_text`` (HTML inline if available, else None), and
    ``needs_firecrawl``. Schema is frozen for the deployed Managed Agent's
    jq pipeline; see BRF_FETCHER_DESIGN.md §7 Phase 2.
    """
    from .fetchers.rss import RssFetcher

    feeds = _resolve_feeds(opml_path)
    output_dir = Path(os.environ.get("BRF_RSS_OUTPUT_DIR", "/tmp/brf-rss"))
    output_dir.mkdir(parents=True, exist_ok=True)
    full_dir = output_dir / "full"

    fetcher = RssFetcher(feeds=feeds, output_dir=output_dir)
    # `since` is required by SourceFetcher.fetch; legacy callers may pass
    # None to mean "no filter". Forward as-is — RssFetcher handles None.
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
        results.append({
            "source": item.source,
            "source_url": item.extra.get("source_url", "") if item.extra else "",
            "title": item.title,
            "url": item.url,
            "published": item.published or "",
            "summary": item.summary or "",
            "full_text": full_text,
            "needs_firecrawl": bool(item.needs_firecrawl),
        })
    return results


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    results = fetch_recent()
    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    print(f"\n[rss] {len(results)} items", file=sys.stderr)
