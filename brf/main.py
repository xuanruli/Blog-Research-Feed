"""brf — CLI bundle for the Blog Research Feed Managed Agent.

Each subcommand is invoked in two contexts:

* By the agent itself, inside its Managed Agents container via bash
  (`brf fetch rss --since YYYY-MM-DD | jq ...`). The CLI auto-loads secrets
  from `/workspace/.env` mounted by the orchestrator at session-create time.
* Locally, for smoke-testing without going through the agent loop
  (`brf firecrawl scrape --url https://...` after `cp .env.example .env`).

`brf daily` is the orchestrator entry point: it builds the .env payload,
uploads it via Files API, creates a session, streams events, and exits.
"""
from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="brf")
def cli() -> None:
    """Blog Research Feed CLI."""


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
@cli.group()
def fetch() -> None:
    """Fetch source content (RSS, X, YouTube, podcasts)."""


@fetch.command("rss")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Only include items published on/after this date (YYYY-MM-DD).")
@click.option("--opml", type=click.Path(exists=False, dir_okay=False), default=None,
              help="Path to OPML file listing feeds. Defaults to repo sources.opml.")
def fetch_rss(since, opml):
    """Fetch new items from RSS/Atom feeds. Outputs JSON list of items."""
    from pathlib import Path

    from .io import emit_json
    from .rss import fetch_recent

    items = fetch_recent(
        since=since,
        opml_path=Path(opml) if opml else None,
    )
    emit_json(items)


@fetch.command("x-user")
@click.option("--handle", type=str, required=True, help="X (Twitter) handle without leading @.")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None,
              help="Only include posts on/after this date (YYYY-MM-DD).")
def fetch_x_user(handle, since):
    """Fetch recent posts from an X user. Outputs JSON."""
    from .x_client import fetch_user_recent
    from .io import emit_json

    result = fetch_user_recent(handle, since=since)
    emit_json(result)


@fetch.command("youtube-transcript")
@click.option("--url", type=str, required=True, help="YouTube video URL.")
def fetch_youtube_transcript(url):
    """Fetch a YouTube transcript. Outputs JSON {title, transcript, channel}."""
    from . import youtube
    from .io import emit_json

    emit_json(youtube.get_transcript(url))


@fetch.command("podcast-transcript")
@click.option("--url", type=str, required=True, help="Podcast RSS feed URL.")
@click.option("--episode-index", type=int, default=0, show_default=True,
              help="Index into the RSS entries list (0 = most recent).")
def fetch_podcast_transcript(url, episode_index):
    """Fetch / generate a podcast transcript. Outputs JSON {title, transcript}."""
    from . import podcast
    from .io import emit_json

    emit_json(podcast.get_transcript(url, episode_index=episode_index))


# ---------------------------------------------------------------------------
# firecrawl
# ---------------------------------------------------------------------------
@cli.group()
def firecrawl() -> None:
    """Firecrawl-backed scrape/search."""


@firecrawl.command("scrape")
@click.option("--url", type=str, required=True, help="URL to scrape.")
def firecrawl_scrape(url):
    """Scrape a URL via Firecrawl. Outputs JSON {markdown, metadata}."""
    from .firecrawl_client import scrape
    from .io import emit_json

    try:
        result = scrape(url)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    emit_json(result)


@firecrawl.command("search")
@click.option("--query", type=str, required=True, help="Search query.")
@click.option("--limit", type=int, default=10, show_default=True,
              help="Maximum number of results.")
def firecrawl_search(query, limit):
    """Search the web via Firecrawl. Outputs JSON."""
    from .firecrawl_client import search
    from .io import emit_json

    try:
        results = search(query, limit=limit)
    except RuntimeError as e:
        raise click.ClickException(str(e))
    emit_json(results)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
@cli.group()
def report() -> None:
    """Reporting / delivery commands."""


@report.command("slack")
@click.option("--webhook-env", type=str, default="SLACK_WEBHOOK_URL", show_default=True,
              help="Name of the env var holding the Slack incoming webhook URL.")
@click.option("--message-file", type=click.Path(exists=False, dir_okay=False), required=True,
              help="Path to a file containing the Slack message body (markdown).")
def report_slack(webhook_env, message_file):
    """Post a message to Slack via incoming webhook."""
    from pathlib import Path

    from .io import emit_json
    from .slack import markdown_to_blocks, post_blocks

    try:
        text = Path(message_file).read_text(encoding="utf-8")
    except OSError as e:
        raise click.ClickException(f"could not read --message-file: {e}")

    blocks = markdown_to_blocks(text)
    result = post_blocks(blocks, webhook_env=webhook_env)
    emit_json(result)


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------
@cli.command("daily")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run the orchestration loop without posting side effects.")
def daily(dry_run):
    """Daily orchestrator.

    Builds a `.env` payload from PASSTHROUGH_KEYS in the runner's env,
    uploads it via Files API, creates a Managed Agent session that mounts
    it at `/workspace/.env`, streams events for logging, and exits when
    the session goes idle (after having seen at least one running transition)
    or terminates. Best-effort deletes the uploaded file on clean exit.

    All the agent's real work happens via bash + `brf` CLI inside the
    container — no custom-tool dispatch on this side.
    """
    from .daily import run as run_daily

    run_daily(dry_run=dry_run)


if __name__ == "__main__":  # pragma: no cover
    cli()
