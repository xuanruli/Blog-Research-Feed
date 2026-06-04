"""Loader for ``feeds.yaml`` — the single source of truth for all sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Required top-level keys (values may be empty).
_REQUIRED_KEYS: tuple[str, ...] = (
    "rss",
    "x",
    "youtube",
    "podcasts",
    "firecrawl_index",
)


def _default_yaml_path() -> Path:
    """Resolve the bundled ``feeds.yaml`` via importlib.resources."""
    from importlib.resources import files

    return Path(str(files("brf.sources") / "feeds.yaml"))


def load_sources(path: Path | None = None) -> dict[str, Any]:
    """Parse and validate ``feeds.yaml``; raises ValueError if a required key is missing."""
    yaml_path = path or _default_yaml_path()
    with open(yaml_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if not isinstance(cfg, dict):
        raise ValueError(f"{yaml_path}: top-level YAML must be a mapping, got {type(cfg).__name__}")

    missing = [k for k in _REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ValueError(f"{yaml_path}: missing required top-level keys: {', '.join(missing)}")
    return cfg


def active_rss_feeds(cfg: dict) -> list[dict]:
    """Return RSS feed entries where ``enabled`` is not explicitly False."""
    return [f for f in (cfg.get("rss") or []) if f.get("enabled", True) is not False]


def active_podcast_feeds(cfg: dict) -> list[dict]:
    """Return podcast feed entries where ``enabled`` is not explicitly False."""
    return [f for f in (cfg.get("podcasts") or []) if f.get("enabled", True) is not False]
