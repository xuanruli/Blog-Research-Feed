"""Tests for brf.x_client.fetch_user_recent — referenced-tweet resolution.

The X API truncates a raw retweet's own text to "RT @x: <clipped>". We pass
`expansions=referenced_tweets.id` and rebuild the full text from
`includes.tweets`. These tests mock the httpx layer to assert that.
"""
from __future__ import annotations

import json

import httpx
import pytest

from brf import x_client


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for httpx.Client; routes the two endpoints x_client calls."""

    def __init__(self, user_payload: dict, tweets_payload: dict):
        self._user = user_payload
        self._tweets = tweets_payload
        self.last_params: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str, params: dict | None = None):
        if "/users/by/username/" in url:
            return _FakeResp(200, self._user)
        if "/tweets" in url:
            self.last_params = params
            return _FakeResp(200, self._tweets)
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(x_client, "get_env", lambda *a, **k: "fake-token")


def _install(monkeypatch, tweets_payload: dict) -> _FakeClient:
    fake = _FakeClient({"data": {"id": "999"}}, tweets_payload)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    return fake


def test_retweet_resolves_full_text(monkeypatch):
    """A raw RT (truncated own text) is rebuilt from the referenced tweet."""
    payload = {
        "data": [
            {
                "id": "1",
                "text": "RT @OpenBMB: Thanks to @_akhaliq for contributing MiniCPM…",
                "created_at": "2026-05-23T16:33:21.000Z",
                "public_metrics": {"like_count": 5, "retweet_count": 2},
                "referenced_tweets": [{"type": "retweeted", "id": "100"}],
            }
        ],
        "includes": {
            "tweets": [
                {"id": "100", "author_id": "200",
                 "text": "Thanks to @_akhaliq for contributing MiniCPM-V 4.6 demo — full paper at arxiv.org/abs/2605.12345"}
            ],
            "users": [{"id": "200", "username": "OpenBMB"}],
        },
    }
    _install(monkeypatch, payload)
    out = x_client.fetch_user_recent("_akhaliq")
    assert out["status"] == "ok"
    assert len(out["posts"]) == 1
    p = out["posts"][0]
    assert p["kind"] == "retweet"
    # full referenced text, not the clipped "RT @x: …" version
    assert "arxiv.org/abs/2605.12345" in p["text"]
    assert p["text"].startswith("RT @OpenBMB:")


def test_quote_appends_referenced_text(monkeypatch):
    payload = {
        "data": [
            {
                "id": "2",
                "text": "this is the paper I was waiting for",
                "created_at": "2026-05-23T10:00:00.000Z",
                "public_metrics": {},
                "referenced_tweets": [{"type": "quoted", "id": "101"}],
            }
        ],
        "includes": {
            "tweets": [{"id": "101", "author_id": "201", "text": "OpenHuman: a new VLM benchmark"}],
            "users": [{"id": "201", "username": "someResearcher"}],
        },
    }
    _install(monkeypatch, payload)
    out = x_client.fetch_user_recent("yoheinakajima")
    p = out["posts"][0]
    assert p["kind"] == "quote"
    assert "this is the paper I was waiting for" in p["text"]
    assert "quoting @someResearcher: OpenHuman" in p["text"]


def test_original_tweet_unchanged(monkeypatch):
    payload = {
        "data": [
            {
                "id": "3",
                "text": "got an agent to fork itself",
                "created_at": "2026-05-23T09:00:00.000Z",
                "public_metrics": {},
            }
        ],
        "includes": {},
    }
    _install(monkeypatch, payload)
    p = x_client.fetch_user_recent("yoheinakajima")["posts"][0]
    assert p["kind"] == "original"
    assert p["text"] == "got an agent to fork itself"


def test_excludes_replies_keeps_retweets(monkeypatch):
    """Verify the request asks the API to drop only replies (keep RT/QT)."""
    fake = _install(monkeypatch, {"data": [], "includes": {}})
    x_client.fetch_user_recent("karpathy")
    assert fake.last_params["exclude"] == "replies"
    assert "referenced_tweets.id" in fake.last_params["expansions"]
