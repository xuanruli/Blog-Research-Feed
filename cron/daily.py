"""GitHub Actions entry for the daily run: dry-run planning, a hard timeout, then run the pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from typing import Optional

from cron.pipeline import (
    AGENT_YAML_PATH,
    CONTAINER_ENV_PATH,
    ENV_YAML_PATH,
    MEMORY_STORE_NAME,
    PASSTHROUGH_KEYS,
    run_daily_session,
)
from session.provisioning import read_yaml_name

LOG = logging.getLogger("cron.daily")

HARD_TIMEOUT_SECONDS = 30 * 60


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


def _dry_run_plan() -> dict:
    return {
        "agent_name": read_yaml_name(AGENT_YAML_PATH),
        "env_name": read_yaml_name(ENV_YAML_PATH),
        "env_source": (
            {"mode": "preuploaded", "file_id": os.environ["ENV_FILE_ID"]}
            if os.environ.get("ENV_FILE_ID")
            else {
                "mode": "build_and_upload",
                "env_keys": [k for k in PASSTHROUGH_KEYS if os.environ.get(k)],
            }
        ),
        "mount_path": CONTAINER_ENV_PATH,
        "memory_store": {"name": MEMORY_STORE_NAME, "access": "read_write"},
    }


def run(dry_run: bool = False) -> int:
    _setup_logging()
    _get_env("ANTHROPIC_API_KEY", required=not dry_run)

    if dry_run:
        plan = _dry_run_plan()
        LOG.info("--dry-run plan: %s", json.dumps(plan, default=str))
        print(json.dumps(plan, indent=2, default=str))
        return 0

    from anthropic import Anthropic

    _arm_timeout(HARD_TIMEOUT_SECONDS)
    try:
        run_daily_session(Anthropic())
    except _Timeout as exc:
        LOG.error("hard timeout: %s", exc)
    finally:
        _disarm_timeout()
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
