"""Markdown link helpers shared by the firecrawl-backed fetchers.

Both ``rss`` (firecrawl-fallback lane) and ``firecrawl_index`` pull article
links out of firecrawl-returned markdown and derive slugs from URLs. This is
the single home for that logic.
"""

from __future__ import annotations

import re

# [text](https://url) — pull article links out of firecrawl markdown.
MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def url_slug(url: str) -> str:
    """Last non-empty path segment of ``url`` (no query, no trailing slash)."""
    no_query = url.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in no_query.rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else ""
