"""Tests for brf.feed_item."""

from __future__ import annotations

import pytest

from brf.feed_item import (
    SCHEMA_VERSION,
    FeedItem,
    _strip_html,
    _truncate,
    make_id,
)


def _sample_item(**overrides) -> FeedItem:
    base = dict(
        id=make_id("rss", "https://example.com/post"),
        source_type="rss",
        source="Example",
        title="Hello",
        url="https://example.com/post",
        published="2026-05-20T00:00:00Z",
        summary="A short summary.",
        has_full=False,
        needs_firecrawl=False,
        extra={"k": "v"},
    )
    base.update(overrides)
    return FeedItem(**base)


# --- make_id -----------------------------------------------------------------


def test_make_id_deterministic_and_short():
    url = "https://example.com/post"
    a = make_id("rss", url)
    b = make_id("rss", url)
    assert a == b
    assert len(a) <= 16
    assert len(a) == 16


def test_make_id_differs_across_source_types_same_url():
    url = "https://example.com/post"
    seen = {make_id(st, url) for st in ("rss", "x", "youtube", "podcast", "firecrawl_index")}
    assert len(seen) == 5, f"collision detected: {seen}"


# --- FeedItem round-trip -----------------------------------------------------


def test_feed_item_round_trip():
    item = _sample_item()
    d = item.to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    restored = FeedItem.from_dict(d)
    assert restored == item


def test_from_dict_missing_schema_version_raises():
    item = _sample_item()
    d = item.to_dict()
    d.pop("schema_version")
    with pytest.raises(ValueError):
        FeedItem.from_dict(d)


def test_from_dict_wrong_schema_version_raises():
    item = _sample_item()
    d = item.to_dict()
    d["schema_version"] = "9.9"
    with pytest.raises(ValueError):
        FeedItem.from_dict(d)


# --- _strip_html -------------------------------------------------------------


def test_strip_html_tags():
    assert _strip_html("<p>hello <b>world</b></p>") == "hello world"


def test_strip_html_entities():
    assert _strip_html("Tom &amp; Jerry") == "Tom & Jerry"
    assert _strip_html("&lt;ok&gt;") == "<ok>"


def test_strip_html_script_block():
    s = "<p>before</p><script>alert('x'); var a=1;</script><p>after</p>"
    out = _strip_html(s)
    assert "alert" not in out
    assert "var a" not in out
    assert "before" in out and "after" in out


def test_strip_html_collapses_whitespace():
    assert _strip_html("a   b\n\nc\t\td") == "a b c d"


# --- _truncate ---------------------------------------------------------------


def test_truncate_short_unchanged():
    assert _truncate("hello", limit=500) == "hello"


def test_truncate_cuts_and_adds_ellipsis():
    s = "x" * 600
    out = _truncate(s, limit=500)
    assert len(out) == 501  # 500 chars + 1 ellipsis char
    assert out.endswith("…")
    assert out[:500] == "x" * 500


def test_truncate_at_exact_limit_unchanged():
    s = "y" * 500
    assert _truncate(s, limit=500) == s
