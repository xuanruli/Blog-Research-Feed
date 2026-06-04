"""Load .env for the brf CLI, trying the container mount paths then the cwd."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Managed Agents mounts session files under /mnt/session/uploads/.
_MANAGED_AGENT_MOUNT = Path("/mnt/session/uploads/workspace/.env")
_CONTAINER_PATH = Path("/workspace/.env")


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("BRF_ENV_FILE")
    if explicit:
        paths.append(Path(explicit))
    paths.append(_MANAGED_AGENT_MOUNT)
    paths.append(_CONTAINER_PATH)
    paths.append(Path.cwd() / ".env")
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        paths.append(parent / ".env")
    return paths


def _autoload() -> Optional[Path]:
    for path in _candidate_paths():
        try:
            if path.is_file():
                load_dotenv(path, override=False)
                return path
        except OSError:
            continue
    return None


_LOADED_FROM = _autoload()


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Return env var ``key`` or ``default``. If ``required``, raise when missing."""
    value = os.environ.get(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Required environment variable not set: {key}")
    return value


def loaded_env_path() -> Optional[Path]:
    """Where (if anywhere) we loaded .env from. For diagnostics."""
    return _LOADED_FROM
