"""Upload (or version-bump) the custom skills under ``skills/`` to the workspace.

Each ``skills/<dir>/`` becomes one custom skill. ``SKILL.md`` must sit at the
top of the bundle, so files are uploaded under the ``<dir>/`` prefix that the
Skills API expects ("SKILL.md file must be exactly in the top-level folder").

Idempotent by display_title (== the directory name):
  * new skill        -> ``skills.create``
  * existing skill   -> ``skills.versions.create`` (new version of same id)

Skill ids are workspace-scoped and stable; ``scripts/create_agent.py``
resolves them from display_title at provision time, so nothing here needs to
be persisted as config.

Usage:
    python -m scripts.upload_skill                 # upload all skills/*/
    python -m scripts.upload_skill brf-cli         # just one
"""
from __future__ import annotations

import sys
from pathlib import Path

from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SKILLS_BETA = "skills-2025-10-02"


def _bundle_files(skill_dir: Path) -> list[tuple[str, bytes, str]]:
    """All files under skill_dir, keyed by ``<dirname>/<relpath>``."""
    prefix = skill_dir.name
    files: list[tuple[str, bytes, str]] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix()
        mime = "text/markdown" if path.suffix == ".md" else "application/octet-stream"
        files.append((f"{prefix}/{rel}", path.read_bytes(), mime))
    return files


def _existing_by_title(client: Anthropic) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in client.beta.skills.list(betas=[SKILLS_BETA]):
        title = getattr(s, "display_title", None)
        if title:
            out[title] = s.id
    return out


def upload_one(client: Anthropic, skill_dir: Path, existing: dict[str, str]) -> str:
    title = skill_dir.name
    files = _bundle_files(skill_dir)
    if not any(f[0].endswith("/SKILL.md") for f in files):
        raise RuntimeError(f"{skill_dir}: no SKILL.md found")

    if title in existing:
        skill_id = existing[title]
        client.beta.skills.versions.create(
            skill_id, files=files, betas=[SKILLS_BETA]
        )
        print(f"# updated '{title}' -> {skill_id} (new version)", file=sys.stderr)
        return skill_id

    resp = client.beta.skills.create(
        display_title=title, files=files, betas=[SKILLS_BETA]
    )
    print(f"# created '{title}' -> {resp.id}", file=sys.stderr)
    return resp.id


def main(argv: list[str]) -> int:
    only = set(argv[1:])
    dirs = [
        d for d in sorted(SKILLS_DIR.iterdir())
        if d.is_dir() and (not only or d.name in only)
    ]
    if not dirs:
        print(f"no skill dirs under {SKILLS_DIR}", file=sys.stderr)
        return 1

    client = Anthropic()
    existing = _existing_by_title(client)
    for d in dirs:
        sid = upload_one(client, d, existing)
        print(f"{d.name}={sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
