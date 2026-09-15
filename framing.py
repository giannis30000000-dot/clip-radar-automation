"""Content-aware, preservation-first framing analysis for Clip Radar."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from media_tools import ffmpeg_binary, media_summary


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("opencv-python-headless and numpy are required for framing QC") from exc
    return cv2, np


def extract_frame(path: Path, seconds: float, width: int = 480):
    """Extract one decoded frame through ffmpeg and return an OpenCV image."""

    cv2, np = _cv()
    result = subprocess.run(
        [
            ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, seconds):.3f}",
            "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ],
        capture_output=True,
    )
    if result.returncode or not result.stdout:
        detail = (result.stderr or b"frame extraction failed").decode(errors="replace").strip()
        raise RuntimeError(f"cannot extract frame from {path.name}: {detail[-300:]}")
    frame = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(f"ffmpeg returned an undecodable frame for {path.name}")
    return frame


def _dark_border_ratio(gray, np) -> float:
    height, width = gray.shape[:2]
    border = max(3, min(height, width) // 18)
    pixels = [gray[:border, :], gray[-border:, :], gray[:, :border], gray[:, -border:]]
    return round(float(np.mean(np.concatenate([item.reshape(-1) for item in pixels]) < 12)), 4)


def analyze_video(path: Path, sample_count: int = 7) -> dict[str, Any]:
    """Return visual signals and a framing decision for *path*."""

    cv2, np = _cv()
    summary = media_summary(path)
    duration = float(summary.get("duration") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"cannot analyze a zero-duration video: {path.name}")
    count = max(3, int(sample_count))
    times = [duration * value for value in np.linspace(0.05, 0.95, count)]
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    frames: list[dict[str, Any]] = []
    previous_gray = None
    all_faces: list[list[int]] = []
    for seconds in times:
        frame = extract_frame(path, float(seconds))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24)) if not cascade.empty() else ()
        face_boxes = [[int(x), int(y), int(w), int(h)] for x, y, w, h in faces]
        all_faces.extend(face_boxes)
        edges = cv2.Canny(gray, 80, 160)
        motion = 0.0 if previous_gray is None else float(np.mean(cv2.absdiff(gray, previous_gray))) / 255.0
        previous_gray = gray
        frames.append({
            "time": round(float(seconds), 3), "face_count": len(face_boxes), "face_boxes": face_boxes,
            "motion": round(motion, 4), "edge_density": round(float(np.mean(edges > 0)), 4),
            "dark_border_ratio": _dark_border_ratio(gray, np),
        })
    width, height = int(summary.get("width") or 0), int(summary.get("height") or 0)
    aspect = width / height if height else 0.0
    face_detected = bool(all_faces)
    max_motion = max((float(item["motion"]) for item in frames), default=0.0)
    average_edge_density = round(sum(float(item["edge_density"]) for item in frames) / len(frames), 4)
    average_border_dark = round(sum(float(item["dark_border_ratio"]) for item in frames) / len(frames), 4)
    if width <= height:
        layout = "portrait_source_preserve"
        reason = "source_is_already_portrait; preserve source geometry"
    elif face_detected:
        layout = "landscape_facecam_gameplay_preserve"
        reason = "face and gameplay regions may both matter; preserve complete landscape frame"
    else:
        layout = "landscape_full_frame_preserve"
        reason = "no reliable face region; preserve complete landscape frame"
    return {
        "schema_version": 1, "path": str(path), "duration": round(duration, 3), "width": width, "height": height,
        "aspect_ratio": round(aspect, 4), "facecam_detected": face_detected,
        "face_detection_confidence": round(min(1.0, len(all_faces) / max(1, len(frames))), 3),
        "motion_score": round(min(1.0, max_motion), 4), "average_edge_density": average_edge_density,
        "average_dark_border_ratio": average_border_dark, "important_regions_visible": True,
        "layout": layout, "layout_reason": reason, "crop_strategy": "none", "zoom_ratio": 1.0,
        "samples": frames,
    }
