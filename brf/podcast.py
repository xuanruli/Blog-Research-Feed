"""Podcast episode transcriber.

Parses a podcast RSS feed, downloads the chosen episode's audio, and
transcribes it via the OpenAI Whisper API (`whisper-1`).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Optional

import httpx

from .config import get_env

_MAX_BYTES = 100 * 1024 * 1024  # 100 MB


def _pick_enclosure_url(entry) -> Optional[str]:
    """Find the best audio enclosure URL from a feedparser entry."""
    enclosures = getattr(entry, "enclosures", None) or []
    if enclosures:
        # Prefer audio/* enclosures.
        for enc in enclosures:
            href = enc.get("href") or enc.get("url")
            etype = (enc.get("type") or "").lower()
            if href and etype.startswith("audio"):
                return href
        # Fallback: first enclosure with an href.
        for enc in enclosures:
            href = enc.get("href") or enc.get("url")
            if href:
                return href
    # Some feeds expose links with rel="enclosure".
    for link in getattr(entry, "links", []) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link["href"]
    return None


def _download_audio(url: str, dest_path: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Stream-download `url` to `dest_path`. Returns (ok, status, error_message)."""
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
            if r.status_code != 200:
                return False, "download_failed", f"http {r.status_code}"
            # Check Content-Length up-front if present.
            cl = r.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > _MAX_BYTES:
                return False, "too_large", f"content-length {cl} > {_MAX_BYTES}"
            total = 0
            with open(dest_path, "wb") as f:
                for chunk in r.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        return False, "too_large", f"downloaded > {_MAX_BYTES} bytes"
                    f.write(chunk)
        return True, None, None
    except httpx.HTTPError as e:
        return False, "download_failed", str(e)
    except Exception as e:
        return False, "download_failed", str(e)


def _transcribe_whisper(path: str, api_key: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Upload `path` to OpenAI Whisper. Returns (text, status, error_message)."""
    try:
        with open(path, "rb") as f:
            files = {"file": (os.path.basename(path) or "audio.mp3", f, "application/octet-stream")}
            data = {"model": "whisper-1"}
            headers = {"Authorization": f"Bearer {api_key}"}
            r = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
                timeout=600.0,
            )
        if r.status_code != 200:
            return None, "transcription_failed", f"http {r.status_code}: {r.text[:500]}"
        payload = r.json()
        text = payload.get("text")
        if not text:
            return None, "transcription_failed", f"no text in response: {payload}"
        return text, "ok", None
    except Exception as e:
        return None, "transcription_failed", str(e)


def get_transcript(rss_url: str, episode_index: int = 0) -> dict:
    """Download and transcribe a podcast episode from an RSS feed.

    Returns dict with keys: title, podcast, episode_url, published,
    transcript, status, error_message.
    """
    result: dict = {
        "title": None,
        "podcast": None,
        "episode_url": None,
        "published": None,
        "transcript": None,
        "status": "error",
        "error_message": None,
    }

    try:
        import feedparser  # type: ignore
    except Exception as e:
        result["error_message"] = f"feedparser import failed: {e}"
        return result

    feed = feedparser.parse(rss_url)
    podcast_title = None
    if getattr(feed, "feed", None):
        podcast_title = feed.feed.get("title")
    result["podcast"] = podcast_title

    entries = getattr(feed, "entries", []) or []
    if not entries:
        result["status"] = "no_episodes"
        result["error_message"] = "no entries in feed"
        return result

    if episode_index < 0 or episode_index >= len(entries):
        result["status"] = "no_episodes"
        result["error_message"] = f"episode_index {episode_index} out of range (have {len(entries)})"
        return result

    entry = entries[episode_index]
    result["title"] = entry.get("title")
    result["published"] = entry.get("published") or entry.get("updated")

    audio_url = _pick_enclosure_url(entry)
    result["episode_url"] = audio_url
    if not audio_url:
        result["status"] = "download_failed"
        result["error_message"] = "no audio enclosure on entry"
        return result

    api_key = get_env("OPENAI_API_KEY")
    if not api_key:
        result["status"] = "no_api_key"
        result["error_message"] = "OPENAI_API_KEY not set"
        return result

    # Download to a tempfile, transcribe, clean up.
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        ok, status, err = _download_audio(audio_url, tmp_path)
        if not ok:
            result["status"] = status or "download_failed"
            result["error_message"] = err
            return result

        text, status, err = _transcribe_whisper(tmp_path, api_key)
        if text is None:
            result["status"] = status or "transcription_failed"
            result["error_message"] = err
            return result

        result["transcript"] = text
        result["status"] = "ok"
        result["error_message"] = None
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print("usage: python -m brf.podcast <rss-url> [episode_index]", file=sys.stderr)
        sys.exit(2)
    rss = sys.argv[1]
    idx = int(sys.argv[2]) if len(sys.argv) >= 3 else 0
    out = get_transcript(rss, idx)
    print(json.dumps(out, ensure_ascii=False, indent=2))
