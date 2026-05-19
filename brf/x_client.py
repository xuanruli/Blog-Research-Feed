"""X (Twitter) API v2 client for fetching recent user posts.

Handles the common error modes — including HTTP 402 CreditsDepleted, since
the dev account this ships with has zero credits — by returning a structured
status field rather than raising.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .config import get_env

_BASE = "https://api.twitter.com/2"
_TIMEOUT = 10.0


def _status_for(code: int) -> str:
    return {
        200: "ok",
        401: "error",
        402: "no_credits",
        404: "user_not_found",
        429: "rate_limited",
    }.get(code, "error")


def fetch_user_recent(
    handle: str,
    since: datetime | None = None,
    max_results: int = 20,
) -> dict:
    """Fetch recent original tweets from an X user.

    Returns a dict with keys: handle, posts, status, error_message.
    """
    handle = handle.lstrip("@")
    result: dict = {
        "handle": handle,
        "posts": [],
        "status": "ok",
        "error_message": None,
    }

    token = get_env("X_BEARER_TOKEN", required=False)
    if not token:
        result["status"] = "error"
        result["error_message"] = "X_BEARER_TOKEN not set"
        return result

    now = datetime.now(timezone.utc)
    if since is None:
        since = now - timedelta(hours=24)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    # X API v2 rejects start_time newer than now-10s.
    max_start = now - timedelta(seconds=10)
    if since > max_start:
        since = max_start
    start_time = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {"Authorization": f"Bearer {token}"}

    try:
        with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
            # 1. Resolve handle -> user id
            r = client.get(f"{_BASE}/users/by/username/{handle}")
            if r.status_code != 200:
                result["status"] = _status_for(r.status_code)
                result["error_message"] = f"users/by/username HTTP {r.status_code}: {r.text[:300]}"
                return result
            data = r.json().get("data")
            if not data:
                result["status"] = "user_not_found"
                result["error_message"] = f"No user data returned for @{handle}"
                return result
            user_id = data["id"]

            # 2. Fetch tweets
            params = {
                "max_results": max(5, min(int(max_results), 100)),
                "tweet.fields": "created_at,public_metrics",
                "exclude": "retweets,replies",
                "start_time": start_time,
            }
            r2 = client.get(f"{_BASE}/users/{user_id}/tweets", params=params)
            if r2.status_code != 200:
                result["status"] = _status_for(r2.status_code)
                result["error_message"] = f"users/{{id}}/tweets HTTP {r2.status_code}: {r2.text[:300]}"
                return result

            body = r2.json()
            for t in body.get("data", []) or []:
                metrics = t.get("public_metrics", {}) or {}
                tid = t.get("id")
                result["posts"].append(
                    {
                        "id": tid,
                        "text": t.get("text", ""),
                        "created_at": t.get("created_at"),
                        "url": f"https://x.com/{handle}/status/{tid}",
                        "like_count": metrics.get("like_count", 0),
                        "retweet_count": metrics.get("retweet_count", 0),
                    }
                )
            return result

    except httpx.TimeoutException as e:
        result["status"] = "error"
        result["error_message"] = f"timeout: {e}"
        return result
    except httpx.HTTPError as e:
        result["status"] = "error"
        result["error_message"] = f"http error: {e}"
        return result
    except Exception as e:  # pragma: no cover - defensive
        result["status"] = "error"
        result["error_message"] = f"{type(e).__name__}: {e}"
        return result


if __name__ == "__main__":  # pragma: no cover
    import json

    handle = sys.argv[1] if len(sys.argv) > 1 else "karpathy"
    out = fetch_user_recent(handle)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
