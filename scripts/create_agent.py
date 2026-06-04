#!/usr/bin/env python3
"""Provision the managed-agent roster (env, reader, reviewer, then coordinator) from agent/*.yaml."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
AGENT_YAML_PATH = AGENT_DIR / "agent.yaml"
ENV_YAML_PATH = AGENT_DIR / "environment.yaml"
SYSTEM_PROMPT_PATH = AGENT_DIR / "system_prompt.md"

# Subagents must be provisioned before the coordinator references them by id.
SUBAGENT_YAML_PATHS: list[Path] = [
    AGENT_DIR / "reader.yaml",
    AGENT_DIR / "reviewer.yaml",
]

ENV_NAME_DEFAULT = "blog-research-feed-env"
ENV_CONFIG_DEFAULT: dict[str, Any] = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
}


def _resolve_system(value: Any, fallback_path: Path) -> str:
    """Inline a `@./path` ref, or return the literal string."""
    if isinstance(value, str) and value.startswith("@"):
        rel = value[1:]
        if rel.startswith("./"):
            rel = rel[2:]
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    if value is None:
        return fallback_path.read_text(encoding="utf-8")
    return str(value)


def load_agent_yaml(path: Path) -> dict[str, Any]:
    """Parse one agent yaml into a flat dict consumable by the SDK."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    model_field = raw.get("model")
    model = model_field.get("id") if isinstance(model_field, dict) else model_field
    return {
        "name": raw["name"],
        "model": model,
        "system": _resolve_system(raw.get("system"), SYSTEM_PROMPT_PATH),
        "tools": raw.get("tools", []),
        "description": raw.get("description"),
        "multiagent_raw": raw.get("multiagent"),  # name refs; resolved later
        "skills_raw": raw.get("skills"),  # title refs; resolved later
    }


def load_env_config() -> tuple[str, dict[str, Any]]:
    if not ENV_YAML_PATH.exists():
        return ENV_NAME_DEFAULT, ENV_CONFIG_DEFAULT
    raw = yaml.safe_load(ENV_YAML_PATH.read_text(encoding="utf-8")) or {}
    name = raw.get("name", ENV_NAME_DEFAULT)
    config = (
        raw.get("config") or {k: v for k, v in raw.items() if k != "name"} or ENV_CONFIG_DEFAULT
    )
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


def _resolve_multiagent(
    raw: dict[str, Any] | None,
    name_to_id: dict[str, str],
) -> dict[str, Any] | None:
    """Resolve ``{type: agent, name}`` roster entries to ``{type: agent, id}``."""
    if not raw:
        return None
    out: dict[str, Any] = {"type": raw.get("type", "coordinator"), "agents": []}
    for entry in raw.get("agents") or []:
        if entry.get("type") != "agent":
            out["agents"].append(entry)
            continue
        if "id" in entry:
            out["agents"].append(entry)
            continue
        name = entry.get("name")
        if name is None:
            raise RuntimeError(f"multiagent entry has no `id` or `name`: {entry!r}")
        agent_id = name_to_id.get(name)
        if agent_id is None:
            raise RuntimeError(
                f"multiagent ref name={name!r} not found in provisioned "
                f"subagents {sorted(name_to_id)!r}"
            )
        resolved = {"type": "agent", "id": agent_id}
        if "version" in entry:
            resolved["version"] = entry["version"]
        out["agents"].append(resolved)
    return out


SKILLS_BETA = "skills-2025-10-02"


def _resolve_skills(
    raw: list[dict[str, Any]] | None,
    client: Anthropic,
) -> list[dict[str, Any]] | None:
    """Resolve ``{type: custom, title}`` skill entries to ``{type, skill_id, version}``."""
    if not raw:
        return None
    title_to_id: dict[str, str] = {}
    for s in client.beta.skills.list(betas=[SKILLS_BETA]):
        t = getattr(s, "display_title", None)
        if t:
            title_to_id[t] = s.id

    out: list[dict[str, Any]] = []
    for entry in raw:
        stype = entry.get("type", "custom")
        if stype == "anthropic" or "skill_id" in entry:
            out.append(entry)
            continue
        title = entry.get("title")
        if title is None:
            raise RuntimeError(f"skill entry has no `skill_id` or `title`: {entry!r}")
        skill_id = title_to_id.get(title)
        if skill_id is None:
            raise RuntimeError(
                f"skill title={title!r} not found in workspace "
                f"{sorted(title_to_id)!r}. Run scripts/upload_skill.py first."
            )
        out.append(
            {"type": "custom", "skill_id": skill_id, "version": entry.get("version", "latest")}
        )
    return out


def ensure_agent(
    client: Anthropic,
    cfg: dict[str, Any],
    update: bool,
    multiagent: dict[str, Any] | None = None,
    skills: list[dict[str, Any]] | None = None,
):
    name = cfg["name"]
    existing = find_by_name(list(client.beta.agents.list()), name)
    create_kwargs: dict[str, Any] = dict(
        name=name,
        model=cfg["model"],
        system=cfg["system"],
        tools=cfg["tools"],
    )
    if multiagent is not None:
        create_kwargs["multiagent"] = multiagent
    if skills is not None:
        create_kwargs["skills"] = skills
        create_kwargs["betas"] = ["managed-agents-2026-04-01", SKILLS_BETA]
    if existing and update:
        print(
            f"# Updating existing agent '{name}' (id={existing.id} v{existing.version}).",
            file=sys.stderr,
        )
        update_kwargs = {k: v for k, v in create_kwargs.items() if k != "name"}
        return client.beta.agents.update(existing.id, version=existing.version, **update_kwargs)
    if existing and not update:
        print(
            f"# Agent '{name}' already exists (id={existing.id}). "
            f"Pass --update to push new config.",
            file=sys.stderr,
        )
        return existing
    return client.beta.agents.create(**create_kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update existing agents (by name) instead of creating new ones.",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    env_name, env_config = load_env_config()
    coordinator_cfg = load_agent_yaml(AGENT_YAML_PATH)
    subagent_cfgs = [load_agent_yaml(p) for p in SUBAGENT_YAML_PATHS]

    client = Anthropic()

    env = ensure_environment(client, env_name, env_config, update=args.update)

    # Step 1: provision subagents first, collect their ids.
    name_to_id: dict[str, str] = {}
    for sub_cfg in subagent_cfgs:
        sub = ensure_agent(client, sub_cfg, update=args.update)
        name_to_id[sub_cfg["name"]] = sub.id
        print(f"# subagent {sub_cfg['name']} id={sub.id}", file=sys.stderr)

    # Step 2: resolve the coordinator's multiagent name-refs + skill title-refs, then provision.
    multiagent = _resolve_multiagent(coordinator_cfg["multiagent_raw"], name_to_id)
    skills = _resolve_skills(coordinator_cfg["skills_raw"], client)
    coordinator = ensure_agent(
        client, coordinator_cfg, update=args.update, multiagent=multiagent, skills=skills
    )

    print(f"ANTHROPIC_AGENT_ID={coordinator.id}")
    print(f"ANTHROPIC_ENV_ID={env.id}")
    for name, agent_id in name_to_id.items():
        print(f"ANTHROPIC_AGENT_ID_{name.upper().replace('-', '_')}={agent_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
