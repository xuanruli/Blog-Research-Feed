"""Consume a session's SSE event stream to completion: collect the reply, archive reader threads, stop at idle."""

from __future__ import annotations

import logging
from typing import Any, Callable

LOG = logging.getLogger("session.drain")


def _truncate(text: str, limit: int = 500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


class SessionRun:
    """One drain of a session's event stream.

    Collects the coordinator's reply text, auto-archives fire-and-forget reader threads as they
    go idle (to free the 25-thread budget), and stops when the session reaches a terminal state.
    """

    def __init__(
        self, client: Any, session_id: str, auto_archive_agents: frozenset[str] = frozenset()
    ):
        self._client = client
        self._session_id = session_id
        self._auto_archive = auto_archive_agents
        self._has_seen_running = False
        self._reader_threads: set[str] = set()
        self._reply: list[str] = []
        self._dispatch: dict[str, Callable[[Any], bool]] = {
            "agent.message": self._on_agent_message,
            "agent.tool_use": self._on_tool_use,
            "agent.tool_result": self._on_tool_result,
            "session.thread_created": self._on_thread_created,
            "session.thread_status_idle": self._on_thread_idle,
            "session.thread_status_terminated": self._on_thread_terminated,
            "agent.thread_message_received": self._on_thread_message,
            "session.status_running": self._on_status_running,
            "session.status_idle": self._on_status_idle,
            "session.status_terminated": self._on_status_terminated,
            "session.error": self._on_error,
        }

    def consume(self, stream: Any) -> str:
        """Block on the stream until terminal; return the coordinator's collected reply text."""
        for event in stream:
            handler = self._dispatch.get(getattr(event, "type", None))
            if handler is not None and handler(event):
                break
        return "\n".join(self._reply).strip()

    # Handlers return True to stop the drain, False to keep consuming.

    def _on_agent_message(self, event: Any) -> bool:
        for block in getattr(event, "content", []) or []:
            if getattr(block, "type", None) == "text":
                self._reply.append(block.text)
                LOG.info("agent.message: %s", _truncate(block.text))
        return False

    def _on_tool_use(self, event: Any) -> bool:
        LOG.info("agent.tool_use name=%s", getattr(event, "name", "?"))
        return False

    def _on_tool_result(self, event: Any) -> bool:
        LOG.info("agent.tool_result is_error=%s", getattr(event, "is_error", False))
        return False

    def _on_thread_created(self, event: Any) -> bool:
        agent_name = getattr(event, "agent_name", "?")
        thread_id = getattr(event, "session_thread_id", "?")
        LOG.info("thread_created agent=%s id=%s", agent_name, thread_id)
        if thread_id and agent_name in self._auto_archive:
            self._reader_threads.add(thread_id)
        return False

    def _on_thread_idle(self, event: Any) -> bool:
        thread_id = getattr(event, "session_thread_id", "?")
        LOG.info("thread_idle id=%s", thread_id)
        if thread_id in self._reader_threads:
            self._reader_threads.discard(thread_id)
            self._archive_thread(thread_id)
        return False

    def _on_thread_terminated(self, event: Any) -> bool:
        LOG.warning("thread_terminated id=%s", getattr(event, "session_thread_id", "?"))
        return False

    def _on_thread_message(self, event: Any) -> bool:
        LOG.info("thread_msg_received from=%s", getattr(event, "from_agent_name", "?"))
        return False

    def _on_status_running(self, event: Any) -> bool:
        self._has_seen_running = True
        LOG.info("status_running")
        return False

    def _on_status_idle(self, event: Any) -> bool:
        stop = getattr(event, "stop_reason", None)
        stop_type = getattr(stop, "type", None) if stop else None
        LOG.info("status_idle stop_reason=%s seen_running=%s", stop_type, self._has_seen_running)
        if not self._has_seen_running:
            return False
        if stop_type == "requires_action":
            LOG.warning("unexpected requires_action without custom tools; continuing")
            return False
        return True

    def _on_status_terminated(self, event: Any) -> bool:
        LOG.warning("session.status_terminated")
        return True

    def _on_error(self, event: Any) -> bool:
        err = getattr(event, "error", None)
        LOG.error("session.error: %s", getattr(err, "message", "unknown") if err else "unknown")
        return True

    def _archive_thread(self, thread_id: str) -> None:
        try:
            self._client.beta.sessions.threads.archive(thread_id, session_id=self._session_id)
            LOG.info("archived reader thread %s", thread_id)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("failed to archive thread %s: %s — slot stays occupied", thread_id, exc)
