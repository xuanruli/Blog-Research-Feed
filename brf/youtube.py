"""YouTube transcript fetcher.

Primary path: youtube-transcript-api against the public caption track.
Fallback path: yt-dlp downloads bestaudio, then OpenAI Whisper transcribes.
The fallback is what saves us when YouTube IP-bans the caption endpoint
(common on cloud egress) or when an uploader disables captions.

Metadata (title / channel / duration) comes from yt-dlp first, oEmbed
second.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from .config import get_env

_WHISPER_MAX_BYTES = 25 * 1024 * 1024  # OpenAI Whisper API hard limit


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _parse_video_id(url: str) -> Optional[str]:
    """Extract the 11-character video id from a YouTube URL."""
    if not url:
        return None
    # Bare id
    if _VIDEO_ID_RE.match(url):
        return url

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host.endswith("youtu.be"):
        vid = path.lstrip("/").split("/")[0]
        return vid if _VIDEO_ID_RE.match(vid) else None

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        # /watch?v=ID
        if path == "/watch":
            qs = parse_qs(parsed.query)
            vid = (qs.get("v") or [None])[0]
            if vid and _VIDEO_ID_RE.match(vid):
                return vid
        # /shorts/ID, /embed/ID, /v/ID, /live/ID
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if path.startswith(prefix):
                vid = path[len(prefix):].split("/")[0]
                return vid if _VIDEO_ID_RE.match(vid) else None

    return None


def _fetch_metadata_ytdlp(url: str) -> Optional[dict]:
    """Use yt-dlp to extract title/channel/duration. Returns None if unavailable."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except Exception:
        return None
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        return {
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration_seconds": info.get("duration"),
        }
    except Exception:
        return None


def _fetch_metadata_oembed(url: str) -> Optional[dict]:
    """Fallback: public oEmbed endpoint. No duration available."""
    try:
        r = httpx.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            "title": data.get("title"),
            "channel": data.get("author_name"),
            "duration_seconds": None,
        }
    except Exception:
        return None


