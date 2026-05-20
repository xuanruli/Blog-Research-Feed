"""Tests for brf.fetchers.x.XFetcher.

Network is fully mocked via monkeypatching ``brf.fetchers.x.fetch_user_recent``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brf.fetchers.base import SourceFetcher
from brf.fetchers.x import XFetcher
from brf.feed_item import make_id


SINCE = datetime(2026, 5, 19, tzinfo=timezone.utc)


def _ok(handle: str, posts: list[dict]) -> dict:
    return {
        "handle": handle,
        "posts": posts,
        "status": "ok",
        "error_message": None,
    }


def _post(
    tid: str = "1",
    handle: str = "karpathy",
    text: str = "hello world",
    likes: int = 10,
    retweets: int = 2,
    created_at: str = "2026-05-19T12:00:00.000Z",
) -> dict:
    return {
        "id": tid,
        "text": text,
        "created_at": created_at,
        "url": f"https://x.com/{handle}/status/{tid}",
        "like_count": likes,
        "retweet_count": retweets,
    }


def _patch(monkeypatch, mapping: dict[str, dict]) -> list[str]:
    """Patch fetch_user_recent to return canned responses keyed by handle.

    Returns a list recording the handles that were queried (for assertions).
    """
    calls: list[str] = []

    def fake(handle: str, since=None, max_results: int = 20):
        calls.append(handle)
        if handle in mapping:
            return mapping[handle]
        return {
            "handle": handle,
            "posts": [],
            "status": "error",
            "error_message": "unmapped handle in test",
        }

    monkeypatch.setattr("brf.fetchers.x.fetch_user_recent", fake)
    return calls


def test_is_subclass_of_sourcefetcher():
    assert issubclass(XFetcher, SourceFetcher)
    assert XFetcher.source_type == "x"


def test_single_handle_two_tweets(monkeypatch):
    _patch(monkeypatch, {
        "karpathy": _ok("karpathy", [
            _post(tid="100", handle="karpathy", text="first tweet", likes=50),
            _post(tid="101", handle="karpathy", text="second tweet", likes=7),
        ])
    })
    items = list(XFetcher(["karpathy"]).fetch(SINCE))
    assert len(items) == 2
    for it in items:
        assert it.source_type == "x"
        assert it.source == "@karpathy"
        assert it.has_full is True
        assert it.needs_firecrawl is False
        assert it.title == ""
        assert it.url.startswith("https://x.com/karpathy/status/")
    summaries = {it.summary for it in items}
    assert summaries == {"first tweet", "second tweet"}
    likes = {it.extra["like_count"] for it in items}
    assert likes == {50, 7}


def test_multiple_handles_merged(monkeypatch):
    _patch(monkeypatch, {
        "karpathy": _ok("karpathy", [_post(tid="1", handle="karpathy", text="k1")]),
        "simonw": _ok("simonw", [
            _post(tid="2", handle="simonw", text="s1"),
            _post(tid="3", handle="simonw", text="s2"),
        ]),
        "natolambert": _ok("natolambert", [_post(tid="4", handle="natolambert", text="n1")]),
    })
    items = list(XFetcher(["karpathy", "simonw", "natolambert"]).fetch(SINCE))
    assert len(items) == 4
    sources = {it.source for it in items}
    assert sources == {"@karpathy", "@simonw", "@natolambert"}


def test_no_credits_skipped(monkeypatch):
    _patch(monkeypatch, {
        "karpathy": _ok("karpathy", [_post(tid="1", handle="karpathy")]),
        "broke": {"handle": "broke", "posts": [],
                  "status": "no_credits", "error_message": "HTTP 402"},
    })
    items = list(XFetcher(["karpathy", "broke"]).fetch(SINCE))
    assert len(items) == 1
    assert items[0].source == "@karpathy"


def test_user_not_found_skipped(monkeypatch):
    _patch(monkeypatch, {
        "karpathy": _ok("karpathy", [_post(tid="1", handle="karpathy")]),
        "ghost": {"handle": "ghost", "posts": [],
                  "status": "user_not_found", "error_message": "no such user"},
    })
    items = list(XFetcher(["karpathy", "ghost"]).fetch(SINCE))
    assert len(items) == 1
    assert items[0].source == "@karpathy"


def test_thread_emoji_marks_has_thread(monkeypatch):
    _patch(monkeypatch, {
        "k": _ok("k", [_post(tid="1", handle="k", text="big announcement 🧵")]),
    })
    items = list(XFetcher(["k"]).fetch(SINCE))
    assert items[0].extra["has_thread"] is True


def test_long_tweet_marks_is_long(monkeypatch):
    long_text = "x" * 270
    _patch(monkeypatch, {
        "k": _ok("k", [_post(tid="1", handle="k", text=long_text)]),
    })
    items = list(XFetcher(["k"]).fetch(SINCE))
    assert items[0].extra["is_long"] is True
    # Short tweet -> is_long False
    _patch(monkeypatch, {
        "k": _ok("k", [_post(tid="2", handle="k", text="short")]),
    })
    items = list(XFetcher(["k"]).fetch(SINCE))
    assert items[0].extra["is_long"] is False


def test_make_id_deterministic_for_same_url(monkeypatch):
    url = "https://x.com/karpathy/status/12345"
    _patch(monkeypatch, {
        "karpathy": _ok("karpathy", [
            {"id": "12345", "text": "hi", "created_at": "2026-05-19T12:00:00Z",
             "url": url, "like_count": 1, "retweet_count": 0},
        ])
    })
    items = list(XFetcher(["karpathy"]).fetch(SINCE))
    assert items[0].id == make_id("x", url)
    # Re-fetch -> same id
    items2 = list(XFetcher(["karpathy"]).fetch(SINCE))
    assert items[0].id == items2[0].id


def test_fetch_full_returns_none(monkeypatch):
    from brf.feed_item import FeedItem
    item = FeedItem(
        id="abc",
        source_type="x",
        source="@karpathy",
        title="",
        url="https://x.com/karpathy/status/1",
        published=None,
        summary="hi",
        has_full=True,
        needs_firecrawl=False,
    )
    assert XFetcher([]).fetch_full(item) is None


def test_empty_handles_returns_no_items(monkeypatch):
    items = list(XFetcher([]).fetch(SINCE))
    assert items == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
