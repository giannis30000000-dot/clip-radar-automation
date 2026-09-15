"""Small persistent audio/visual fingerprints for source and final dedupe."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Iterable

from framing import extract_frame
from media_tools import ffmpeg_binary, media_summary


def _cv():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("numpy is required for video fingerprints") from exc
    return np


def _average_hash(frame, cv2, np) -> str:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    bits = (small >= float(np.mean(small))).reshape(-1)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return f"{value:064x}"


def _color_histogram(frame, cv2, np) -> list[int]:
    """Return a compact global HSV signature for one sampled frame."""

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [12, 4], [0, 180, 0, 256])
    histogram = cv2.normalize(histogram, None, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)
    return [int(max(0, min(255, round(float(value) * 255)))) for value in histogram.reshape(-1)]


def _audio_signature(path: Path, np) -> list[int]:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        capture_output=True,
    )
    if result.returncode or not result.stdout:
        return []
    audio = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
    if not len(audio):
        return []
    chunks = np.array_split(audio, 64)
    rms = np.array([float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0 for chunk in chunks])
    peak = max(float(np.max(rms)), 1.0)
    return [int(max(0, min(255, round(value / peak * 255)))) for value in rms]


def video_fingerprint(path: Path, sample_count: int = 8) -> dict[str, Any]:
    """Fingerprint decoded frames and mono audio, never just the filename."""

    np = _cv()
    import cv2
    summary = media_summary(path)
    duration = float(summary.get("duration") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"cannot fingerprint a zero-duration video: {path.name}")
    times = [duration * value for value in np.linspace(0.08, 0.92, max(4, int(sample_count)))]
    sampled_frames = [extract_frame(path, float(seconds)) for seconds in times]
    hashes = [_average_hash(frame, cv2, np) for frame in sampled_frames]
    color_histograms = [_color_histogram(frame, cv2, np) for frame in sampled_frames]
    return {
        "schema_version": 2, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "duration": round(duration, 3),
        "frame_hashes": hashes, "frame_color_histograms": color_histograms,
        "audio_signature": _audio_signature(path, np),
    }


def _hamming(left: str, right: str) -> float:
    try:
        value = (int(left, 16) ^ int(right, 16)).bit_count()
        width = max(len(left), len(right)) * 4
        return 1.0 - value / max(1, width)
    except (TypeError, ValueError):
        return 0.0


def _aligned_frame_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    left_values, right_values = list(left), list(right)
    if not left_values or not right_values:
        return 0.0
    pairs = []
    for index, value in enumerate(left_values):
        other = right_values[round(index * (len(right_values) - 1) / max(1, len(left_values) - 1))]
        pairs.append(_hamming(value, other))
    return sum(pairs) / len(pairs)


def _aligned_histogram_similarity(left: Iterable[Iterable[int]], right: Iterable[Iterable[int]]) -> float:
    left_values, right_values = list(left), list(right)
    if not left_values or not right_values:
        return 0.0
    pairs = []
    for index, values in enumerate(left_values):
        other = right_values[round(index * (len(right_values) - 1) / max(1, len(left_values) - 1))]
        if not values or not other:
            continue
        length = min(len(values), len(other))
        distance = sum(abs(int(values[i]) - int(other[i])) for i in range(length))
        pairs.append(1.0 - distance / max(1.0, 2.0 * 255.0))
    return sum(pairs) / len(pairs) if pairs else 0.0


def fingerprint_similarity(left: dict[str, Any], right: dict[str, Any]) -> dict[str, float | bool]:
    frame = _aligned_frame_similarity(left.get("frame_hashes", []), right.get("frame_hashes", []))
    color = _aligned_histogram_similarity(left.get("frame_color_histograms", []), right.get("frame_color_histograms", []))
    left_audio, right_audio = left.get("audio_signature", []), right.get("audio_signature", [])
    if left_audio and right_audio:
        length = min(len(left_audio), len(right_audio))
        audio = 1.0 - sum(abs(left_audio[i] - right_audio[i]) for i in range(length)) / (255.0 * length)
    else:
        audio = 0.0
    exact = bool(left.get("sha256") and left.get("sha256") == right.get("sha256"))
    left_duration, right_duration = float(left.get("duration") or 0.0), float(right.get("duration") or 0.0)
    duration_similarity = min(left_duration, right_duration) / max(left_duration, right_duration) if left_duration and right_duration else 0.0
    has_color = bool(left.get("frame_color_histograms") and right.get("frame_color_histograms"))
    if has_color:
        perceptual_duplicate = duration_similarity >= 0.75 and (
            (frame >= 0.965 and color >= 0.94) or
            (frame >= 0.90 and color >= 0.97 and audio >= 0.96)
        )
    else:
        # Legacy v1 records have only average hashes.  Require both a very
        # strong visual match and near-identical audio so generic HUD frames
        # cannot suppress an unrelated clip.
        perceptual_duplicate = frame >= 0.975 and audio >= 0.97
    return {
        "exact": exact, "frame_similarity": round(frame, 4),
        "color_similarity": round(color, 4), "audio_similarity": round(audio, 4),
        "duration_similarity": round(duration_similarity, 4),
        "duplicate": exact or perceptual_duplicate,
    }
