# Slack bot setup (Modal)

Stand up the `slackbot/` Modal app: the 9am daily report posts to Slack, and you can `@`-mention
or reply in its thread to ask follow-ups that reuse that day's Managed Agents session (so the agent
keeps the full report's context).

```
Slack  ──HTTP──▶  Modal slack_web (Bolt, verifies + acks <3s)  ──spawn──▶  answer_followup
  ▲                                                                            │
  └──────────────── chat.postMessage (thread reply) ◀──── AgentSession.ask ◀──┘

Modal Cron 09:00 UTC ─▶ daily_report ─▶ run curator ─▶ chat.postMessage (report) ─▶ Dict[thread_ts]=session_id
```

## 1. Create the Slack app

1. <https://api.slack.com/apps> → **Create New App** → **From scratch**. Name it, pick your workspace.
2. **OAuth & Permissions → Bot Token Scopes**, add:
   - `app_mentions:read` — receive `@bot` mentions
   - `chat:write` — post messages
   - `channels:history` — read replies in the report's thread (use `groups:history` for private channels)
3. **Event Subscriptions → Enable Events**. Leave the Request URL for now (step 4 gives it). Under
   **Subscribe to bot events** add:
   - `app_mention`
   - `message.channels` (or `message.groups` for private channels)
4. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`).
5. **Basic Information → App Credentials** → copy the **Signing Secret**.
6. Invite the bot into the target channel: `/invite @your-bot`.

## 2. Create the Modal secret

The app reads everything from one Modal secret named `brf-secrets`:

```sh
modal secret create brf-secrets \
  ANTHROPIC_API_KEY=sk-ant-... \
  SLACK_BOT_TOKEN=xoxb-... \
  SLACK_SIGNING_SECRET=... \
  SLACK_CHANNEL='#ai-news' \
  FIRECRAWL_API_KEY=... \
  X_BEARER_TOKEN=... \
  OPENAI_API_KEY=...
```

`SLACK_CHANNEL` is where the daily report is posted (the bot must be a member). The `FIRECRAWL_/X_/OPENAI_`
keys are mounted into the agent's container `.env` exactly as the GitHub Actions cron does today.

## 3. Re-provision the agent (option A)

The agent now **returns** the report wrapped in `<report>…</report>` instead of posting it itself
(`system_prompt.md` step 7). Push that prompt to the live agent:

```sh
ANTHROPIC_API_KEY=... python scripts/create_agent.py --update
```

## 4. Deploy and wire the Request URL

```sh
modal deploy slackbot/app.py
```

Modal prints the web URL for `slack_web` (e.g. `https://<you>--blog-research-feed-slack-web.modal.run`).
Back in Slack **Event Subscriptions → Request URL**, enter `<web-url>/slack/events` and wait for the
green **Verified** (Bolt answers the URL-verification handshake automatically).

## 5. Cut over from GitHub Actions

The agent no longer posts the report itself, so the old GitHub Actions daily would now produce nothing.
Disable it so you don't run two dailies:

- Either delete `.github/workflows/daily.yml`, or
- GitHub → repo **Actions** tab → the `daily-ai-news` workflow → **⋯ → Disable workflow**.

Modal's `daily_report` Cron (`0 9 * * *` UTC) now owns the daily run.

## How it runs

| Piece | Modal function | Trigger |
|---|---|---|
| Daily report | `daily_report` | `modal.Cron("0 9 * * *")` |
| Receive Slack events | `slack_web` | HTTP (the Request URL) |
| Answer a follow-up | `answer_followup` | `.spawn()` from `slack_web`, off the 3s-ack path |

State (`thread_ts → session_id`) lives in a `modal.Dict` named `brf-thread-sessions`, so follow-ups
survive cold starts. Nothing needs to stay warm — Modal cold-starts each function on demand.

## Verify / debug

- `modal app logs blog-research-feed` — tail logs.
- Trigger the daily without waiting for 9am: `modal run slackbot/app.py::daily_report`.
- Re-deploy after code changes: `modal deploy slackbot/app.py`.

## Known limits

- A very long report can exceed Slack's 50-block-per-message limit (`markdown_to_blocks` splits by
  section; >50 sections is rejected). If it bites, switch the daily post to chunked thread messages.
- Follow-ups only work on threads the bot itself created (it must hold `thread_ts → session_id`).
  Messages in unknown threads are ignored.
