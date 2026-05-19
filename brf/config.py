"""Environment/config loading for the brf CLI.

Auto-loads .env from (in order, first hit wins):
1. ``BRF_ENV_FILE`` env var (explicit override)
2. ``/workspace/.env`` — container path (mounted by daily.py via Files API)
3. ``./.env`` — current working directory (local dev)
4. walking up from this file's directory (running from inside the repo)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_CONTAINER_PATH = Path("/workspace/.env")


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("BRF_ENV_FILE")
    if explicit:
        paths.append(Path(explicit))
    paths.append(_CONTAINER_PATH)
    paths.append(Path.cwd() / ".env")
    # Walk up from this file looking for .env (handy when running from src)
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
