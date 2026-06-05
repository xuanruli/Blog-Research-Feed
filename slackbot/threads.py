"""Persistent map from a Slack thread to the Managed Agents session that owns its context."""

from __future__ import annotations

from typing import Optional, Protocol


class _Store(Protocol):
    def get(self, key: str) -> Optional[str]: ...

    def __setitem__(self, key: str, value: str) -> None: ...


class ThreadSessionMap:
    """Remember which session backs each Slack thread, so follow-ups reuse the daily session's context."""

    def __init__(self, store: _Store):
        self._store = store

    def remember(self, thread_ts: str, session_id: str) -> None:
        self._store[thread_ts] = session_id

    def lookup(self, thread_ts: str) -> Optional[str]:
        return self._store.get(thread_ts)
