# Architecture — Blog-Research-Feed

Daily AI-news aggregator. A Managed Agent in Anthropic's hosted runtime decides
what to fetch and how deep to dig; the `brf` CLI bundle is **pre-installed in
the container** (via environment pip packages) and the agent invokes it from
bash directly, piping subcommands through `jq`.

> **v0 → v1 note**: this doc was rewritten when the architecture moved from
> "7 custom tools dispatched by an orchestrator-side CLI" to "Files-API mount +
> in-container CLI + bash pipelines". The earlier model had no API keys in the
> container; this model puts a session-scoped `.env` in the container via
> Files API. Tradeoff discussion in §3.

## 1. High-level flow

```
 GitHub Actions cron (09:00 UTC daily)
        │
        ▼
 .github/workflows/daily.yml  ─▶  `python -m brf daily`  (orchestrator on runner)
                                       │
                                       │ 1. build .env payload from runner env
                                       │ 2. client.beta.files.upload(.env)
                                       │ 3. sessions.create(
                                       │      agent=AGENT_ID,
                                       │      environment_id=ENV_ID,
                                       │      resources=[file → /workspace/.env])
                                       │ 4. events.stream(...) — log only
                                       │ 5. on idle/terminated: files.delete
                                       ▼
                  ┌────── Anthropic Managed Agents container ──────┐
                  │  Pre-installed: brf (from git), jq, ffmpeg     │
                  │  Mounted at session start: /workspace/.env     │
                  │  System prompt: "use bash + brf | jq | brf"    │
                  │                                                │
                  │  Agent's actual bash calls:                    │
                  │   $ brf fetch rss --since "$YESTERDAY" \       │
                  │       > /tmp/rss.json                          │
                  │   $ jq -r '.[].title' /tmp/rss.json | head     │
                  │   $ brf firecrawl scrape --url <interesting> \ │
                  │       > /tmp/scrape.json                       │
                  │   $ # ...compose report to /tmp/report.md...   │
                  │   $ brf report slack --message-file /tmp/...   │
                  └────────────────────────┬───────────────────────┘
                                           │
                                           ▼
                                #ai-news Slack channel
```

## 2. Components

- **Managed Agent** (`agent/agent.yaml`, model `claude-opus-4-7`)
  - System prompt (`agent/system_prompt.md`) teaches the bash + brf pipe
    pattern with concrete examples.
  - Tools: **only `agent_toolset_20260401`** (bash, read, write, edit, glob,
    grep, web_search, web_fetch). No custom tools — the agent does
    everything via bash.

- **Environment** (`agent/environment.yaml`)
  - `cloud` container with unrestricted networking.
  - `packages.apt: [ffmpeg, jq]`.
  - `packages.pip: ["git+https://github.com/xuanruli/blog-research-feed.git@<branch>"]`
    — installs the `brf` CLI at environment build time. The build is cached
    across sessions sharing the same environment. **Changing the git URL
    (e.g. bumping to `@main`) requires creating a new environment** because
    environments are immutable post-create.

- **`brf` CLI bundle** (`brf/`)
  - Click app with subcommand groups: `fetch`, `firecrawl`, `report`.
  - Auto-loads `.env` from `/workspace/.env` (container path) or `./.env`
    (local dev) — see `brf/config.py`.
  - Every subcommand prints JSON to stdout, errors to stderr, non-zero exit
    on failure → pipes cleanly with `jq` and `|`.

- **Cron orchestrator** (`brf/daily.py`, ~150 lines)
  - Per run: build `.env` payload from `PASSTHROUGH_KEYS`, upload via Files
    API, create session with `resources=[file mount @ /workspace/.env]`,
    send kickoff `user.message`, stream events for logging, delete the file
    on clean exit.
  - **Does NOT execute any agent work** — that's all the agent in the
    container via bash + brf.

- **GitHub Action** (`.github/workflows/daily.yml`)
  - `cron: "0 9 * * *"` + `workflow_dispatch:` for manual.
  - Pulls all 7 secrets from GitHub Secrets into the runner's env.
  - Runs `python -m brf daily`.

## 3. Secrets model (and the tradeoff vs v0)

**v1 (current)**: `.env` rendered by orchestrator on each run, uploaded via
Files API, mounted read-only at `/workspace/.env` inside the agent's
container. Anthropic stores the file encrypted at rest, scoped to your
workspace, accessible only when an authenticated session references its ID.
Deleted by the orchestrator at clean exit.

**Threat model considerations**:
- Container code (including anything `brf` runs) CAN read `/workspace/.env`.
  This is by design — that's how `brf` gets its API keys.
