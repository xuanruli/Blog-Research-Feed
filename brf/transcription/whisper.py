"""Shared OpenAI Whisper transcription: endpoint, 25 MB cap, and the upload POST."""
from __future__ import annotations

import os
from typing import Optional

import httpx

WHISPER_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
WHISPER_MAX_BYTES = 25 * 1024 * 1024  # OpenAI Whisper API hard limit
WHISPER_TIMEOUT_SECS = 600.0


def transcribe(path: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """POST ``path`` to Whisper; return ``(text, error)`` with the 25 MB cap enforced first."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, f"stat failed: {e}"
    if size > WHISPER_MAX_BYTES:
        return None, f"audio {size} bytes exceeds Whisper {WHISPER_MAX_BYTES} limit"
    try:
        with open(path, "rb") as f:
            files = {"file": (os.path.basename(path) or "audio", f, "application/octet-stream")}
            data = {"model": WHISPER_MODEL}
            headers = {"Authorization": f"Bearer {api_key}"}
            r = httpx.post(
                WHISPER_ENDPOINT,
                headers=headers,
                files=files,
                data=data,
                timeout=WHISPER_TIMEOUT_SECS,
            )
        if r.status_code != 200:
            return None, f"whisper http {r.status_code}: {r.text[:500]}"
        text = r.json().get("text")
        if not text:
            return None, "whisper returned no text"
        return text, None
    except Exception as e:
        return None, str(e)
