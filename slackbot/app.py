"""Modal deployment: the 9am daily report and the Slack follow-up bot, over the shared session core.

Three Modal functions:
- ``daily_report`` (Cron): run the curator, post the report via chat.postMessage, remember thread_ts -> session_id.
- ``slack_web`` (web endpoint): Bolt receives @mentions / thread replies, acks within 3s, spawns a worker.
- ``answer_followup`` (spawned worker): reuse that thread's session to answer, post back into the thread.

Deploy with ``modal deploy slackbot/app.py``; the Slack Request URL is ``<web-url>/slack/events``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import modal

from slackbot.threads import ThreadSessionMap

APP_NAME = "blog-research-feed"
SECRET_NAME = "brf-secrets"
THREAD_MAP_NAME = "brf-thread-sessions"
DEFAULT_CHANNEL = "#ai-news"

AGENT_YAML = Path("/root/agent/agent.yaml")
ENV_YAML = Path("/root/agent/environment.yaml")

image = (
    modal.Image.debian_slim()
    .uv_pip_install(
        "anthropic", "slack-bolt", "fastapi[standard]", "httpx", "python-dotenv", "pyyaml"
    )
    .add_local_python_source("session", "brf", "cron", "slackbot")
    .add_local_dir("agent", remote_path="/root/agent")
)

app = modal.App(APP_NAME, image=image)
secret = modal.Secret.from_name(SECRET_NAME)
thread_sessions = modal.Dict.from_name(THREAD_MAP_NAME, create_if_missing=True)


def _post(
    channel: str, text: str, blocks: list | None = None, thread_ts: str | None = None
) -> dict:
    """Post to Slack via the Web API and return the response (carries the message ``ts``)."""
    from slack_sdk import WebClient

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return client.chat_postMessage(channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text or "").strip()


def _dispatch_followup(event: dict) -> None:
    """Route a Slack thread message to the session that owns the thread, off the 3s-ack path."""
    text = _strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event.get("channel")
    if not (text and thread_ts and channel):
        return
    session_id = ThreadSessionMap(thread_sessions).lookup(thread_ts)
    if session_id is None:
        return  # not a thread we own
    answer_followup.spawn(channel, thread_ts, session_id, text)


@app.function(schedule=modal.Cron("0 9 * * *"), secrets=[secret], timeout=60 * 60)
def daily_report() -> None:
    import logging

    from anthropic import Anthropic

    from brf.delivery.slack import markdown_to_blocks
    from cron.pipeline import run_daily_session

    logging.basicConfig(level=logging.INFO)
    report, session_id = run_daily_session(Anthropic(), AGENT_YAML, ENV_YAML)
    channel = os.environ.get("SLACK_CHANNEL", DEFAULT_CHANNEL)
    resp = _post(channel, "Blog Research Feed — daily", blocks=markdown_to_blocks(report))
    ThreadSessionMap(thread_sessions).remember(resp["ts"], session_id)
    logging.info("posted daily report ts=%s session=%s", resp["ts"], session_id)


@app.function(secrets=[secret], timeout=60 * 30)
def answer_followup(channel: str, thread_ts: str, session_id: str, text: str) -> None:
    from anthropic import Anthropic

    from cron.pipeline import AUTO_ARCHIVE_AGENTS
    from session import AgentSession

    reply = AgentSession(Anthropic(), session_id, AUTO_ARCHIVE_AGENTS).ask(text)
    _post(channel, reply or "(没有返回内容)", thread_ts=thread_ts)


@app.function(secrets=[secret])
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def slack_web():
    from fastapi import FastAPI, Request
    from slack_bolt import App as BoltApp
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    bolt = BoltApp(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )

    @bolt.event("app_mention")
    def _on_mention(event, ack):
        ack()
        _dispatch_followup(event)

    @bolt.event("message")
    def _on_message(event, ack):
        ack()
        if event.get("bot_id") or event.get("subtype"):
            return  # ignore the bot's own posts and edits/joins (avoids loops)
        _dispatch_followup(event)

    handler = SlackRequestHandler(bolt)
    web = FastAPI()

    @web.post("/slack/events")
    async def _events(req: Request):
        return await handler.async_handle(req)

    return web
