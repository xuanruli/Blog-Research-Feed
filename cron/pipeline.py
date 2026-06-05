"""The daily aggregation pipeline: provision a session, run the curator, return its report and session id."""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from pathlib import Path
from typing import Any

from session import AgentSession, resolve_agent_and_env, resolve_memory_store_id
from session.env_file import build_env_payload, try_delete_file, upload_env_file

LOG = logging.getLogger("cron.pipeline")

# Keys forwarded into the container .env (ANTHROPIC_* deliberately excluded).
PASSTHROUGH_KEYS = (
    "FIRECRAWL_API_KEY",
    "X_BEARER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "OPENAI_API_KEY",
)
CONTAINER_ENV_PATH = "/workspace/.env"

MEMORY_STORE_NAME = os.environ.get("MEMORY_STORE_NAME", "Resource_Insight")
MEMORY_STORE_INSTRUCTIONS = (
    "Read before triaging today's items to prioritize known-good sources; "
    "after sending the report, record new good/trash source judgments."
)

# Fire-and-forget agents whose threads are archived on first idle (reviewer is not).
AUTO_ARCHIVE_AGENTS = frozenset({"blog-research-feed-reader"})

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML_PATH = _PROJECT_ROOT / "agent" / "agent.yaml"
ENV_YAML_PATH = _PROJECT_ROOT / "agent" / "environment.yaml"


def daily_kickoff(today: str, yesterday: str) -> str:
    """The kickoff for one day's run — just the dates; the pipeline itself lives in the system prompt."""
    return (
        f"今天 (UTC) 是 {today}，请按 system prompt 的 pipeline 处理 {yesterday} 的内容。\n"
        f"YESTERDAY={yesterday}"
    )


_REPORT_RE = re.compile(r"<report>(.*?)</report>", re.DOTALL)


def _extract_report(text: str) -> str:
    """Pull the report out of the agent's final message; fall back to the whole text if unmarked."""
    match = _REPORT_RE.search(text or "")
    return (match.group(1) if match else text or "").strip()


def _build_session_resources(client: Any, file_id: str) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = [
        {"type": "file", "file_id": file_id, "mount_path": CONTAINER_ENV_PATH}
    ]
    memory_store_id = resolve_memory_store_id(client, MEMORY_STORE_NAME)
    if memory_store_id:
        resources.append(
            {
                "type": "memory_store",
                "memory_store_id": memory_store_id,
                "access": "read_write",
                "instructions": MEMORY_STORE_INSTRUCTIONS,
            }
        )
    return resources


def run_daily_session(
    client: Any,
    agent_yaml: Path = AGENT_YAML_PATH,
    env_yaml: Path = ENV_YAML_PATH,
) -> tuple[str, str]:
    """Provision today's session, run the curator to idle, and return (report_text, session_id)."""
    today = _dt.date.today().isoformat()
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    agent_id, env_id = resolve_agent_and_env(client, agent_yaml, env_yaml)

    preuploaded = os.environ.get("ENV_FILE_ID") or None
    if preuploaded:
        file_id, uploaded_here = preuploaded, False
    else:
        file_id = upload_env_file(client, build_env_payload(PASSTHROUGH_KEYS)).id
        uploaded_here = True
        LOG.info("uploaded env file id=%s", file_id)

    try:
        session = AgentSession.create(
            client,
            agent_id=agent_id,
            environment_id=env_id,
            title=f"Daily aggregation {today}",
            resources=_build_session_resources(client, file_id),
            auto_archive_agents=AUTO_ARCHIVE_AGENTS,
        )
        LOG.info("session id=%s", session.id)
        report = _extract_report(session.ask(daily_kickoff(today, yesterday)))
        return report, session.id
    finally:
        if uploaded_here:
            try_delete_file(client, file_id)
