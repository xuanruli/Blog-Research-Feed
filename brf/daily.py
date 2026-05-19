"""Daily orchestrator for the Blog-Research-Feed Managed Agent.

Per run:

1. Build a ``.env`` payload from the host's environment (only the keys the
   container needs), upload via Files API.
2. Create a session that references the pre-created agent + environment and
   mounts the uploaded .env at ``/workspace/.env``.
3. Stream events for logs/visibility; exit when the session goes idle with a
   terminal stop_reason or terminates.
4. Best-effort delete the uploaded .env file at the end (kept on failure for
   debugging).

The agent itself drives all the work via the pre-installed ``brf`` CLI in
bash. There is no custom-tool dispatch in this process — that pattern was
removed once the env-file mount made on-container secrets viable.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import os
import signal
import sys
import threading
from typing import Any, Optional

from .config import get_env

LOG = logging.getLogger("brf.daily")

HARD_TIMEOUT_SECONDS = 30 * 60

# Keys we forward into the container .env. Anything else stays host-side.
# ANTHROPIC_API_KEY is intentionally NOT included — the agent shouldn't
# call back into the API from inside its own session.
PASSTHROUGH_KEYS = (
    "FIRECRAWL_API_KEY",
    "X_BEARER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "OPENAI_API_KEY",
)

CONTAINER_ENV_PATH = "/workspace/.env"
FILES_BETA = "files-api-2025-04-14"


def _setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    logging.Formatter.converter = __import__("time").gmtime


# ---------------------------------------------------------------------------
# Hard timeout
# ---------------------------------------------------------------------------
class _Timeout(RuntimeError):
    pass


def _arm_timeout(seconds: int) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        raise _Timeout(f"orchestrator exceeded {seconds}s hard cap")

    if not hasattr(signal, "SIGALRM"):
        return  # Windows / non-POSIX
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _disarm_timeout() -> None:
    try:
        signal.alarm(0)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# .env payload
# ---------------------------------------------------------------------------
def _build_env_payload(extra: Optional[dict[str, str]] = None) -> bytes:
    """Render PASSTHROUGH_KEYS (plus optional extras) as KEY=value lines.

    Values are quoted with double-quotes; embedded double-quotes are escaped.
    Missing keys are skipped (the container side will fail loud at use time).
    """
    lines: list[str] = []
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for key in PASSTHROUGH_KEYS:
        value = os.environ.get(key)
        if value is None or value == "":
            LOG.warning("passthrough %s: MISSING (skipping)", key)
            continue
        pairs.append((key, value))
        seen.add(key)
        LOG.info("passthrough %s: present (%d chars)", key, len(value))
    for key, value in (extra or {}).items():
        if key in seen:
            continue
        pairs.append((key, value))

    for key, value in pairs:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _upload_env_file(client: Any, payload: bytes) -> Any:
    """Upload the .env payload via Files API; return the FileMetadata object."""
    buf = io.BytesIO(payload)
    return client.beta.files.upload(
        file=(".env", buf, "text/plain"),
        betas=[FILES_BETA],
    )


def _try_delete_file(client: Any, file_id: str) -> None:
    try:
        client.beta.files.delete(file_id, betas=[FILES_BETA])
        LOG.info("deleted uploaded env file %s", file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("failed to delete env file %s: %s", file_id, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(dry_run: bool = False) -> None:
    _setup_logging()
    today = _dt.date.today().isoformat()
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    agent_id = get_env("ANTHROPIC_AGENT_ID", required=not dry_run)
    env_id = get_env("ANTHROPIC_ENV_ID", required=not dry_run)
    get_env("ANTHROPIC_API_KEY", required=not dry_run)

    env_payload = _build_env_payload()
    LOG.info("env payload: %d bytes", len(env_payload))

    if dry_run:
        plan = {
            "agent": {"type": "agent", "id": agent_id},
            "environment_id": env_id,
            "today": today,
            "yesterday": yesterday,
            "env_keys": [
                k for k in PASSTHROUGH_KEYS if os.environ.get(k)
            ],
            "mount_path": CONTAINER_ENV_PATH,
        }
        LOG.info("--dry-run plan: %s", json.dumps(plan, default=str))
        print(json.dumps(plan, indent=2, default=str))
        return

    from anthropic import Anthropic

    client = Anthropic()

    LOG.info("uploading env payload to Files API")
    uploaded = _upload_env_file(client, env_payload)
    LOG.info("uploaded file id=%s", uploaded.id)

    session = None
    delete_after = True
    try:
        LOG.info("creating session agent=%s env=%s", agent_id, env_id)
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id},
            environment_id=env_id,
            title=f"Daily aggregation {today}",
            resources=[
                {
                    "type": "file",
                    "file_id": uploaded.id,
                    "mount_path": CONTAINER_ENV_PATH,
                }
            ],
        )
        LOG.info("session id=%s status=%s", session.id, getattr(session, "status", "?"))

        kickoff_text = (
            f"今天 (UTC) 是 {today}。请处理 {yesterday} 的内容：\n"
            f"YESTERDAY={yesterday}\n"
            f"按 system prompt 的 pipeline 执行：先 `brf fetch rss --since {yesterday}`，"
            f"triage，按需 deep-dive，最后 `brf report slack --message-file <path>`。\n"
            f"环境变量已经在 {CONTAINER_ENV_PATH}（`brf` 自动加载，不需要手动 source）。"
        )

        _arm_timeout(HARD_TIMEOUT_SECONDS)
        try:
            # Open stream BEFORE sending kickoff (stream-first ordering).
            with client.beta.sessions.events.stream(session.id) as stream:
                client.beta.sessions.events.send(
                    session.id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": kickoff_text}],
                        }
                    ],
                )
                LOG.info("kickoff sent")
                _drain(stream)
        except _Timeout as exc:
            LOG.error("hard timeout: %s", exc)
            delete_after = False  # keep .env file for forensic debug
        finally:
            _disarm_timeout()
    except Exception:
        delete_after = False
        raise
    finally:
        if delete_after:
            _try_delete_file(client, uploaded.id)


def _drain(stream: Any) -> None:
    """Consume the SSE stream, logging events; break on terminal idle/terminated."""
    for event in stream:
        etype = getattr(event, "type", None)
        if etype == "agent.message":
            for block in getattr(event, "content", []) or []:
                if getattr(block, "type", None) == "text":
                    LOG.info("agent.message: %s", _truncate(block.text))
        elif etype == "agent.tool_use":
            name = getattr(event, "name", "?")
            LOG.info("agent.tool_use name=%s", name)
        elif etype == "agent.tool_result":
            err = getattr(event, "is_error", False)
            LOG.info("agent.tool_result is_error=%s", err)
        elif etype == "agent.thinking":
            text = "".join(getattr(b, "text", "") or "" for b in (getattr(event, "content", []) or []))
            LOG.debug("agent.thinking: %s", _truncate(text, 200))
        elif etype == "session.status_running":
            LOG.info("status_running")
        elif etype == "session.status_idle":
            stop = getattr(event, "stop_reason", None)
            stop_type = getattr(stop, "type", None) if stop else None
            LOG.info("status_idle stop_reason=%s", stop_type)
            # Without custom tools, requires_action shouldn't happen — log
            # and continue if it does. All other stop reasons are terminal.
            if stop_type == "requires_action":
                LOG.warning(
                    "unexpected requires_action without custom tools; continuing"
                )
                continue
            break
        elif etype == "session.status_terminated":
            LOG.warning("session.status_terminated")
            break
        elif etype == "session.error":
            err = getattr(event, "error", None)
            msg = getattr(err, "message", None) if err else "unknown"
            LOG.error("session.error: %s", msg)
            break
        else:
            LOG.debug("event %s", etype)


def _truncate(text: str, limit: int = 500) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"
