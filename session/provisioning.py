"""Resolve pre-provisioned Managed Agents resources (agent, environment, memory store) by name."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("session.provisioning")


def read_yaml_name(path: Path) -> str:
    """Read the top-level ``name:`` field from a yaml file."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Expected {path} (run from the repo root).") from exc
    name = (data or {}).get("name")
    if not name:
        raise RuntimeError(f"{path} is missing a top-level 'name:' field.")
    return name


def find_active_by_name(items: Any, name: str) -> Any:
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


def resolve_agent_and_env(client: Any, agent_yaml: Path, env_yaml: Path) -> tuple[str, str]:
    """Return (agent_id, env_id) by looking up the names declared in the yaml files."""
    agent_name = read_yaml_name(agent_yaml)
    env_name = read_yaml_name(env_yaml)
    LOG.info("resolving agent=%r env=%r by name", agent_name, env_name)
    agent = find_active_by_name(client.beta.agents.list(), agent_name)
    env = find_active_by_name(client.beta.environments.list(), env_name)
    LOG.info("resolved agent.id=%s env.id=%s", agent.id, env.id)
    return agent.id, env.id


def resolve_memory_store_id(client: Any, name: str) -> Optional[str]:
    """Return the memory store id for ``name``, or None (non-fatal) when it can't be uniquely resolved."""
    try:
        match = find_active_by_name(client.beta.memory_stores.list(), name)
    except RuntimeError as exc:
        LOG.warning("memory store %r unavailable (%s) — running without memory", name, exc)
        return None
    LOG.info("resolved memory_store.id=%s", match.id)
    return match.id
