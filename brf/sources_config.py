"""Loader for ``brf/sources.yaml`` — the single source of truth for sources.

Replaces the per-type Python constants in ``brf/rss.py`` (``SKIP_FEEDS``,
``SUMMARY_ONLY_FEEDS``, ``FIRECRAWL_FALLBACK_FEEDS``) and consolidates the
inputs to the upcoming aggregator (Phase 1 of the brf fetcher refactor —
see ``BRF_FETCHER_DESIGN.md`` §5).

Public API:

* :func:`load_sources` — parse and validate the yaml, return the raw dict.
* :func:`active_rss_feeds` — RSS entries with ``enabled`` not False.
* :func:`active_podcast_feeds` — podcast entries with ``enabled`` not False.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Top-level keys every sources.yaml must declare (may be empty list/dict).
_REQUIRED_KEYS: tuple[str, ...] = (
    "rss",
    "x",
    "youtube",
    "podcasts",
    "firecrawl_index",
)


def _default_yaml_path() -> Path:
    """Resolve ``brf/sources.yaml`` inside the installed package.

    Uses :mod:`importlib.resources` so this works both in editable installs
    and in a zip-installed wheel.
    """
    from importlib.resources import files

    return Path(str(files("brf") / "sources.yaml"))


def load_sources(path: Path | None = None) -> dict[str, Any]:
    """Parse ``sources.yaml`` and return the dict.

    Validates that all top-level keys (``rss``, ``x``, ``youtube``,
    ``podcasts``, ``firecrawl_index``) are present. Empty lists/dicts are
    allowed so partial configs don't crash the loader during phased rollout.

    Raises:
        FileNotFoundError: if the yaml file is missing.
        ValueError: if the file is not a mapping or a required top-level
            key is absent.
    """
    yaml_path = path or _default_yaml_path()
    with open(yaml_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if not isinstance(cfg, dict):
        raise ValueError(
            f"{yaml_path}: top-level YAML must be a mapping, got {type(cfg).__name__}"
        )

    missing = [k for k in _REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ValueError(
            f"{yaml_path}: missing required top-level keys: {', '.join(missing)}"
        )
    return cfg


def active_rss_feeds(cfg: dict) -> list[dict]:
    """Return RSS feed entries where ``enabled`` is not explicitly False."""
    return [f for f in (cfg.get("rss") or []) if f.get("enabled", True) is not False]


def active_podcast_feeds(cfg: dict) -> list[dict]:
    """Return podcast feed entries where ``enabled`` is not explicitly False."""
    return [f for f in (cfg.get("podcasts") or []) if f.get("enabled", True) is not False]
