"""Small wrappers around the ffmpeg tools used by Clip Radar.

The GitHub-hosted runner provides ffmpeg/ffprobe.  A local executable can be
selected with FFMPEG_BINARY and FFPROBE_BINARY for repeatable development
tests without changing the pipeline code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaToolError(RuntimeError):
    """Raised when ffmpeg or ffprobe is unavailable or rejects a media file."""


def _configured_binary(name: str) -> str | None:
    env_name = f"{name.upper()}_BINARY"
    configured = os.getenv(env_name)
    if configured:
        return configured
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def ffmpeg_binary() -> str:
    binary = _configured_binary("ffmpeg")
    if not binary:
        raise MediaToolError(
            "ffmpeg is required; install it on the runner or set FFMPEG_BINARY"
        )
    return binary


def ffprobe_binary() -> str:
    binary = _configured_binary("ffprobe")
    if not binary:
        raise MediaToolError(
            "ffprobe is required; install it on the runner or set FFPROBE_BINARY"
        )
    return binary


def probe_media(path: Path) -> dict[str, Any]:
    """Return machine-readable stream and container metadata for *path*."""

    if not path.exists():
        raise MediaToolError(f"media file does not exist: {path}")
    command = [
        ffprobe_binary(),
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or "ffprobe failed").strip().splitlines()[-1]
        raise MediaToolError(f"ffprobe rejected {path.name}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"ffprobe returned invalid JSON for {path.name}") from exc


def media_summary(path: Path) -> dict[str, Any]:
    data = probe_media(path)
    format_data = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "path": str(path),
        "size": int(format_data.get("size") or path.stat().st_size),
        "duration": round(float(format_data.get("duration") or 0), 3),
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "video_codec": (video or {}).get("codec_name"),
        "audio_codec": (audio or {}).get("codec_name"),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }
