"""Daily orchestrator for the Blog-Research-Feed Managed Agent.

Creates a session, opens the SSE event stream, sends the kickoff user
message, and dispatches `agent.custom_tool_use` events to local `brf`
module functions. Results are returned as JSON-stringified
`user.custom_tool_result` events.

Run via ``python -m brf daily`` (see ``brf.main``).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import signal
import sys
from typing import Any

from .config import get_env

LOG = logging.getLogger("brf.daily")

# Hard wall-clock cap for the whole orchestration loop.
HARD_TIMEOUT_SECONDS = 30 * 60

# Env vars that must be present in the orchestrator process. The hosted
# agent has no access to these — the orchestrator reads them and uses
# them to fulfill custom tool calls.
PASSTHROUGH_KEYS = (
    "FIRECRAWL_API_KEY",
    "X_BEARER_TOKEN",
    "SLACK_WEBHOOK_URL",
    "OPENAI_API_KEY",
)


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    logging.Formatter.converter = __import__("time").gmtime


# ---------------------------------------------------------------------------
# Custom tool dispatcher
# ---------------------------------------------------------------------------
def _dispatch_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Invoke the local `brf` function matching the agent's tool name.

    Returns a JSON-serializable value (or list). Raises on unrecoverable
    Python errors; the caller wraps these into `is_error` results.
    """
    if name == "fetch_rss_recent":
        from . import rss

        since_raw = tool_input.get("since_date") or tool_input.get("since")
        since = _parse_iso(since_raw) if since_raw else None
        return rss.fetch_recent(since=since)

    if name == "fetch_x_user":
        from . import x_client

        handle = tool_input["handle"]
        since_raw = tool_input.get("since_date") or tool_input.get("since")
        since = _parse_iso(since_raw) if since_raw else None
        return x_client.fetch_user_recent(handle, since=since)

    if name == "firecrawl_scrape":
        from . import firecrawl_client

        return firecrawl_client.scrape(tool_input["url"])

    if name == "firecrawl_search":
        from . import firecrawl_client

        return firecrawl_client.search(
            tool_input["query"], limit=int(tool_input.get("limit", 10))
        )

    if name == "fetch_youtube_transcript":
        from . import youtube

        return youtube.get_transcript(tool_input["url"])

    if name == "fetch_podcast_transcript":
        from . import podcast  # type: ignore[attr-defined]

        return podcast.get_transcript(
            tool_input["rss_url"],
            episode_index=int(tool_input.get("episode_index", 0)),
        )

    if name == "post_to_slack":
        from . import slack  # type: ignore[attr-defined]

        report_md = tool_input["report_markdown"]
        blocks = slack.markdown_to_blocks(report_md)
        return slack.post_blocks(blocks)

    return {"error": f"unknown_tool: {name}"}


def _parse_iso(value: str) -> _dt.datetime:
    """Parse an ISO8601 date or datetime string into a datetime."""
    # Accept both pure dates and full timestamps.
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _dt.datetime.strptime(value, "%Y-%m-%d")


def _safe_dispatch(name: str, tool_input: dict[str, Any]) -> tuple[Any, bool]:
    """Wrap dispatch in a try/except, returning (payload, is_error)."""
    try:
        return _dispatch_tool(name, tool_input or {}), False
    except Exception as exc:  # noqa: BLE001 — orchestrator must not crash
        LOG.exception("tool %s raised", name)
        return {"error": f"{type(exc).__name__}: {exc}"}, True


# ---------------------------------------------------------------------------
# Hard timeout
# ---------------------------------------------------------------------------
class _Timeout(RuntimeError):
    pass


def _arm_timeout(seconds: int) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        raise _Timeout(f"orchestrator exceeded {seconds}s hard cap")

    # signal.alarm is POSIX-only and main-thread-only, which is fine for cron.
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _disarm_timeout() -> None:
    try:
        signal.alarm(0)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(dry_run: bool = False) -> None:
    """Run the daily orchestration loop."""
    _setup_logging()
    today = _today_iso()

    agent_id = get_env("ANTHROPIC_AGENT_ID", required=not dry_run)
    env_id = get_env("ANTHROPIC_ENV_ID", required=not dry_run)
    # The Anthropic client picks ANTHROPIC_API_KEY up from env automatically;
    # we still assert it's set so failures are loud and early.
    get_env("ANTHROPIC_API_KEY", required=not dry_run)

    # Surface which pass-through keys are present — useful for debugging cron.
    for key in PASSTHROUGH_KEYS:
        present = bool(os.environ.get(key))
        LOG.info("passthrough %s: %s", key, "set" if present else "MISSING")

    session_params = {
        "agent": {"type": "agent", "id": agent_id},
        "environment_id": env_id,
        "title": f"Daily aggregation {today}",
    }

    if dry_run:
        LOG.info("--dry-run: session.create params=%s", json.dumps(session_params, default=str))
        print(json.dumps(session_params, indent=2, default=str))
        return

    # Defer import so `--dry-run` works without the SDK installed.
    from anthropic import Anthropic

    client = Anthropic()
    LOG.info("creating session for agent=%s env=%s", agent_id, env_id)
    session = client.beta.sessions.create(**session_params)
    LOG.info("session created id=%s", session.id)

    kickoff_text = (
        f"今天是 {today}. 拉取过去24小时的内容，按照系统提示词的指示完成"
        f"今日 AI 新闻聚合：调用 fetch_rss_recent，筛选并 firecrawl_scrape，"
        f"必要时 fetch_x_user / 转写音视频，最后 post_to_slack 一次。"
    )

    _arm_timeout(HARD_TIMEOUT_SECONDS)
    # Track each custom_tool_use event so we can resolve `requires_action`
    # stop reasons by event_id.
    tool_events: dict[str, Any] = {}

    try:
        # IMPORTANT: open the stream BEFORE sending the kickoff message so we
        # don't race and miss early events (see events-and-streaming.md).
        with client.beta.sessions.events.stream(session.id) as stream:
            client.beta.sessions.events.send(
                session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": kickoff_text}],
                    }
                ],
            )
            LOG.info("kickoff sent; streaming events")

            for event in stream:
                etype = getattr(event, "type", None)
                LOG.info("event %s", etype)

                if etype == "agent.message":
                    for block in getattr(event, "content", []) or []:
                        if getattr(block, "type", None) == "text":
                            LOG.info("agent.message: %s", _truncate(block.text))

                elif etype == "agent.thinking":
                    # Optional debug log; truncate to keep stderr sane.
                    text = ""
                    for block in getattr(event, "content", []) or []:
                        text += getattr(block, "text", "") or ""
                    LOG.debug("agent.thinking: %s", _truncate(text, 200))

                elif etype == "agent.custom_tool_use":
                    tool_events[event.id] = event
                    LOG.info(
                        "custom_tool_use id=%s name=%s",
                        event.id,
                        getattr(event, "name", "?"),
                    )

                elif etype == "session.status_idle":
                    stop = getattr(event, "stop_reason", None)
                    stop_type = getattr(stop, "type", None) if stop else None
                    LOG.info("status_idle stop_reason=%s", stop_type)
                    if stop_type == "requires_action":
                        event_ids = list(getattr(stop, "event_ids", []) or [])
                        for eid in event_ids:
                            tev = tool_events.get(eid)
                            if tev is None:
                                LOG.warning(
                                    "requires_action references unknown event %s; sending error",
                                    eid,
                                )
                                _send_tool_result(
                                    client,
                                    session.id,
                                    eid,
                                    {"error": "tool_use event not seen"},
                                    is_error=True,
                                )
                                continue
                            tool_input = _normalize_input(getattr(tev, "input", {}) or {})
                            name = getattr(tev, "name", "")
                            LOG.info("dispatching %s(%s)", name, _truncate(json.dumps(tool_input, default=str), 200))
                            result, is_err = _safe_dispatch(name, tool_input)
                            _send_tool_result(client, session.id, eid, result, is_error=is_err)
                    else:
                        # end_turn, max_tokens, etc. — agent is done.
                        break

                elif etype == "session.status_terminated":
                    LOG.warning("session terminated")
                    break

                elif etype == "session.error":
                    err = getattr(event, "error", None)
                    msg = getattr(err, "message", None) if err else "unknown"
                    LOG.error("session.error: %s", msg)
                    break

                # All other event types (span.*, status_running, etc.) are
                # informational — already logged above.
    except _Timeout as exc:
        LOG.error("hard timeout reached: %s", exc)
    finally:
        _disarm_timeout()


def _normalize_input(value: Any) -> dict[str, Any]:
    """Best-effort coercion of an event input into a plain dict."""
    if isinstance(value, dict):
        return value
    # Pydantic-ish models from the SDK
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return {}


def _send_tool_result(
    client: Any,
    session_id: str,
    tool_use_id: str,
    result: Any,
    is_error: bool = False,
) -> None:
    """Send a `user.custom_tool_result` event with a JSON-stringified body."""
    text = json.dumps(result, default=str)
    payload: dict[str, Any] = {
        "type": "user.custom_tool_result",
        "custom_tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        payload["is_error"] = True
    LOG.info(
        "sending tool_result id=%s is_error=%s bytes=%d",
        tool_use_id,
        is_error,
        len(text),
    )
    client.beta.sessions.events.send(session_id, events=[payload])


def _truncate(text: str, limit: int = 500) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"