def _fetch_transcript(video_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Fetch transcript text. Returns (text, status, error_message).

    Status is one of "ok", "no_transcript", "private_or_deleted", "error".
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        from youtube_transcript_api._errors import (  # type: ignore
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )
    except Exception as e:
        return None, "error", f"youtube-transcript-api import failed: {e}"

    # Preferred language order.
    languages = ["en", "en-US", "en-GB", "zh-Hans", "zh-Hant"]

    api = YouTubeTranscriptApi()
    try:
        try:
            # Direct fetch: library handles manual+auto fallback across languages.
            entries = api.fetch(video_id, languages=languages)
        except NoTranscriptFound:
            # Fall back to listing and translating any translatable transcript.
            tlist = api.list(video_id)
            translated = None
            for t in tlist:
                if t.is_translatable:
                    try:
                        translated = t.translate("en").fetch()
                        break
                    except Exception:
                        continue
            if translated is None:
                return None, "no_transcript", "no transcripts available"
            entries = translated

        # Normalize entries (FetchedTranscriptSnippet or dict).
        lines = []
        for e in entries:
            text = getattr(e, "text", None) if not isinstance(e, dict) else e.get("text")
            if text:
                lines.append(text)
        return "\n".join(lines), "ok", None
    except TranscriptsDisabled as e:
        return None, "no_transcript", str(e)
    except VideoUnavailable as e:
        return None, "private_or_deleted", str(e)
    except Exception as e:
        return None, "error", str(e)


def _download_audio_ytdlp(url: str, dest_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Download bestaudio to ``dest_dir``. Returns (path, error_message)."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except Exception as e:
        return None, f"yt-dlp import failed: {e}"

    outtmpl = os.path.join(dest_dir, "audio.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        # Cap download to keep us under the Whisper 25MB ceiling on long videos.
        # bestaudio is usually m4a/webm; ~50kbps audio = ~25MB for ~70min.
        "format_sort": ["+size", "+br"],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            return None, "yt-dlp returned no info"
        # yt-dlp may have transcoded; locate the resulting file.
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            # Fall back to any file in dest_dir.
            for fname in os.listdir(dest_dir):
                if fname.startswith("audio."):
                    path = os.path.join(dest_dir, fname)
                    break
        if not os.path.exists(path):
            return None, "downloaded file not found"
        return path, None
    except Exception as e:
        return None, str(e)


def _transcribe_whisper(path: str, api_key: str) -> tuple[Optional[str], Optional[str]]:
    """POST ``path`` to OpenAI Whisper. Returns (text, error_message)."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, f"stat failed: {e}"
    if size > _WHISPER_MAX_BYTES:
        return None, f"audio {size} bytes exceeds Whisper {_WHISPER_MAX_BYTES} limit"
    try:
        with open(path, "rb") as f:
            files = {"file": (os.path.basename(path) or "audio.m4a", f, "application/octet-stream")}
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
            return None, f"whisper http {r.status_code}: {r.text[:500]}"
        text = r.json().get("text")
        if not text:
            return None, "whisper returned no text"
        return text, None
    except Exception as e:
        return None, str(e)


def _whisper_fallback(url: str) -> tuple[Optional[str], Optional[str]]:
    """Download audio with yt-dlp and transcribe with Whisper.

    Returns (text, error_message). Caller handles missing API key upstream.
    """
    api_key = get_env("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY not set"
    with tempfile.TemporaryDirectory(prefix="brf-yt-") as tmp:
        path, err = _download_audio_ytdlp(url, tmp)
        if not path:
            return None, f"download failed: {err}"
        return _transcribe_whisper(path, api_key)


def get_transcript(url: str) -> dict:
    """Fetch a YouTube video's transcript plus metadata.

    Returns dict with keys: video_id, title, channel, transcript,
    transcript_source, duration_seconds, status, error_message.

    ``transcript_source`` is ``"captions"`` (youtube-transcript-api) or
    ``"whisper"`` (yt-dlp + OpenAI Whisper) when a transcript was obtained,
    else ``None``.
    """
    result: dict = {
        "video_id": None,
        "title": None,
        "channel": None,
        "transcript": None,
        "transcript_source": None,
        "duration_seconds": None,
        "status": "error",
        "error_message": None,
    }

    video_id = _parse_video_id(url)
    if not video_id:
        result["error_message"] = f"could not parse video id from url: {url}"
        return result
    result["video_id"] = video_id

    # Metadata: yt-dlp preferred, oembed fallback.
    meta = _fetch_metadata_ytdlp(url) or _fetch_metadata_oembed(url)
    if meta:
        result["title"] = meta.get("title")
        result["channel"] = meta.get("channel")
        result["duration_seconds"] = meta.get("duration_seconds")

    text, status, err = _fetch_transcript(video_id)
    if status == "ok" and text:
        result["transcript"] = text
        result["transcript_source"] = "captions"
        result["status"] = "ok"
        result["error_message"] = None
        return result

    # Captions path failed. yt-dlp can't recover private/deleted videos,
    # so skip Whisper for those — they'll just fail the same way.
    if status == "private_or_deleted":
        result["status"] = status
        result["error_message"] = err
        return result

    whisper_text, whisper_err = _whisper_fallback(url)
    if whisper_text:
        result["transcript"] = whisper_text
        result["transcript_source"] = "whisper"
        result["status"] = "ok"
        result["error_message"] = None
        return result

    result["status"] = status or "error"
    result["error_message"] = (
        f"captions: {err or 'unknown'}; whisper: {whisper_err or 'unknown'}"
    )
    return result


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print("usage: python -m brf.youtube <youtube-url>", file=sys.stderr)
        sys.exit(2)
    out = get_transcript(sys.argv[1])
    print(json.dumps(out, ensure_ascii=False, indent=2))
