"""brf — CLI bundle for the Blog Research Feed Managed Agent.

This package is a pure **tool CLI** used by the agent inside its Managed
Agents container via bash. It knows nothing about Managed Agents, sessions,
or the Anthropic SDK — it just fetches things and emits JSON.

Two invocation contexts:

* Inside the agent's session container: ``brf fetch rss --since … | jq …``.
  The CLI auto-loads secrets from ``/workspace/.env`` (mounted by the
  orchestrator at session-create time) via ``brf.config``.
* Locally, for smoke-testing without going through the agent loop. Set the
  same env vars in your shell or a local ``.env``.

The cron-side orchestrator that creates the Managed Agent session and
manages the SSE event stream lives in the separate ``orchestrator``
package (``python -m orchestrator.daily``). The two are intentionally
decoupled — ``brf`` does not import from ``orchestrator`` and vice versa.
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
# fetch-all / fetch-full — unified entry points (Phase 1 scaffolding;
# behavior wired up in Phase 2+).
# See BRF_FETCHER_DESIGN.md §4.
# ---------------------------------------------------------------------------
def _build_aggregator(output_dir):
    """Construct the FeedAggregator with all registered fetchers.

    Phase 2: RssFetcher registered. Phase 3+ adds XFetcher / YouTubeFetcher
    / PodcastFetcher / FirecrawlIndexFetcher one at a time.
    """
    from pathlib import Path

    from .aggregator import FeedAggregator
    from .fetchers.firecrawl_index import FirecrawlIndexFetcher
    from .fetchers.podcast import PodcastFetcher
    from .fetchers.rss import RssFetcher
    from .fetchers.x import XFetcher
    from .fetchers.youtube import YouTubeFetcher
    from .sources_config import active_podcast_feeds, active_rss_feeds, load_sources

    output_dir = Path(output_dir)
    cfg = load_sources()
    fetchers: list = [
        RssFetcher(feeds=active_rss_feeds(cfg), output_dir=output_dir),
        XFetcher(handles=cfg["x"]["handles"]),
        YouTubeFetcher(channels=cfg["youtube"]["channels"]),
        PodcastFetcher(feeds=active_podcast_feeds(cfg)),
        FirecrawlIndexFetcher(entries=cfg.get("firecrawl_index") or []),
    ]
    return FeedAggregator(fetchers, output_dir=output_dir)


@cli.command("fetch-all")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), required=True,
              help="Only include items published on/after this date (YYYY-MM-DD).")
@click.option("--output-dir", type=click.Path(file_okay=False), default="/tmp/feed",
              show_default=True,
              help="Directory to write index.json + full/<id>.* into.")
def fetch_all(since, output_dir):
    """Bulk-fetch all configured sources, write unified index.json.

    Phase 1: writes an empty list. Phase 2+: actual fetcher fan-out.
    """
    agg = _build_aggregator(output_dir)
    items = agg.fetch_all(since)
    click.echo(f"{len(items)} items written to {output_dir}/index.json",
               err=True)


@cli.command("fetch-full")
@click.option("--id", "item_id", type=str, required=True,
              help="FeedItem id (from index.json).")
@click.option("--output-dir", type=click.Path(file_okay=False), default="/tmp/feed",
              show_default=True,
              help="Directory containing index.json + full/.")
@click.option("--force", is_flag=True, default=False,
              help="Re-fetch even if the body file already exists.")
def fetch_full_cmd(item_id, output_dir, force):
    """Drill-down on one item by id; dispatches by source_type."""
    agg = _build_aggregator(output_dir)
    path = agg.fetch_full(item_id, force=force)
    if path is None:
        raise click.ClickException(
            f"could not fetch full for id={item_id!r} "
            f"(not in index, no fetcher registered, or content unavailable)"
        )
    click.echo(str(path))


if __name__ == "__main__":  # pragma: no cover
    cli()
