"""Stdlib RSS / Atom parser shared by ``fetchers/rss.py``.

Lifted verbatim from ``brf/rss.py`` so the new fetcher can be developed
independently of the legacy module's planned consolidation (see
BRF_FETCHER_DESIGN.md §12 Review #7). Once legacy ``brf/rss.py`` is
pointed at ``RssFetcher``, it should re-export from here too.

Public surface:

* ``parse_feed(content) -> {title, entries: [...]}``
* ``parse_pub_date(text) -> str``  (ISO 8601 UTC, "" on failure)

Each entry dict: ``{title, link, summary, full_text, published_iso}``.
``full_text`` carries the raw ``content:encoded`` (RSS) or ``<content>``
(Atom) body when present, otherwise ``None``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from xml.etree import ElementTree as ET

# XML namespaces. RSS 2.0 has no default namespace on its tags;
# content:encoded uses the content namespace; Atom uses its own.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _local(tag: str) -> str:
    """Strip ElementTree namespace prefix: ``{http://...}title`` -> ``title``."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def parse_pub_date(text: str) -> str:
    """Accept RFC 822 (RSS pubDate) or ISO 8601 (Atom) and return ISO 8601 UTC."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
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
        "published_iso": parse_pub_date(
            _text(item.find("pubDate")) or _text(item.find("dc:date", NS))
        ),
    }


def _parse_atom_entry(entry) -> dict:
    """One ``<entry>`` from Atom 1.0."""
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
        "published_iso": parse_pub_date(
            _text(entry.find("atom:published", NS))
            or _text(entry.find("atom:updated", NS))
        ),
    }


def parse_feed(content: bytes) -> dict:
    """Parse RSS 2.0 or Atom 1.0 bytes into ``{title, entries: [...]}``.

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
