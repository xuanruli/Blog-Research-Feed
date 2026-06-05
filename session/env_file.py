"""Build and upload the .env file mounted into a session's container."""

from __future__ import annotations

import io
import logging
import os
from typing import Any, Optional

LOG = logging.getLogger("session.env_file")

FILES_BETAS = ["managed-agents-2026-04-01", "files-api-2025-04-14"]


def build_env_payload(keys: tuple[str, ...], extra: Optional[dict[str, str]] = None) -> bytes:
    """Render ``keys`` (read from os.environ) plus ``extra`` as quoted KEY="value" lines."""
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for key in keys:
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


def upload_env_file(client: Any, payload: bytes) -> Any:
    """Upload ``payload`` as a .env file via the Files API and return the file object."""
    return client.beta.files.upload(
        file=(".env", io.BytesIO(payload), "text/plain"), betas=FILES_BETAS
    )


def try_delete_file(client: Any, file_id: str) -> None:
    """Delete an uploaded file, swallowing errors — cleanup must never abort the run."""
    try:
        client.beta.files.delete(file_id, betas=FILES_BETAS)
        LOG.info("deleted uploaded env file %s", file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("failed to delete env file %s: %s", file_id, exc)
