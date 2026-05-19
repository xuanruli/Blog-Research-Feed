#!/usr/bin/env python3
"""One-shot provisioning script for the Blog-Research-Feed managed agent.

Source of truth for the agent config is `agent/agent.yaml` plus
`agent/system_prompt.md` (referenced by the yaml).
`agent/environment.yaml` (if present) drives environment config.

Usage:
    python scripts/create_agent.py            # create new
    python scripts/create_agent.py --update   # update existing by name
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML_PATH = REPO_ROOT / "agent" / "agent.yaml"
ENV_YAML_PATH = REPO_ROOT / "agent" / "environment.yaml"
SYSTEM_PROMPT_PATH = REPO_ROOT / "agent" / "system_prompt.md"

ENV_NAME_DEFAULT = "blog-research-feed-env"
ENV_CONFIG_DEFAULT: dict[str, Any] = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
}


def _resolve_system(value: Any) -> str:
    """Inline a `@./path` ref, or return the literal string."""
    if isinstance(value, str) and value.startswith("@"):
        rel = value[1:]
        # Strip a leading "./" so it resolves from repo root.
        if rel.startswith("./"):
            rel = rel[2:]
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    if value is None:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return str(value)


def load_agent_config() -> dict[str, Any]:
    raw = yaml.safe_load(AGENT_YAML_PATH.read_text(encoding="utf-8"))
    model_field = raw.get("model")
    if isinstance(model_field, dict):
        model = model_field.get("id")
    else:
        model = model_field
    return {
        "name": raw["name"],
        "model": model,
        "system": _resolve_system(raw.get("system")),
        "tools": raw.get("tools", []),
        "description": raw.get("description"),
    }


def load_env_config() -> tuple[str, dict[str, Any]]:
    if not ENV_YAML_PATH.exists():
        return ENV_NAME_DEFAULT, ENV_CONFIG_DEFAULT
    raw = yaml.safe_load(ENV_YAML_PATH.read_text(encoding="utf-8")) or {}
    name = raw.get("name", ENV_NAME_DEFAULT)
    config = raw.get("config") or {
        k: v for k, v in raw.items() if k != "name"
    } or ENV_CONFIG_DEFAULT
    return name, config


def find_by_name(items, name: str):
    for it in items:
        if getattr(it, "name", None) == name and getattr(it, "archived_at", None) is None:
            return it
    return None


def ensure_environment(client: Anthropic, name: str, config: dict[str, Any], update: bool):
    existing = find_by_name(list(client.beta.environments.list()), name)
    if existing and not update:
        print(f"# Environment '{name}' already exists; reusing.", file=sys.stderr)
        return existing
    if existing and update:
        print(f"# Reusing existing environment '{name}' (envs are not updatable).", file=sys.stderr)
        return existing
    return client.beta.environments.create(name=name, config=config)


def ensure_agent(client: Anthropic, cfg: dict[str, Any], update: bool):
    name = cfg["name"]
    existing = find_by_name(list(client.beta.agents.list()), name)
    if existing and update:
        print(f"# Updating existing agent '{name}' (id={existing.id} v{existing.version}).", file=sys.stderr)
        return client.beta.agents.update(
            existing.id,
            version=existing.version,
            model=cfg["model"],
            system=cfg["system"],
            tools=cfg["tools"],
        )
    if existing and not update:
        print(
            f"# Agent '{name}' already exists (id={existing.id}). "
            f"Pass --update to push new config.",
            file=sys.stderr,
        )
        return existing
    return client.beta.agents.create(
        name=name,
        model=cfg["model"],
        system=cfg["system"],
        tools=cfg["tools"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update existing agent (by name) instead of creating a new one.",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    agent_cfg = load_agent_config()
    env_name, env_config = load_env_config()
    client = Anthropic()

    env = ensure_environment(client, env_name, env_config, update=args.update)
    agent = ensure_agent(client, agent_cfg, update=args.update)

    print(f"ANTHROPIC_AGENT_ID={agent.id}")
    print(f"ANTHROPIC_ENV_ID={env.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
