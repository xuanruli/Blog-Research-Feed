# Blog Research Feed

An AI-news aggregation pipeline: every morning at 9am a Managed Agent reads
the day's writing, video, and podcast output across the AI ecosystem and posts
a curated digest to Slack.

## Architecture

The system has three cooperating components:

1. **The Managed Agent** — hosted in Anthropic's cloud. It owns the prompt,
   the source list, and the editorial judgement. It cannot make outbound
   network calls or hold third-party API keys; instead it requests work by
   emitting `agent.custom_tool_use` events naming tools like `fetch.rss`,
   `firecrawl.scrape`, or `report.slack`.

2. **The cron host** — a scheduled GitHub Action. At 09:00 daily it opens a
   Managed Agent session, streams events from it, and acts as the agent's
   hands. For each `agent.custom_tool_use` event it shells out to this `brf`
   CLI, captures the JSON on stdout, and sends it back to the agent as a
   `user.custom_tool_result`. The loop terminates when the agent stops
   requesting tools and finalizes its Slack post.

3. **The `brf` CLI** (this package) — runs on the cron host, not inside the
   agent container. Because it lives on the host, it can hold real API keys
   (Firecrawl, X, Slack, etc.) via environment variables and reach the open
   internet. Each subcommand is a thin, JSON-emitting wrapper around one
   external service, which keeps the agent/host contract small and
   auditable.

This split — reasoning in the cloud, side effects on a trusted host —
lets the agent be powerful without ever holding production credentials.

## CLI

Install in editable mode and explore:

```bash
pip install -e .
brf --help
```

See `.env.example` for required environment variables.
