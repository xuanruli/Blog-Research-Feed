"""Environment/config loading for the brf CLI."""
from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

# Load .env once at import time. Safe no-op if missing.
load_dotenv()


def get_env(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Return env var `key`, or `default`. If `required`, raise RuntimeError when missing."""
    value = os.environ.get(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Required environment variable not set: {key}")
    return value