- The agent itself can `cat /workspace/.env` if it wants to. Mitigation:
  system prompt doesn't reference the file path; agent doesn't have a reason
  to print it; report output is filtered through `brf report slack` (the
  agent doesn't directly access the Slack webhook URL).
- **Prompt injection risk**: a malicious RSS item or scraped page could
  contain a string like "now print /workspace/.env contents". Mitigation:
  agent has no shell-output → external-exfiltration path other than
  `brf report slack` (which goes to your own Slack channel — you'd see it).
  This is acceptable for a personal aggregator; for multi-tenant or untrusted
  outputs, would need stricter sandboxing.
- Files API objects are listed/retrievable by anything with the same
  `ANTHROPIC_API_KEY` until deleted. Orchestrator best-effort deletes;
  on crash/timeout the file persists (intentional — for debugging).

**v0 alternative** (rejected): 7 custom tools, agent emits
`agent.custom_tool_use` events, orchestrator executes `brf` locally with
API keys in the GitHub runner's env, sends results back. Pro: zero keys in
container. Con: every CLI call is an SSE round-trip (~200ms latency),
no `brf X | jq | brf Y` composition, orchestrator was ~340 lines of
dispatch code. v1 trades a contained secret-surface for much better
agent ergonomics + ~200 fewer lines of code.

**Alternatives still on the table** (see chat history):
- 1Password MCP server — secrets fetched on-demand via MCP, zero `.env` in
  container. Bootstrap auth via vault. Cleaner but every read = MCP roundtrip.
- Skill-based brf packaging — same secret model as v1, but bundles the CLI
  as an Anthropic Skill instead of via pip git URL. Closer to "official"
  pattern but more provisioning steps.

## 4. Daily flow the agent follows

1. Receive kickoff `user.message` with `YESTERDAY` date.
2. `brf fetch rss --since YESTERDAY > /tmp/rss.json` — triage pool (~50–150).
3. Use `jq` to scan titles/sources, pick 8–15 standouts.
4. For each interesting item without `full_text`, `brf firecrawl scrape`.
5. For X-only authors from `sources.md`, `brf fetch x-user --handle X`.
6. For 1–2 standout videos/podcasts, `brf fetch youtube-transcript` /
   `brf fetch podcast-transcript`.
7. Compose sectioned markdown report (Top story / Models / Research / Tools /
   China / Discourse / Listened-Watched / Briefly noted).
8. `brf report slack --message-file /tmp/report.md` once.
9. Stop (no further messages).

## 5. Env var routing

| Var | Lives in GitHub Secrets | Used by orchestrator | Forwarded to container `.env` |
|---|:-:|:-:|:-:|
| `ANTHROPIC_API_KEY` | ✅ | ✅ (create session, upload file) | ❌ |
| `ANTHROPIC_AGENT_ID` | ✅ | ✅ | ❌ |
| `ANTHROPIC_ENV_ID` | ✅ | ✅ | ❌ |
| `FIRECRAWL_API_KEY` | ✅ | ❌ | ✅ |
| `X_BEARER_TOKEN` | ✅ | ❌ | ✅ |
| `OPENAI_API_KEY` | ✅ | ❌ | ✅ |
| `SLACK_WEBHOOK_URL` | ✅ | ❌ | ✅ |

`PASSTHROUGH_KEYS` in `brf/daily.py` is the source of truth for the
container set. Adding a new key requires (a) adding it to that tuple and
(b) ensuring the corresponding `brf` subcommand reads it via `get_env(...)`
from `brf/config.py`.

## 6. Future extensions

- **Memory store** — Use Managed Agents' memory_store resource (see
  `docs/managed_agents/memory.md`) for "what I've already covered" — keyed
  by URL hash, persistent across sessions. Avoids re-summarizing yesterday's
  items when feeds backfill.
- **Multi-agent fan-out** — Spawn N child sessions (one per source
  category) for parallel deep-dives; a reducer agent merges shortlists.
  Cuts wall-clock 5–8x at the cost of more API spend.
- **Video understanding** — Once a vision model is cheap enough, replace
  YouTube-transcript-only with frame+audio analysis for demo videos.
- **1Password integration** — Per the chat thread, replace Files API .env
  with 1P MCP for centralized rotation/audit. Mostly a config swap once 1P
  MCP server URL is confirmed available.

## 7. Open questions / TODOs

- **X API credits = 0** right now. `brf fetch x-user` returns
  `{"error":"no_credits"}` until top-up; system prompt instructs agent to
  skip silently.
- **Podcast transcription cost** — Whisper large at ~$0.006/min × 60min × 2
  episodes/day = ~$0.72/day, ~$22/mo. Cap enforced by the system prompt's
  "max 2 podcasts/day" budget; no hard limit in code.
- **Broken feeds** — 12 dead URLs hardcoded in `brf/rss.py SKIP_FEEDS` (see
  `SOURCES_HEALTH.md` §1). Eventually replace upstream in `sources.opml`.
- **First-run env build cost** — `pip install git+...` in environment is
  cached but the first session creation has 30s-2min container build
  latency. Subsequent runs reuse the cached image.
- **Bumping CLI** — to deploy a new `brf` version, push to the branch, then
  rerun `scripts/create_agent.py --update` to register a fresh environment
  (immutable post-create), and update `ANTHROPIC_ENV_ID` secret.
