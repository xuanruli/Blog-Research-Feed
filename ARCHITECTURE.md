# Architecture — Blog-Research-Feed

Daily AI-news aggregator. A Managed Agent in Anthropic's hosted runtime decides what
to fetch and how deep to dig; a local CLI bundle (`brf`) executes the actual I/O on
the orchestrator host (where API keys live).

## 1. High-level flow

```
 GitHub Actions cron (09:00 UTC daily)
        │
        ▼
 .github/workflows/daily.yml  ───▶  `python -m brf daily`  (orchestrator)
                                        │
                                        │ client.beta.sessions.create(agent_id=...)
                                        ▼
                          ┌────────── Anthropic Managed Agents ──────────┐
                          │   Hosted container running host agent         │
                          │   (system prompt + custom-tool declarations)  │
                          └───────────────────┬───────────────────────────┘
                                              │ SSE event stream
                          client.beta.sessions.events.stream(session_id)
                                              │
                  ┌───────────────────────────┴───────────────────────────┐
                  │ event: tool_use {name: fetch_rss_recent, input: {...}}│
                  ▼                                                       │
        Orchestrator dispatches to `brf` subcommand on host               │
        (reads FIRECRAWL_API_KEY / X_BEARER_TOKEN / etc. from env)        │
                  │                                                       │
                  ▼                                                       │
        sessions.tool_results.submit(session_id, tool_use_id, result)  ───┘
                                              │
                                              ▼
                       Agent loops: decides to scrape / transcribe / search more
                                              │
                                              ▼
                         Final tool call: post_to_slack(report_markdown)
                                              │
                                              ▼
                                    #ai-news Slack channel
```

## 2. Components

- **Managed Agent** (Anthropic hosted, model `claude-opus-4-7`)
  - System prompt: "You are a daily AI-news curator. Yesterday is {date}. Use
    `fetch_rss_recent` first; for every interesting headline call
    `firecrawl_scrape`; for X-only authors call `fetch_x_user`; transcribe
    podcasts/videos when warranted; finally call `post_to_slack` exactly once."
  - Custom tools declared at agent create-time (schemas in section 3).
  - Container has no secrets — all key-bearing work happens on the host.

- **`brf` CLI bundle** (`/home/user/Blog-Research-Feed/brf/`)
  - Each subcommand is BOTH a standalone CLI (`brf fetch-rss --since 2026-05-18`)
    AND the implementation behind a same-named custom tool. The orchestrator's
    tool-dispatcher is a thin `argparse`-style wrapper that calls the same
    Python function and returns JSON.
  - Reads sources from `sources.opml`. Honors `SOURCES_HEALTH.md` flags (skip
    `BROKEN`, auto-Firecrawl for `NEEDS_FIRECRAWL`).

- **Cron orchestrator** (`brf/daily.py`, entry `python -m brf daily`)
  - Creates session, opens `client.beta.sessions.events.stream(...)`, on each
    `tool_use` event dispatches to local CLI, posts result back via
    `sessions.tool_results.submit`. Exits when session emits `message_stop`.

- **GitHub Action** (`.github/workflows/daily.yml`)
  - `schedule: cron: "0 9 * * *"`, `workflow_dispatch:` for manual.
  - Steps: checkout, `pip install -e .`, `python -m brf daily`.
  - Pulls all secrets from GitHub Secrets into the runner's env.

## 3. Custom tools (input schema bullets)

- `fetch_rss_recent` — `since_date: ISO8601` → list of `{source, title, url, published, summary, full_text?}`
- `fetch_x_user` — `handle: str`, `since_date: ISO8601` → list of posts
- `firecrawl_scrape` — `url: str` → `{title, markdown, author, published}`
- `firecrawl_search` — `query: str`, `limit: int=10` → list of `{url, title, snippet}`
- `fetch_youtube_transcript` — `url: str` → `{title, channel, transcript}`
- `fetch_podcast_transcript` — `rss_url: str`, `episode_index: int=0` → `{title, transcript}` (Whisper)
- `post_to_slack` — `report_markdown: str` → `{ok: bool, ts: str}`

## 4. Daily flow the agent follows

1. Call `fetch_rss_recent(since_date=yesterday)`.
2. Triage headlines; drop dupes/marketing.
3. For each promising item without `full_text`, call `firecrawl_scrape(url)`.
4. Call `fetch_x_user` for the X-only handle list (see `sources.md`).
5. Optionally `firecrawl_search` for hot topics not on the source list.
6. For 1–2 standout podcasts/videos, transcribe via the matching tool.
7. Compose a sectioned markdown report (Models / Research / Tools / China / Discourse).
8. Call `post_to_slack(report_markdown)` once. Stop.

## 5. Secrets

- **GitHub Secrets** (injected as env into the runner — and thus into the
  orchestrator's process, where `brf` subcommands read them):
  - `ANTHROPIC_API_KEY`, `ANTHROPIC_AGENT_ID`
  - `FIRECRAWL_API_KEY`, `X_BEARER_TOKEN`
  - `YOUTUBE_API_KEY` (optional), `OPENAI_API_KEY` (Whisper)
  - `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`
- **Anthropic vault**: nothing today. Per
  `shared/managed-agents-client-patterns.md` Pattern 9, vaults are MCP-only;
  the hosted container has no env-var mechanism, so key-bearing CLIs MUST stay
  host-side.
- **Local `.env`** (dev only, gitignored): same vars for `brf <subcmd>`
  smoke-testing without going through the agent loop.

## 6. Future extensions

- **Memory store** — SQLite at `./state/seen.db` keyed by URL hash, exposed as
  `recall_seen(url)` / `mark_seen(url)` tools so the agent doesn't re-summarize
  yesterday's items when feeds backfill. Long-term: per-topic episodic memory
  ("already covered MoE survey on 2026-04-12").
- **Multi-agent fan-out** — Spawn N child sessions (one per source category)
  for parallel deep-dives; a reducer agent merges shortlists into one report.
  Cuts wall-clock 5–8x.
- **Video understanding** — Once a vision model on the platform is cheap
  enough, replace YouTube-transcript-only with frame+audio analysis for demos.

## 7. Open questions / TODOs

- **X API credits = 0** right now. `fetch_x_user` will fail until top-up; agent
  prompt should tolerate the tool returning `{error: "no_credits"}` and skip.
- **Podcast transcription cost** — Whisper-large at ~$0.006/min × 60min × 3
  episodes/day = $1/day, $30/mo. Need budget cap; consider gating on title
  heuristic before transcribing.
- **Video model TBD** — no decision yet on YouTube beyond transcript fetch.
- **Broken feeds** — 12 dead URLs in `SOURCES_HEALTH.md` §1; need replacements
  or Firecrawl fallbacks before `fetch_rss_recent` is reliable.
- **Agent ID provisioning** — one-time `scripts/create_agent.py` to register
  the agent + tools and print the ID into GitHub Secrets; not written yet.
