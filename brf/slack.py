"""Slack delivery module — post markdown-style messages to Slack incoming webhooks."""
from __future__ import annotations

import datetime as _dt
import re
import sys
from typing import Optional

import httpx

from .config import get_env

_TIMEOUT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# Markdown → Slack mrkdwn
# ---------------------------------------------------------------------------
def _markdown_to_mrkdwn(text: str) -> str:
    """Convert a subset of CommonMark to Slack mrkdwn."""
    # Links: [label](url) → <url|label>. Do this before bold so URLs aren't molested.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Bold: **x** → *x*
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)
    # Headings: leading "# " / "## " / "### " on a line → *...*
    text = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"*\1*", text)
    # Bullets: leading "- " or "* " → "• "
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
    return text


# ---------------------------------------------------------------------------
# Webhook POST helpers
# ---------------------------------------------------------------------------
def _resolve_webhook(webhook_url: Optional[str], webhook_env: str) -> Optional[str]:
    if webhook_url:
        return webhook_url
    return get_env(webhook_env)


def _post(payload: dict, webhook_url: Optional[str], webhook_env: str) -> dict:
    url = _resolve_webhook(webhook_url, webhook_env)
    if not url:
        return {
            "ok": False,
            "status_code": 0,
            "ts": None,
            "error": f"No webhook URL provided and env var {webhook_env} is not set.",
        }
    try:
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        return {"ok": False, "status_code": 0, "ts": None, "error": f"httpx error: {e}"}
    body = (resp.text or "").strip()
    ok = resp.status_code == 200 and body == "ok"
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "ts": None,  # Incoming webhooks don't return a message ts.
        "error": None if ok else body or f"HTTP {resp.status_code}",
    }


def post_message(
    text: str,
    webhook_url: Optional[str] = None,
    webhook_env: str = "SLACK_WEBHOOK_URL",
) -> dict:
    """Posts a message to Slack via incoming webhook.

    text: Markdown-style content (will be converted to Slack mrkdwn).

    Returns {ok: bool, status_code: int, ts: str | None, error: str | None}.
    """
    mrkdwn = _markdown_to_mrkdwn(text)
    return _post({"text": mrkdwn}, webhook_url, webhook_env)


def post_blocks(
    blocks: list[dict],
    webhook_url: Optional[str] = None,
    webhook_env: str = "SLACK_WEBHOOK_URL",
) -> dict:
    """Post using Slack Block Kit blocks for richer formatting."""
    return _post({"blocks": blocks}, webhook_url, webhook_env)


# ---------------------------------------------------------------------------
# Markdown → Block Kit
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"(?m)^(#{1,2})\s+(.+)$")


def _split_on_headings(markdown_text: str) -> list[tuple[Optional[str], str]]:
    """Return [(heading_or_None, body_text)] split on H1/H2 boundaries."""
    matches = list(_HEADING_RE.finditer(markdown_text))
    if not matches:
        return [(None, markdown_text)]

    sections: list[tuple[Optional[str], str]] = []
    # Preamble before the first heading, if any.
    first = matches[0]
    if first.start() > 0:
        pre = markdown_text[: first.start()].strip()
        if pre:
            sections.append((None, pre))

    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown_text)
        body = markdown_text[body_start:body_end].strip()
        sections.append((heading, body))
    return sections


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Hard-split text on paragraph boundaries so each chunk is <= max_chars."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = para if not current else current + "\n\n" + para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(para) <= max_chars:
                current = para
            else:
                # Single paragraph too large — slice by chars.
                for i in range(0, len(para), max_chars):
                    piece = para[i : i + max_chars]
                    if i + max_chars >= len(para):
                        current = piece
                    else:
                        chunks.append(piece)
                        current = ""
    if current:
        chunks.append(current)
    return chunks


def markdown_to_blocks(markdown_text: str, max_section_chars: int = 2900) -> list[dict]:
    """Convert markdown text to Slack Block Kit blocks.
    Splits on H1/H2 boundaries. Each section becomes a section block with mrkdwn.
    Slack mrkdwn rules: **bold** → *bold*, no _underscore_ italic abuse, links as <url|label>.
    """
    sections = _split_on_headings(markdown_text)
    blocks: list[dict] = []

    # Header block from the first heading, if present.
    first_heading: Optional[str] = None
    for heading, _body in sections:
        if heading:
            first_heading = heading
            break
    if first_heading:
        # plain_text has a 150-char limit; truncate defensively.
        header_text = first_heading[:150]
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": header_text}})

    consumed_header = False
    for heading, body in sections:
        parts: list[str] = []
        if heading:
            if heading == first_heading and not consumed_header:
                consumed_header = True  # Already rendered as header block; skip duplicate bold.
            else:
                parts.append(f"*{heading}*")
        if body:
            parts.append(body)
        if not parts:
            continue
        section_md = "\n\n".join(parts)
        section_mrkdwn = _markdown_to_mrkdwn(section_md)

        for chunk in _hard_split(section_mrkdwn, max_section_chars):
            chunk = chunk.strip()
            if not chunk:
                continue
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
            )

    today = _dt.date.today().isoformat()
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Generated {today} via Blog-Research-Feed",
                }
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# CLI entry: python -m brf.slack "test message"
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    msg = sys.argv[1] if len(sys.argv) > 1 else "test message"
    import json as _json

    print(_json.dumps(post_message(msg), ensure_ascii=False))
