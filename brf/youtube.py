"""YouTube transcript fetcher.

Extracts the transcript for a YouTube video using youtube-transcript-api,
and supplements with title/channel metadata via yt-dlp (preferred) or the
public oEmbed endpoint as a fallback.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx


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
    languages = ["en", "en-US", "en-GB"]

    try:
        # Try list_transcripts so we can pick generated as a fallback.
        try:
            tlist = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = None
            # Try manually-created in preferred languages first.
            try:
                transcript = tlist.find_manually_created_transcript(languages)
            except Exception:
                pass
            # Then auto-generated in preferred languages.
            if transcript is None:
                try:
                    transcript = tlist.find_generated_transcript(languages)
                except Exception:
                    pass
            # Then anything, translated to English if possible.
            if transcript is None:
                for t in tlist:
                    transcript = t
                    if t.is_translatable:
                        try:
                            transcript = t.translate("en")
                        except Exception:
                            pass
                    break
            if transcript is None:
                return None, "no_transcript", "no transcripts available"
            entries = transcript.fetch()
        except (TranscriptsDisabled, NoTranscriptFound):
            # Last-ditch direct call.
            try:
                entries = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            except Exception as e:
                return None, "no_transcript", str(e)

        # Normalize entries (FetchedTranscriptSnippet or dict).
        lines = []
        for e in entries:
            text = getattr(e, "text", None) if not isinstance(e, dict) else e.get("text")
            if text:
                lines.append(text)
        return "\n".join(lines), "ok", None
    except VideoUnavailable as e:
        return None, "private_or_deleted", str(e)
    except Exception as e:
        msg = str(e).lower()
        if "unavailable" in msg or "private" in msg or "deleted" in msg:
            return None, "private_or_deleted", str(e)
        if "no transcript" in msg or "disabled" in msg or "could not retrieve" in msg:
            return None, "no_transcript", str(e)
        return None, "error", str(e)


def get_transcript(url: str) -> dict:
    """Fetch a YouTube video's transcript plus metadata.

    Returns dict with keys: video_id, title, channel, transcript,
    duration_seconds, status, error_message.
    """
    result: dict = {
        "video_id": None,
        "title": None,
        "channel": None,
        "transcript": None,
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
    result["transcript"] = text
    result["status"] = status or "error"
    result["error_message"] = err
    return result


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print("usage: python -m brf.youtube <youtube-url>", file=sys.stderr)
        sys.exit(2)
    out = get_transcript(sys.argv[1])
    print(json.dumps(out, ensure_ascii=False, indent=2))
