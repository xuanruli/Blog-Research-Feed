"""I/O helpers — stdout is the wire format between this CLI and the cron host."""

from __future__ import annotations

import json
import sys
from typing import Any

import click


def emit_json(obj: Any) -> None:
    """Serialize ``obj`` as JSON to stdout and exit 0."""
    click.echo(json.dumps(obj, ensure_ascii=False, default=str))
    sys.exit(0)
