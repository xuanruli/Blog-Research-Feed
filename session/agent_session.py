"""A live Managed Agents session you can talk to: create one, then ask() and get the coordinator's reply."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .drain import SessionRun


def _user_message(text: str) -> dict:
    return {"type": "user.message", "content": [{"type": "text", "text": text}]}


@dataclass
class AgentSession:
    """Handle to a server-side Managed Agents session.

    Reuse the same instance (same ``id``) across turns to keep conversation context — that is what
    makes Slack follow-ups land in the same session that produced the daily report.
    """

    client: Any
    id: str
    auto_archive_agents: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def create(
        cls,
        client: Any,
        *,
        agent_id: str,
        environment_id: str,
        title: str,
        resources: list[dict],
        auto_archive_agents: frozenset[str] = frozenset(),
    ) -> "AgentSession":
        """Provision a session against a pre-created agent + environment and return a handle to it."""
        session = client.beta.sessions.create(
            agent=agent_id,
            environment_id=environment_id,
            title=title,
            resources=resources,
        )
        return cls(client=client, id=session.id, auto_archive_agents=auto_archive_agents)

    def ask(self, text: str) -> str:
        """Send one user message, drain to idle (stream-first), and return the coordinator's reply text."""
        run = SessionRun(self.client, self.id, self.auto_archive_agents)
        with self.client.beta.sessions.events.stream(self.id) as stream:
            self.client.beta.sessions.events.send(self.id, events=[_user_message(text)])
            return run.consume(stream)
