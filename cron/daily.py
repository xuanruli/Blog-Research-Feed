"""Daily cron runner: mount the .env, create a session, drive it to completion."""

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

from session import AgentSession, resolve_agent_and_env, resolve_memory_store_id
from session.provisioning import read_yaml_name

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
    "Read before triaging today's items to prioritize known-good sources; "
    "after sending the report, record new good/trash source judgments."
)

# Fire-and-forget agents whose threads are archived on first idle (reviewer is not).
AUTO_ARCHIVE_AGENTS = frozenset({"blog-research-feed-reader"})

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
    return client.beta.files.upload(file=(".env", buf, "text/plain"), betas=FILES_BETAS)


def _try_delete_file(client: Any, file_id: str) -> None:
    try:
        client.beta.files.delete(file_id, betas=FILES_BETAS)
        LOG.info("deleted uploaded env file %s", file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("failed to delete env file %s: %s", file_id, exc)


def _session_resources(client: Any, file_id: str) -> list[dict[str, Any]]:
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
            "agent_name": read_yaml_name(AGENT_YAML_PATH),
            "env_name": read_yaml_name(ENV_YAML_PATH),
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
    agent_id, env_id = resolve_agent_and_env(client, AGENT_YAML_PATH, ENV_YAML_PATH)

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
        session = AgentSession.create(
            client,
            agent_id=agent_id,
            environment_id=env_id,
            title=f"Daily aggregation {today}",
            resources=_session_resources(client, file_id),
            auto_archive_agents=AUTO_ARCHIVE_AGENTS,
        )
        LOG.info("session id=%s", session.id)

        kickoff_text = (
            f"今天 (UTC) 是 {today}，请按 system prompt 的 pipeline 处理 {yesterday} 的内容。\n"
            f"YESTERDAY={yesterday}"
        )

        _arm_timeout(HARD_TIMEOUT_SECONDS)
        try:
            session.ask(kickoff_text)
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
