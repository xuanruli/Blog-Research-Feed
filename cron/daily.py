"""Daily cron runner: mount the .env, create a session, stream it to completion."""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("cron.daily")

HARD_TIMEOUT_SECONDS = 30 * 60

# Keys forwarded into the container .env (ANTHROPIC_* deliberately excluded).
PASSTHROUGH_KEYS = (
    "FIRECRAWL_API_KEY",
    "X_BEARER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "OPENAI_API_KEY",
)

CONTAINER_ENV_PATH = "/workspace/.env"
FILES_BETAS = ["managed-agents-2026-04-01", "files-api-2025-04-14"]

MEMORY_STORE_NAME = os.environ.get("MEMORY_STORE_NAME", "Resource_Insight")
MEMORY_STORE_INSTRUCTIONS = (
    "Persistent source-quality memory. Each entry records whether a given "
    "source (RSS feed, author, X handle, podcast, YouTube channel) tends to "
    "produce high-signal items or low-value noise. READ this before triaging "
    "today's /tmp/feed/index.json so you can prioritize known-good sources and "
    "deprioritize known-trash ones. After delivering the Slack report, UPDATE "
    "it with what today's run revealed about source quality (new good/trash "
    "sources, or corrections to prior judgments)."
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML_PATH = _PROJECT_ROOT / "agent" / "agent.yaml"
ENV_YAML_PATH = _PROJECT_ROOT / "agent" / "environment.yaml"


def _get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.environ.get(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Required environment variable not set: {key}")
    return value


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


class _Timeout(RuntimeError):
    pass


def _arm_timeout(seconds: int) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        raise _Timeout(f"cron runner exceeded {seconds}s hard cap")

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


def _build_env_payload(extra: Optional[dict[str, str]] = None) -> bytes:
    """Render PASSTHROUGH_KEYS (plus extras) as quoted KEY="value" lines."""
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

    lines = []
    for key, value in pairs:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _upload_env_file(client: Any, payload: bytes) -> Any:
    buf = io.BytesIO(payload)
    return client.beta.files.upload(
        file=(".env", buf, "text/plain"),
        betas=FILES_BETAS,
    )


def _try_delete_file(client: Any, file_id: str) -> None:
    try:
        client.beta.files.delete(file_id, betas=FILES_BETAS)
        LOG.info("deleted uploaded env file %s", file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("failed to delete env file %s: %s", file_id, exc)


def _read_yaml_name(path: Path) -> str:
    """Read the top-level ``name:`` field from a yaml file."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Expected {path} (this cron runner script runs from the repo root)."
        ) from exc
    name = (data or {}).get("name")
    if not name:
        raise RuntimeError(f"{path} is missing a top-level 'name:' field.")
    return name


def _find_active_by_name(items: Any, name: str) -> Any:
    """Pick the unique non-archived item named ``name``; raise on zero or many."""
    matches = [
        x
        for x in items
        if getattr(x, "name", None) == name and getattr(x, "archived_at", None) is None
    ]
    if not matches:
        raise RuntimeError(
            f"No active resource named {name!r}. Run `python scripts/create_agent.py` to provision."
        )
    if len(matches) > 1:
        ids = ", ".join(getattr(m, "id", "?") for m in matches)
        raise RuntimeError(
            f"Multiple active resources named {name!r} ({ids}). Archive the stale ones."
        )
    return matches[0]


def _resolve_agent_and_env(client: Any) -> tuple[str, str]:
    """Return (agent_id, env_id) by looking them up by name."""
    agent_name = _read_yaml_name(AGENT_YAML_PATH)
    env_name = _read_yaml_name(ENV_YAML_PATH)
    LOG.info("resolving agent=%r env=%r by name", agent_name, env_name)

    agent = _find_active_by_name(client.beta.agents.list(), agent_name)
    env = _find_active_by_name(client.beta.environments.list(), env_name)

    LOG.info("resolved agent.id=%s env.id=%s", agent.id, env.id)
    return agent.id, env.id


def _resolve_memory_store_id(client: Any) -> Optional[str]:
    try:
        match = _find_active_by_name(client.beta.memory_stores.list(), MEMORY_STORE_NAME)
    except RuntimeError as exc:
        LOG.warning(
            "memory store %r unavailable (%s) — running without memory", MEMORY_STORE_NAME, exc
        )
        return None
    LOG.info("resolved memory_store.id=%s", match.id)
    return match.id


def run(dry_run: bool = False) -> int:
    _setup_logging()
    today = _dt.date.today().isoformat()
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    _get_env("ANTHROPIC_API_KEY", required=not dry_run)

    preuploaded_file_id = os.environ.get("ENV_FILE_ID") or None
    if preuploaded_file_id:
        LOG.info("using pre-uploaded env file: %s", preuploaded_file_id)
        env_payload = b""
    else:
        env_payload = _build_env_payload()
        LOG.info("env payload: %d bytes (will upload per-run)", len(env_payload))

    if dry_run:
        plan = {
            "agent_name": _read_yaml_name(AGENT_YAML_PATH),
            "env_name": _read_yaml_name(ENV_YAML_PATH),
            "today": today,
            "yesterday": yesterday,
            "env_source": (
                {"mode": "preuploaded", "file_id": preuploaded_file_id}
                if preuploaded_file_id
                else {
                    "mode": "build_and_upload",
                    "env_keys": [k for k in PASSTHROUGH_KEYS if os.environ.get(k)],
                }
            ),
            "mount_path": CONTAINER_ENV_PATH,
            "memory_store": {"name": MEMORY_STORE_NAME, "access": "read_write"},
        }
        LOG.info("--dry-run plan: %s", json.dumps(plan, default=str))
        print(json.dumps(plan, indent=2, default=str))
        return 0

    from anthropic import Anthropic

    client = Anthropic()
    agent_id, env_id = _resolve_agent_and_env(client)

    if preuploaded_file_id:
        file_id = preuploaded_file_id
        delete_after = False
    else:
        LOG.info("uploading env payload to Files API")
        uploaded = _upload_env_file(client, env_payload)
        LOG.info("uploaded file id=%s", uploaded.id)
        file_id = uploaded.id
        delete_after = True

    try:
        resources: list[dict[str, Any]] = [
            {"type": "file", "file_id": file_id, "mount_path": CONTAINER_ENV_PATH}
        ]
        memory_store_id = _resolve_memory_store_id(client)
        if memory_store_id:
            resources.append(
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store_id,
                    "access": "read_write",
                    "instructions": MEMORY_STORE_INSTRUCTIONS,
                }
            )

        LOG.info("creating session agent=%s env=%s", agent_id, env_id)
        session = client.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
            title=f"Daily aggregation {today}",
            resources=resources,
        )
        LOG.info(
            "session id=%s status=%s",
            session.id,
            getattr(session, "status", "?"),
        )

        kickoff_text = (
            f"今天 (UTC) 是 {today}。请处理 {yesterday} 的内容：\n"
            f"YESTERDAY={yesterday}\n"
            f"按 system prompt 的 pipeline 执行：先 `brf fetch-all --since {yesterday}`，"
            f"用 jq triage `/tmp/feed/index.json`，按需 `brf fetch-full --id <id>` "
            f"drill-down，最后 `brf report slack --message-file <path>`。\n"
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
                _drain(stream, client=client, session_id=session.id)
        except _Timeout as exc:
            LOG.error("hard timeout: %s", exc)
            delete_after = False  # keep the .env file for debugging
        finally:
            _disarm_timeout()
    except Exception:
        delete_after = False
        raise
    finally:
        if delete_after:
            _try_delete_file(client, file_id)
    return 0


# Fire-and-forget agents whose threads are archived on first idle (reviewer is not).
_AUTO_ARCHIVE_AGENTS: frozenset[str] = frozenset(
    {
        "blog-research-feed-reader",
    }
)


def _archive_thread_safe(client: Any, session_id: str, thread_id: str, agent_name: str) -> None:
    """Archive a thread, swallowing errors — never abort the run for cleanup."""
    try:
        client.beta.sessions.threads.archive(thread_id, session_id=session_id)
        LOG.info("archived %s thread %s", agent_name, thread_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "failed to archive %s thread %s: %s — slot stays occupied",
            agent_name,
            thread_id,
            exc,
        )


def _drain(stream: Any, client: Any, session_id: str) -> None:
    """Consume the SSE stream, archiving reader threads as they go idle, until the session ends."""
    has_seen_running = False
    reader_threads: set[str] = set()
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
            text = "".join(
                getattr(b, "text", "") or "" for b in (getattr(event, "content", []) or [])
            )
            LOG.debug("agent.thinking: %s", _truncate(text, 200))
        elif etype == "session.thread_created":
            agent_name = getattr(event, "agent_name", "?")
            thread_id = getattr(event, "session_thread_id", "?")
            LOG.info("thread_created agent=%s id=%s", agent_name, thread_id)
            if thread_id and agent_name in _AUTO_ARCHIVE_AGENTS:
                reader_threads.add(thread_id)
        elif etype == "session.thread_status_running":
            LOG.info(
                "thread_running id=%s",
                getattr(event, "session_thread_id", "?"),
            )
        elif etype == "session.thread_status_idle":
            thread_id = getattr(event, "session_thread_id", "?")
            LOG.info(
                "thread_idle id=%s stop_reason=%s",
                thread_id,
                getattr(getattr(event, "stop_reason", None), "type", None),
            )
            if thread_id in reader_threads:
                reader_threads.discard(thread_id)
                _archive_thread_safe(client, session_id, thread_id, "blog-research-feed-reader")
        elif etype == "session.thread_status_terminated":
            LOG.warning(
                "thread_terminated id=%s",
                getattr(event, "session_thread_id", "?"),
            )
        elif etype == "agent.thread_message_sent":
            LOG.info(
                "thread_msg_sent to=%s thread=%s",
                getattr(event, "to_agent_name", "?"),
                getattr(event, "to_session_thread_id", "?"),
            )
        elif etype == "agent.thread_message_received":
            from_agent = getattr(event, "from_agent_name", "?")
            from_thread = getattr(event, "from_session_thread_id", None)
            LOG.info("thread_msg_received from=%s thread=%s", from_agent, from_thread)
            # Don't archive here: the thread is often still running; archive on its idle event.
        elif etype == "session.status_running":
            has_seen_running = True
            LOG.info("status_running")
        elif etype == "session.status_idle":
            stop = getattr(event, "stop_reason", None)
            stop_type = getattr(stop, "type", None) if stop else None
            LOG.info(
                "status_idle stop_reason=%s has_seen_running=%s",
                stop_type,
                has_seen_running,
            )
            if not has_seen_running:
                continue
            if stop_type == "requires_action":
                LOG.warning("unexpected requires_action without custom tools; continuing")
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cron.daily",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + log the session-create plan; no API calls.",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
