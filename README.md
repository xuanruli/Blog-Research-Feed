# Blog Research Feed

A daily AI engineering signal curator. Every morning a Claude agent hosted
on Anthropic Managed Agents fetches yesterday's frontier engineering content
(RSS, X, YouTube, podcasts, Firecrawl-indexed blogs), drills into the most
promising items, picks a Top 10, and posts a report to Slack.

This is not a news digest. The target reader is an engineer working on VLM,
video agents, multimodal, or coding agents — the agent surfaces the ten
things worth reading yesterday and writes its own takeaway for each.

## How it works

```
GitHub Action (cron, 09:00 UTC)
  └── python -m cron.daily
        ├── Resolve agent + environment by name via Anthropic API
        ├── sessions.create(...) mounts /workspace/.env (pre-uploaded Files API object)
        └── Stream session events until idle

Inside the session container:
  $ brf fetch-all --since YESTERDAY        → /tmp/feed/index.json
  $ jq ... /tmp/feed/index.json            triage candidates
  $ brf fetch-full --id <id>               drill into selected items
  $ brf report slack --message-file ...    post to Slack
```

The runner only calls the Anthropic API. All third-party API keys
(Firecrawl, X, OpenAI, Slack) live in a `.env` file pre-uploaded once to
the Files API; the runner mounts it by file id and never sees the
contents.

## Setup

### 1. API keys

You need:

| Key | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Runner calls sessions/files API |
| `FIRECRAWL_API_KEY` | Scrape + index endpoints (https://firecrawl.dev) |
| `X_BEARER_TOKEN` | X API v2 |
| `OPENAI_API_KEY` | Whisper transcription |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for the target channel |

Put them in a local `.env` (copy from `.env.example`).

### 2. Provision the Managed Agent + Environment

```bash
pip install -e .
python scripts/create_agent.py
```

This creates the environment (cloud container with `blog-research-feed`
from PyPI + apt jq/ffmpeg) and the coordinator agent plus its reader and
reviewer subagents. To push config changes later, run with `--update`.

The script identifies resources by the `name:` field in
`agent/*.yaml` — concrete IDs are looked up at runtime, so you never
have to track them as secrets.

### 3. Upload the container `.env`

```bash
python -m scripts.upload_env --from-file .env
```

This prints a file id like `file_01ABC...`. Save it as a GitHub
**repository variable** named `ENV_FILE_ID` (Settings → Variables →
Actions). Re-run whenever you rotate a key.

### 4. GitHub Secrets

The only secret the runner needs:

- `ANTHROPIC_API_KEY` — for calling sessions/files API

Third-party keys do not go on the runner; they live inside the uploaded
`.env`.

### 5. Test

Trigger manually from GitHub Actions → "daily-ai-news" → Run workflow,
or dry-run locally:

```bash
python -m cron.daily --dry-run
```

## Operations

### Releasing a new `brf` version

```bash
# bump version in pyproject.toml, then:
rm -rf dist/ build/ *.egg-info
python -m build
TWINE_USERNAME=__token__ TWINE_PASSWORD=$PYPI_API_TOKEN twine upload dist/blog_research_feed-*

# bump agent/environment.yaml:
#   name: blog-research-feed-env-v8 → v9
#   "blog-research-feed==0.2.4"     → "==0.2.5"

python scripts/create_agent.py --update
```

The cron run picks up the new environment by name on its next invocation.

### Rotating secrets

Update the value in your local `.env`, re-run `python -m scripts.upload_env`,
update the `ENV_FILE_ID` repo variable with the new file id.

### Local development

```bash
pip install -e .[dev]
pytest -q
brf fetch-all --since 2026-05-20      # end-to-end smoke (~$0.2 firecrawl spend)
```

## CLI reference

Main pipeline (what the agent uses):

```bash
brf fetch-all  --since <date> --output-dir /tmp/feed   # parallel fetch, writes index.json
brf fetch-full --id <id>      --output-dir /tmp/feed   # drill into one item
brf report slack --message-file <path>                 # post to Slack webhook
```

Auxiliary (rarely needed):

```bash
brf fetch x-user --handle <handle> --since <date>
brf fetch youtube-transcript --url <url>
brf fetch podcast-transcript --url <feed-or-episode-url>
brf firecrawl scrape --url <url>
brf firecrawl search --query <q> --limit 10
```

`FeedItem` shape (see `brf/feed_item.py`):

```json
{
  "id": "ab12cd34ef567890",
  "source_type": "rss",
  "source": "Simon Willison's Weblog",
  "title": "...",
  "url": "https://...",
  "published": "2026-05-19T14:23:00+00:00",
  "summary": "≤ 500 chars",
  "has_full": true,
  "needs_firecrawl": false,
  "extra": {}
}
```

## Repository layout

| Path | Purpose |
|---|---|
| `brf/` | CLI bundle, installed into the session container |
| `brf/main.py` | Click entry: `fetch-all` / `fetch-full` are the main flow |
| `brf/aggregator.py`, `brf/fetchers/` | Parallel fetch + dedupe + scheduling |
| `brf/sources.yaml` | Single source of truth for all feeds / handles / channels |
| `brf/firecrawl_client.py`, `slack.py`, `rss.py`, `x_client.py`, `youtube.py`, `podcast.py` | Per-service clients |
| `agent/agent.yaml`, `reader.yaml`, `reviewer.yaml` | Managed Agent definitions (lookup by name) |
| `agent/*_prompt.md` | System prompts for coordinator / reader / reviewer |
| `cron/daily.py` | Host-side runner: resolves agent/env by name, opens a session, streams events |
| `scripts/create_agent.py` | One-shot provisioning / updates for agent + env |
| `scripts/upload_env.py` | One-shot upload of `.env` to the Files API |
| `.github/workflows/daily.yml` | 09:00 UTC cron |
| `docs/managed_agents/`, `docs/agent_sdk/` | Local copies of Anthropic docs |

For deeper design notes see [`ARCHITECTURE.md`](./ARCHITECTURE.md) and
[`BRF_FETCHER_DESIGN.md`](./BRF_FETCHER_DESIGN.md).

## Known limits

- Whisper has a 25 MB per-file cap; long podcasts are rejected (status `too_large`). The system prompt caps daily transcriptions to keep cost predictable.
- Some Chinese-language RSS feeds are flaky; `firecrawl_index` backfills the gap (see `SOURCES_HEALTH.md`).
- The SSE event stream does not auto-reconnect on drop. The 45-minute job timeout normally avoids the issue.
- YouTube channel RSS occasionally rate-limits residential IPs. The fetcher swallows the error and continues.
