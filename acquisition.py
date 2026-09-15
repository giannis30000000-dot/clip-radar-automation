"""Authorized public Twitch clip acquisition for Clip Radar.

This module deliberately uses only the public Twitch clip page through the
maintained yt-dlp Twitch extractor.  It does not load browser cookies, call
private endpoints, bypass authentication, or process a candidate before the
rights gate has selected it.

The provider boundary is intentional: an official creator/editor download
provider or a licensed provider can be added later without changing ranking,
rights, editing, or QC code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from media_tools import MediaToolError, media_summary


class AcquisitionError(RuntimeError):
    """A candidate could not be acquired or did not pass source validation."""


@dataclass(frozen=True)
class AcquisitionResult:
    clip_id: str
    path: Path
    method: str
    width: int
    height: int
    duration: float
    file_size: int
    video_codec: str | None
    audio_codec: str | None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


class MediaProvider(Protocol):
    name: str

    def acquire(self, candidate: Any, output_dir: Path) -> Path:
        """Acquire an authorized source and return its local path."""


def _value(candidate: Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def candidate_clip_id(candidate: Any) -> str:
    clip_id = _value(candidate, "clip_id") or _value(candidate, "id")
    if not clip_id:
        url = _value(candidate, "url", "")
        slug = url.rstrip("/").split("/")[-1].split("?")[0]
        clip_id = slug.rsplit("-", 1)[-1] if "-" in slug else slug
    if not clip_id or len(str(clip_id)) < 6:
        raise AcquisitionError("candidate has no usable Twitch clip ID")
    return str(clip_id)


def twitch_clip_url(candidate: Any) -> str:
    url = str(_value(candidate, "url", "") or "").strip()
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"twitch.tv", "www.twitch.tv", "m.twitch.tv", "clips.twitch.tv"}:
            raise AcquisitionError(f"candidate URL is not a public Twitch URL: {url}")
        if host != "clips.twitch.tv" and "/clip/" not in parsed.path.lower():
            raise AcquisitionError(f"candidate URL is not a Twitch clip URL: {url}")
        return url
    broadcaster = str(
        _value(candidate, "broadcaster_login")
        or _value(candidate, "broadcaster")
        or _value(candidate, "streamer")
        or ""
    ).strip()
    if not broadcaster:
        raise AcquisitionError("candidate has neither a Twitch URL nor broadcaster")
    return f"https://www.twitch.tv/{broadcaster}/clip/{candidate_clip_id(candidate)}"


class TwitchPublicClipProvider:
    """Download the best public progressive MP4 exposed for a Twitch clip."""

    name = "twitch-public-yt-dlp"

    @staticmethod
    def _select_progressive_mp4(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        progressive = [
            item
            for item in formats
            if item.get("ext") == "mp4"
            and item.get("vcodec") not in {None, "none"}
            and item.get("acodec") not in {None, "none"}
            and item.get("url")
        ]
        landscape = [
            item
            for item in progressive
            if int(item.get("width") or 0) >= int(item.get("height") or 0)
        ]
        pool = landscape or progressive
        return max(
            pool,
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                float(item.get("tbr") or 0),
                float(item.get("fps") or 0),
            ),
            default=None,
        )

    def acquire(self, candidate: Any, output_dir: Path) -> Path:
        try:
            import yt_dlp
        except ImportError as exc:
            raise AcquisitionError(
                "yt-dlp is not installed; install requirements.txt before acquisition"
            ) from exc

        clip_id = candidate_clip_id(candidate)
        url = twitch_clip_url(candidate)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / f"{clip_id}_landscape.%(ext)s")
        options = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 2,
            "fragment_retries": 2,
            "continuedl": True,
            "overwrites": True,
            # Never provide cookies or browser credentials here.
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
                selected = self._select_progressive_mp4(info.get("formats", []))
                if selected:
                    downloader.params["format"] = selected["format_id"]
                downloader.extract_info(url, download=True)
        except Exception as exc:
            raise AcquisitionError(f"{self.name} failed for {clip_id}: {exc}") from exc

        files = sorted(output_dir.glob(f"{clip_id}_landscape.*"))
        mp4s = [p for p in files if p.suffix.lower() == ".mp4" and p.is_file()]
        if not mp4s:
            raise AcquisitionError(
                f"{self.name} returned no MP4 for {clip_id} (files={','.join(p.name for p in files)})"
            )
        return max(mp4s, key=lambda path: path.stat().st_size)


def validate_landscape_source(path: Path, clip_id: str, method: str) -> AcquisitionResult:
    try:
        details = media_summary(path)
    except MediaToolError as exc:
        raise AcquisitionError(f"source validation failed for {clip_id}: {exc}") from exc
    if not details["has_video"]:
        raise AcquisitionError(f"source validation failed for {clip_id}: no_video")
    if not details["has_audio"]:
        raise AcquisitionError(f"source validation failed for {clip_id}: no_audio")
    if details["width"] <= details["height"]:
        raise AcquisitionError(
            f"source validation failed for {clip_id}: not_landscape_{details['width']}x{details['height']}"
        )
    if not 1 <= details["duration"] <= 65:
        raise AcquisitionError(
            f"source validation failed for {clip_id}: bad_duration_{details['duration']}"
        )
    if details["size"] < 50_000:
        raise AcquisitionError(f"source validation failed for {clip_id}: suspiciously_small")
    return AcquisitionResult(
        clip_id=clip_id,
        path=path,
        method=method,
        width=details["width"],
        height=details["height"],
        duration=details["duration"],
        file_size=details["size"],
        video_codec=details["video_codec"],
        audio_codec=details["audio_codec"],
    )


def acquire_candidate(
    candidate: Any,
    output_dir: Path,
    providers: list[MediaProvider] | None = None,
    rights_verified: bool = False,
) -> AcquisitionResult:
    """Try authorized providers and validate the first playable landscape MP4."""

    clip_id = candidate_clip_id(candidate)
    if not rights_verified:
        raise AcquisitionError(
            f"rights gate approval is required before acquiring clip {clip_id}"
        )
    provider_list = providers or [TwitchPublicClipProvider()]
    failures: list[str] = []
    for provider in provider_list:
        try:
            path = provider.acquire(candidate, output_dir)
            return validate_landscape_source(path, clip_id, provider.name)
        except (AcquisitionError, MediaToolError) as exc:
            failures.append(str(exc))
    raise AcquisitionError("; ".join(failures) or f"no provider available for {clip_id}")
