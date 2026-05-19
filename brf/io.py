"""I/O helpers — stdout is the wire format between this CLI and the cron host."""
from __future__ import annotations

import json
import sys
from typing import Any

import click


def emit_json(obj: Any) -> None:
    """Serialize `obj` as JSON to stdout and exit cleanly.

    Used by every subcommand so the cron host can parse stdout uniformly when
    forwarding `user.custom_tool_result` back to the Managed Agent.
    """
    click.echo(json.dumps(obj, ensure_ascii=False, default=str))
    sys.exit(0)
