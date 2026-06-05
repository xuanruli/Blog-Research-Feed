"""Tests for SessionDrain: the session event-stream drain."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from session.drain import SessionDrain

READERS = frozenset({"blog-research-feed-reader"})


class _FakeThreads:
    def __init__(self) -> None:
        self.archived: list[tuple[str, str]] = []

    def archive(self, thread_id: str, session_id: str | None = None) -> None:
        self.archived.append((thread_id, session_id or ""))


class _FakeClient:
    def __init__(self) -> None:
        self.beta = SimpleNamespace(sessions=SimpleNamespace(threads=_FakeThreads()))


def _ev(type_: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **fields)


def _idle(stop: str) -> SimpleNamespace:
    return SimpleNamespace(type="session.status_idle", stop_reason=SimpleNamespace(type=stop))


def _consume_tracking(events: list[Any], client: Any) -> tuple[list[Any], str]:
    """Run SessionRun.consume over a generator that records which events were pulled."""
    consumed: list[Any] = []

    def gen():
        for e in events:
            consumed.append(e)
            yield e

    reply = SessionDrain(client, "sess", READERS).consume(gen())
    return consumed, reply


def test_stops_on_idle_after_running():
    extra = _ev("agent.message", content=[])
    consumed, _ = _consume_tracking(
        [_ev("session.status_running"), _idle("end_turn"), extra], _FakeClient()
    )
    assert extra not in consumed


def test_idle_before_running_does_not_stop():
    extra = _ev("agent.message", content=[])
    consumed, _ = _consume_tracking(
        [_idle("end_turn"), _ev("session.status_running"), _idle("end_turn"), extra],
        _FakeClient(),
    )
    # The first idle (before any running) must not stop the loop; it stops on the second.
    assert len(consumed) == 3
    assert extra not in consumed


def test_requires_action_does_not_stop():
    extra = _ev("agent.message", content=[])
    consumed, _ = _consume_tracking(
        [_ev("session.status_running"), _idle("requires_action"), extra], _FakeClient()
    )
    assert extra in consumed


def test_reader_thread_archived_on_idle():
    client = _FakeClient()
    _consume_tracking(
        [
            _ev(
                "session.thread_created",
                agent_name="blog-research-feed-reader",
                session_thread_id="t1",
            ),
            _ev("session.thread_status_idle", session_thread_id="t1", stop_reason=None),
            _ev("session.status_running"),
            _idle("end_turn"),
        ],
        client,
    )
    assert client.beta.sessions.threads.archived == [("t1", "sess")]


def test_reviewer_thread_not_archived():
    client = _FakeClient()
    _consume_tracking(
        [
            _ev(
                "session.thread_created",
                agent_name="blog-research-feed-reviewer",
                session_thread_id="t2",
            ),
            _ev("session.thread_status_idle", session_thread_id="t2", stop_reason=None),
            _ev("session.status_running"),
            _idle("end_turn"),
        ],
        client,
    )
    assert client.beta.sessions.threads.archived == []


def test_collects_reply_text():
    _, reply = _consume_tracking(
        [
            _ev("session.status_running"),
            _ev("agent.message", content=[SimpleNamespace(type="text", text="hello")]),
            _ev("agent.message", content=[SimpleNamespace(type="text", text="world")]),
            _idle("end_turn"),
        ],
        _FakeClient(),
    )
    assert reply == "hello\nworld"
