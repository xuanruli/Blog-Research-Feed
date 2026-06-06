"""Tests for ThreadSessionMap."""

from slackbot.threads import ThreadSessionMap


def test_remember_then_lookup():
    m = ThreadSessionMap({})
    m.remember("1700.1", "sesn_abc")
    assert m.lookup("1700.1") == "sesn_abc"


def test_lookup_unknown_returns_none():
    assert ThreadSessionMap({}).lookup("nope") is None
