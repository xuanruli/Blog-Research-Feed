"""Normalized FeedItem schema shared across all source fetchers.

See BRF_FETCHER_DESIGN.md §3.2 / §3.2.1 / §10 Q5.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

SourceType = Literal["rss", "x", "youtube", "podcast", "firecrawl_index"]

SCHEMA_VERSION = "0.1"


def make_id(source_type: str, url: str) -> str:
    """Stable 16-char id derived from source_type+url. See §3.2.1."""
    return hashlib.sha1(f"{source_type}:{url}".encode()).hexdigest()[:16]


@dataclass
class FeedItem:
    """One item the agent sees in index.json. Uniform shape across source types."""

    id: str
    source_type: SourceType
    source: str
    title: str
    url: str
    published: str | None
    summary: str  # always a string, never None
    has_full: bool
    needs_firecrawl: bool
    schema_version: str = SCHEMA_VERSION
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FeedItem":
        sv = d.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ValueError(
                f"FeedItem schema_version mismatch: got {sv!r}, expected {SCHEMA_VERSION!r}"
            )
        return cls(**d)


# --- Shared utility helpers ---------------------------------------------------

_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace. Stdlib only."""
    if not s:
        return ""
    s = _SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _truncate(s: str, limit: int = 500) -> str:
    """Truncate to `limit` chars, appending an ellipsis if cut."""
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "…"
