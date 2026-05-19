#!/usr/bin/env python3
"""One-shot provisioning script for the Blog-Research-Feed managed agent.

Creates (or updates) the cloud environment and the host agent, then prints
the IDs in env-var form for piping into a .env file or GitHub Secrets.

Usage:
    python scripts/create_agent.py            # create new
    python scripts/create_agent.py --update   # update existing by name
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = REPO_ROOT / "agent" / "system_prompt.md"

AGENT_NAME = "blog-research-feed-curator"
ENV_NAME = "blog-research-feed-env"
MODEL = "claude-opus-4-7"

ENV_CONFIG = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
}


def build_tools() -> list[dict]:
    """Tool list: full pre-built toolset + 7 custom tools from ARCHITECTURE.md §3."""
    return [
        {"type": "agent_toolset_20260401"},
        {
            "type": "custom",
            "name": "fetch_rss_recent",
            "description": (
                "Fetch recent items from the curated RSS source list (sources.opml, respecting "
                "SOURCES_HEALTH.md flags). ALWAYS call this first; returns the triage pool of "
                "~50-150 items published on/after `since_date`. Items with `full_text` populated "
                "do not require a follow-up scrape."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "since_date": {
                        "type": "string",
                        "description": "ISO8601 date or datetime (e.g. '2026-05-18').",
                    },
                },
                "required": ["since_date"],
            },
        },
        {
            "type": "custom",
            "name": "fetch_x_user",
            "description": (
                "Fetch recent posts from a single X (Twitter) user since `since_date`. Use for "
                "X-only authors with no RSS. May return {\"error\": \"no_credits\"} when quota "
                "is exhausted — in that case silently skip and continue."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "X handle without @ prefix (e.g. 'karpathy').",
                    },
                    "since_date": {
                        "type": "string",
                        "description": "ISO8601 date or datetime.",
                    },
                },
                "required": ["handle", "since_date"],
            },
        },
        {
            "type": "custom",
            "name": "firecrawl_scrape",
            "description": (
                "Scrape a single URL via Firecrawl and return clean markdown plus metadata "
                "{title, markdown, author, published}. Use for promising RSS items lacking "
                "full_text. Cost is non-trivial — only call after the headline already looks "
                "interesting."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute https URL.",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "type": "custom",
            "name": "firecrawl_search",
            "description": (
                "Web search via Firecrawl, returning list of {url, title, snippet}. Use ONLY "
                "for hot topics not covered by the curated RSS list (e.g. breaking news)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language query."},
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-20). Default 10.",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "type": "custom",
            "name": "fetch_youtube_transcript",
            "description": (
                "Fetch the transcript of a YouTube video, returning {title, channel, transcript}. "
                "Use sparingly — at most 1-2 per daily run, only for talks that look unusually "
                "high-signal from title alone."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full YouTube watch URL.",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "type": "custom",
            "name": "fetch_podcast_transcript",
            "description": (
                "Transcribe a podcast episode via Whisper, returning {title, transcript}. "
                "EXPENSIVE (~$0.36/episode); cap at 1-2 episodes per daily run. "
                "episode_index=0 is the most recent."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rss_url": {"type": "string", "description": "Podcast RSS feed URL."},
                    "episode_index": {
                        "type": "integer",
                        "description": "0 = most recent. Default 0.",
                        "default": 0,
                    },
                },
                "required": ["rss_url"],
            },
        },
        {
            "type": "custom",
            "name": "post_to_slack",
            "description": (
                "Post the final daily report to the #ai-news Slack channel. Call EXACTLY ONCE at "
                "the end of the session. Returns {ok: bool, ts: str}. After this call the run is "
                "complete — do not emit further tool calls."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "report_markdown": {
                        "type": "string",
                        "description": "Complete Slack-flavored markdown report.",
                    },
                },
                "required": ["report_markdown"],
            },
        },
    ]


def find_by_name(items, name: str):
    for it in items:
        if getattr(it, "name", None) == name and getattr(it, "archived_at", None) is None:
            return it
    return None


def ensure_environment(client: Anthropic, update: bool):
    existing = find_by_name(list(client.beta.environments.list()), ENV_NAME)
    if existing and not update:
        print(f"# Environment '{ENV_NAME}' already exists; reusing.", file=sys.stderr)
        return existing
    if existing and update:
        # Environments are not versioned — recreate-style update isn't supported by the API.
        # We just reuse the existing one (config rarely changes).
        print(f"# Reusing existing environment '{ENV_NAME}' (envs are not updatable).", file=sys.stderr)
        return existing
    return client.beta.environments.create(name=ENV_NAME, config=ENV_CONFIG)


def ensure_agent(client: Anthropic, system_prompt: str, tools: list[dict], update: bool):
    existing = find_by_name(list(client.beta.agents.list()), AGENT_NAME)
    if existing and update:
        print(f"# Updating existing agent '{AGENT_NAME}' (v{existing.version}).", file=sys.stderr)
        return client.beta.agents.update(
            existing.id,
            version=existing.version,
            model=MODEL,
            system=system_prompt,
            tools=tools,
        )
    if existing and not update:
        print(
            f"# Agent '{AGENT_NAME}' already exists (id={existing.id}). "
            f"Pass --update to push new config.",
            file=sys.stderr,
        )
        return existing
    return client.beta.agents.create(
        name=AGENT_NAME,
        model=MODEL,
        system=system_prompt,
        tools=tools,
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

    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    tools = build_tools()
    client = Anthropic()

    env = ensure_environment(client, update=args.update)
    agent = ensure_agent(client, system_prompt=system_prompt, tools=tools, update=args.update)

    print(f"ANTHROPIC_AGENT_ID={agent.id}")
    print(f"ANTHROPIC_ENV_ID={env.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
