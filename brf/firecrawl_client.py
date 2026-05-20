"""Firecrawl scrape/search client.

Thin wrapper around the official `firecrawl-py` SDK that normalizes response
shapes (pydantic models in newer SDKs, plain dicts in older ones) into the
JSON-friendly dicts the CLI emits to stdout.
"""
from __future__ import annotations

from typing import Any

from .config import get_env


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a dict-like or attribute-like object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _client():
    api_key = get_env("FIRECRAWL_API_KEY", required=True)
    try:
        from firecrawl import FirecrawlApp
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(f"firecrawl-py not installed: {e}") from e
    return FirecrawlApp(api_key=api_key)


def scrape(url: str) -> dict:
    """Scrape `url` via Firecrawl.

    Returns {markdown, metadata: {title, author, published, source_url}, status_code}.
    Raises RuntimeError on API failure.
    """
    app = _client()
    try:
        # firecrawl-py v4 renamed the method to `scrape`; the v1/v2
        # `scrape_url` was removed. The response is a `Document` pydantic
        # object — `_get` reads attrs via getattr, so the downstream
        # unwrap (`data`, `success`, `markdown`, `metadata`, etc.) keeps
        # working without an explicit version branch.
        resp = app.scrape(url, formats=["markdown"], only_main_content=True)
    except Exception as e:
        raise RuntimeError(f"Firecrawl scrape failed for {url}: {e}") from e

    # Newer SDKs return the data object directly; older returned {success, data}.
    data = _get(resp, "data", resp)
    success = _get(resp, "success", True)
    if success is False:
        err = _get(resp, "error", "unknown error")
        raise RuntimeError(f"Firecrawl scrape error: {err}")

    markdown = _get(data, "markdown") or ""
    meta = _get(data, "metadata") or {}
    status_code = _get(meta, "statusCode") or _get(meta, "status_code") or _get(data, "statusCode")

    if status_code is not None and isinstance(status_code, int) and status_code >= 400:
        raise RuntimeError(
            f"Firecrawl scrape returned status {status_code} for {url}: "
            f"{_get(meta, 'error') or _get(meta, 'statusMessage') or ''}"
        )

    metadata = {
        "title": _get(meta, "title"),
        "author": _get(meta, "author"),
        "published": (
            _get(meta, "publishedTime")
            or _get(meta, "published")
            or _get(meta, "article:published_time")
        ),
        "source_url": _get(meta, "sourceURL") or _get(meta, "source_url") or url,
    }
    return {"markdown": markdown, "metadata": metadata, "status_code": status_code}


def search(query: str, limit: int = 10) -> list[dict]:
    """Search the web via Firecrawl. Limit hard-capped at 25."""
    capped = max(1, min(limit, 25))
    app = _client()
    try:
        resp = app.search(query, limit=capped)
    except Exception as e:
        raise RuntimeError(f"Firecrawl search failed for query {query!r}: {e}") from e

    success = _get(resp, "success", True)
    if success is False:
        err = _get(resp, "error", "unknown error")
        raise RuntimeError(f"Firecrawl search error: {err}")

    raw_results = _get(resp, "data", None)
    if raw_results is None:
        raw_results = _get(resp, "web") or _get(resp, "results") or []
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("web") or raw_results.get("results") or []

    out: list[dict] = []
    for r in raw_results:
        out.append(
            {
                "url": _get(r, "url"),
                "title": _get(r, "title"),
                "snippet": _get(r, "description") or _get(r, "snippet"),
            }
        )
    return out


def _main(argv: list[str]) -> None:
    import json
    import sys

    if len(argv) < 2:
        print("usage: python -m brf.firecrawl_client {scrape URL | search QUERY [LIMIT]}", file=sys.stderr)
        sys.exit(2)
    cmd = argv[1]
    if cmd == "scrape":
        if len(argv) < 3:
            print("scrape requires a URL", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(scrape(argv[2]), ensure_ascii=False, default=str))
    elif cmd == "search":
        if len(argv) < 3:
            print("search requires a query", file=sys.stderr)
            sys.exit(2)
        limit = int(argv[3]) if len(argv) > 3 else 10
        print(json.dumps(search(argv[2], limit), ensure_ascii=False, default=str))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    import sys
    _main(sys.argv)
