"""Daily cron runner entry point.

Per run:

1. Resolve the .env file to mount.
   - Preferred: ``ENV_FILE_ID`` env var points at a pre-uploaded Files API
     object (see ``scripts/upload_env.py``). Nothing is uploaded or deleted.
   - Fallback: build a ``.env`` payload from host environment
     (PASSTHROUGH_KEYS), upload via Files API for this run, best-effort
     delete on clean exit. Kept for local dev / one-off runs.
2. Create a session referencing the pre-created agent + environment that
   mounts the .env at ``/workspace/.env``.
3. Stream events for logs/visibility; exit when the session goes idle with a
   terminal stop_reason (after seeing at least one running transition) or
   terminates.

Run via:

    python -m cron.daily            # real run
    python -m cron.daily --dry-run  # planning only, no API calls

The agent itself drives all the work via the pre-installed ``brf`` CLI in
bash inside its session container.
"""
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

# Keys forwarded into the container .env. Anything else stays host-side.
# ANTHROPIC_* is intentionally NOT included — the agent shouldn't call back
# into the API from inside its own session.
PASSTHROUGH_KEYS = (
    "FIRECRAWL_API_KEY",
    "X_BEARER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "OPENAI_API_KEY",
)

CONTAINER_ENV_PATH = "/workspace/.env"
# The SDK auto-sets files-api-2025-04-14 on client.beta.files.*; we also need
# managed-agents-2026-04-01 because we're using files in a Managed Agents
# context (per docs/managed_agents/files.md).
FILES_BETAS = ["managed-agents-2026-04-01", "files-api-2025-04-14"]

# Path to the project root (where agent/*.yaml lives) — repo root.
# cron/daily.py → project root is parent of parent.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML_PATH = _PROJECT_ROOT / "agent" / "agent.yaml"
ENV_YAML_PATH = _PROJECT_ROOT / "agent" / "environment.yaml"


# ---------------------------------------------------------------------------
# Env loading (host-side; no dep on brf)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Hard timeout
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# .env payload
# ---------------------------------------------------------------------------
def _build_env_payload(extra: Optional[dict[str, str]] = None) -> bytes:
    """Render PASSTHROUGH_KEYS (plus optional extras) as KEY=value lines.

    Values are quoted with double-quotes; embedded backslashes/quotes are
    escaped. Missing keys are skipped (the container side fails loud at use
    time).
    """
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


# ---------------------------------------------------------------------------
# Resolve agent + environment by name (look up at runtime instead of
# requiring users to store the auto-generated IDs as secrets).
# ---------------------------------------------------------------------------
def _read_yaml_name(path: Path) -> str:
    """Read the top-level ``name:`` field from a yaml file."""
    import yaml  # local import — kept off the dry-run-only hot path

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
    """Pick the unique non-archived item matching ``name`` from an SDK list page.

    Raises if zero matches (caller should provision) or more than one
    (caller has a stale duplicate to clean up).
    """
    matches = [
        x
        for x in items
        if getattr(x, "name", None) == name
        and getattr(x, "archived_at", None) is None
    ]
    if not matches:
        raise RuntimeError(
            f"No active resource named {name!r}. "
            "Run `python scripts/create_agent.py` to provision."
        )
    if len(matches) > 1:
        ids = ", ".join(getattr(m, "id", "?") for m in matches)
        raise RuntimeError(
            f"Multiple active resources named {name!r} ({ids}). "
            "Archive the stale ones."
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
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
        # Read names but don't hit the API.
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
        LOG.info("creating session agent=%s env=%s", agent_id, env_id)
        session = client.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
            title=f"Daily aggregation {today}",
            resources=[
                {
                    "type": "file",
                    "file_id": file_id,
                    "mount_path": CONTAINER_ENV_PATH,
                }
            ],
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
            _try_delete_file(client, file_id)
    return 0


def _drain(stream: Any) -> None:
    """Consume the SSE stream; break on terminal idle/terminated.

    Sessions start in ``idle`` (per docs/managed_agents/sessions.md §417), so
    we must NOT break on the first idle — only after we've seen at least one
    ``status_running`` transition, which proves the agent actually started
    working on our kickoff.
    """
    has_seen_running = False
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
                getattr(b, "text", "") or ""
                for b in (getattr(event, "content", []) or [])
            )
            LOG.debug("agent.thinking: %s", _truncate(text, 200))
        elif etype == "session.status_running":
            has_seen_running = True
            LOG.info("status_running")
        elif etype == "session.status_idle":
            stop = getattr(event, "stop_reason", None)
            stop_type = getattr(stop, "type", None) if stop else None
            LOG.info(
                "status_idle stop_reason=%s has_seen_running=%s",
                stop_type, has_seen_running,
            )
            if not has_seen_running:
                continue
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
