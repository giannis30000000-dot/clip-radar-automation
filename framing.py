"""Visual layout classification and importance mapping for Clip Radar.

The source is inspected as pixels before the renderer chooses a composition. A
title or game name can be useful editorial context, but it is never the reason
that a layout is selected. The returned manifest is explicit so rendering, QC,
and human inspection all see the same decision.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from media_tools import ffmpeg_binary, media_summary


LAYOUT_CATEGORIES = (
    "GAMEPLAY_WITH_FACECAM",
    "FULLSCREEN_GAMEPLAY",
    "FACECAM_REACTION",
    "BROWSER_REACTION",
    "GTA_RP_CONVERSATION",
    "MULTI_SUBJECT",
    "UNKNOWN",
)


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


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, float(value))), 4)


def _region(x: float, y: float, width: float, height: float, *, role: str | None = None) -> dict[str, Any]:
    x = _clamp(x, 0.0, 0.96)
    y = _clamp(y, 0.0, 0.96)
    width = _clamp(min(width, 1.0 - x), 0.04, 1.0)
    height = _clamp(min(height, 1.0 - y), 0.04, 1.0)
    result: dict[str, Any] = {"x": x, "y": y, "width": width, "height": height}
    if role:
        result["role"] = role
    return result


def _dark_border_ratio(gray, np) -> float:
    height, width = gray.shape[:2]
    border = max(3, min(height, width) // 18)
    pixels = [gray[:border, :], gray[-border:, :], gray[:, :border], gray[:, -border:]]
    return round(float(np.mean(np.concatenate([item.reshape(-1) for item in pixels]) < 12)), 4)


def _edge_density(gray, cv2, np) -> float:
    edges = cv2.Canny(gray, 80, 160)
    return float(np.mean(edges > 0))


def _facecam_region(face_boxes: list[list[int]], frame_width: int, frame_height: int, np) -> tuple[dict[str, Any] | None, float]:
    """Find a persistent small corner face region, not merely any face."""

    candidates: list[tuple[float, float, float, float]] = []
    for x, y, width, height in face_boxes:
        nw, nh = width / max(frame_width, 1), height / max(frame_height, 1)
        cx, cy = (x + width / 2) / max(frame_width, 1), (y + height / 2) / max(frame_height, 1)
        if nw <= 0.23 and nh <= 0.34 and cy >= 0.38 and (cx <= 0.42 or cx >= 0.58):
            candidates.append((cx, cy, nw, nh))
    if not candidates:
        return None, 0.0
    cx = float(np.median([item[0] for item in candidates]))
    cy = float(np.median([item[1] for item in candidates]))
    face_width = float(np.median([item[2] for item in candidates]))
    face_height = float(np.median([item[3] for item in candidates]))
    region_width = max(0.20, min(0.38, face_width * 2.8))
    region_height = max(0.22, min(0.44, face_height * 3.2))
    result = _region(cx - region_width / 2, cy - region_height / 2, region_width, region_height, role="facecam")
    confidence = _clamp(len(candidates) / 4.0 + min(face_width, 0.18) / 0.18 * 0.25)
    return result, confidence


def _classify(frames: list[dict[str, Any]], facecam: dict[str, Any] | None, aspect: float) -> tuple[str, str]:
    if aspect < 1.15:
        return "UNKNOWN", "portrait_or_near_portrait_source_requires_manual_review"
    average_motion = sum(float(item["motion"]) for item in frames) / max(len(frames), 1)
    max_motion = max((float(item["motion"]) for item in frames), default=0.0)
    average_faces = sum(int(item["face_count"]) for item in frames) / max(len(frames), 1)
    simultaneous_multi = sum(int(item["face_count"]) >= 2 for item in frames) >= max(2, len(frames) // 3)
    browser_score = sum(float(item["browser_score"]) for item in frames) / max(len(frames), 1)
    dominant_face = sum(float(item["dominant_face_ratio"]) for item in frames) / max(len(frames), 1)
    if browser_score >= 0.58 and (average_motion < 0.08 or browser_score >= 0.72) and (facecam or average_faces > 0):
        return "BROWSER_REACTION", "persistent browser-like chrome/text regions with a visible reaction subject"
    if facecam and (max_motion >= 0.105 or average_motion >= 0.055):
        return "GAMEPLAY_WITH_FACECAM", "small persistent corner facecam plus independent high-motion screen content"
    if simultaneous_multi and average_motion < 0.115:
        return "GTA_RP_CONVERSATION", "multiple persistent subjects with dialogue-like low-motion composition"
    if simultaneous_multi:
        return "MULTI_SUBJECT", "multiple subjects remain visible across sampled frames"
    if dominant_face >= 0.20 and average_motion < 0.12:
        return "FACECAM_REACTION", "a dominant face occupies the source and the scene is reaction-led"
    if max_motion >= 0.095:
        return "FULLSCREEN_GAMEPLAY", "high-motion screen content without a reliable separate facecam"
    return "UNKNOWN", "no safe adaptive composition met the visual evidence threshold"


def _profile(category: str, facecam: dict[str, Any] | None, frames: list[dict[str, Any]]) -> dict[str, Any]:
    primary_center = 0.5
    if facecam:
        primary_center = 0.67 if float(facecam["x"]) < 0.5 else 0.33
    average_browser = sum(float(item["browser_score"]) for item in frames) / max(len(frames), 1)
    profiles = {
        "GAMEPLAY_WITH_FACECAM": {"main_x": max(0.18, float(facecam["x"]) + float(facecam["width"]) - 0.02) if facecam and float(facecam["x"]) < 0.5 else 0.0, "main_y": 0.0, "main_w": 0.78 if facecam and float(facecam["x"]) < 0.5 else 0.80, "main_h": 0.98, "panel_y": 330, "panel_h": 900, "occupancy": 0.78, "subtitle_y": 1112, "mask": {"x": 94, "y": 944, "width": 532, "height": 70}},
        "BROWSER_REACTION": {"main_x": max(0.16, float(facecam["x"]) + float(facecam["width"]) + 0.02) if facecam and float(facecam["x"]) < 0.5 else 0.06, "main_y": 0.06, "main_w": 0.78 if facecam and float(facecam["x"]) < 0.5 else 0.88, "main_h": 0.88, "panel_y": 410, "panel_h": 820, "occupancy": 0.75, "subtitle_y": 1110, "mask": {"x": 94, "y": 922, "width": 532, "height": 64}},
        "FACECAM_REACTION": {"main_x": max(0.0, primary_center - 0.40), "main_y": 0.02, "main_w": 0.80, "main_h": 0.96, "panel_y": 105, "panel_h": 1060, "occupancy": 0.86, "subtitle_y": 1110, "mask": {"x": 94, "y": 770, "width": 532, "height": 82}},
        "FULLSCREEN_GAMEPLAY": {"main_x": 0.11, "main_y": 0.02, "main_w": 0.78, "main_h": 0.96, "panel_y": 70, "panel_h": 1130, "occupancy": 0.90, "subtitle_y": 1110, "mask": {"x": 94, "y": 770, "width": 532, "height": 82}},
        "GTA_RP_CONVERSATION": {"main_x": 0.09, "main_y": 0.02, "main_w": 0.82, "main_h": 0.96, "panel_y": 170, "panel_h": 980, "occupancy": 0.82, "subtitle_y": 1100, "mask": {"x": 94, "y": 810, "width": 532, "height": 78}},
        "MULTI_SUBJECT": {"main_x": 0.08, "main_y": 0.02, "main_w": 0.84, "main_h": 0.96, "panel_y": 150, "panel_h": 1000, "occupancy": 0.83, "subtitle_y": 1100, "mask": {"x": 94, "y": 820, "width": 532, "height": 78}},
        "UNKNOWN": {"main_x": 0.0, "main_y": 0.0, "main_w": 1.0, "main_h": 1.0, "panel_y": 438, "panel_h": 405, "occupancy": 0.43, "subtitle_y": 1060, "mask": {"x": 116, "y": 672, "width": 488, "height": 47}},
    }
    result = dict(profiles.get(category, profiles["UNKNOWN"]))
    result["browser_score"] = round(average_browser, 4)
    return result


def analyze_video(path: Path, sample_count: int = 7) -> dict[str, Any]:
    """Return visual signals, adaptive category, importance map, and QC score."""

    cv2, np = _cv()
    summary = media_summary(path)
    duration = float(summary.get("duration") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"cannot analyze a zero-duration video: {path.name}")
    count = max(3, int(sample_count))
    times = [duration * value for value in np.linspace(0.05, 0.95, count)]
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    frames: list[dict[str, Any]] = []
    all_faces: list[list[int]] = []
    previous_gray = None
    representative_facecam: dict[str, Any] | None = None
    facecam_confidences: list[float] = []
    width, height = int(summary.get("width") or 0), int(summary.get("height") or 0)
    for seconds in times:
        frame = extract_frame(path, float(seconds))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24)) if not cascade.empty() else ()
        face_boxes = [[int(x), int(y), int(w), int(h)] for x, y, w, h in faces]
        all_faces.extend(face_boxes)
        edges = _edge_density(gray, cv2, np)
        motion = 0.0 if previous_gray is None else float(np.mean(cv2.absdiff(gray, previous_gray))) / 255.0
        previous_gray = gray
        frame_height, frame_width = gray.shape[:2]
        lower = gray[int(frame_height * 0.58):int(frame_height * 0.82), int(frame_width * 0.16):int(frame_width * 0.84)]
        upper = gray[:max(2, int(frame_height * 0.16)), :]
        lower_edges = _edge_density(lower, cv2, np) if lower.size else edges
        upper_edges = _edge_density(upper, cv2, np) if upper.size else edges
        text_like = _clamp(lower_edges / max(edges, 0.005) * 0.50)
        browser_score = _clamp((upper_edges / max(edges, 0.005)) * 0.35 + text_like * 0.35 + (1.0 - min(1.0, motion * 5.0)) * 0.30)
        dominant_face_ratio = 0.0
        if face_boxes:
            dominant_face_ratio = max(float(item[2] * item[3]) / max(frame_width * frame_height, 1) for item in face_boxes)
        current_facecam, confidence = _facecam_region(face_boxes, frame_width, frame_height, np)
        if current_facecam:
            representative_facecam = current_facecam
            facecam_confidences.append(confidence)
        frames.append({
            "time": round(float(seconds), 3), "face_count": len(face_boxes), "face_boxes": face_boxes,
            "motion": round(motion, 4), "edge_density": round(edges, 4),
            "dark_border_ratio": _dark_border_ratio(gray, np),
            "browser_score": round(browser_score, 4), "dominant_face_ratio": round(dominant_face_ratio, 4),
        })
    aspect = width / height if height else 0.0
    category, reason = _classify(frames, representative_facecam, aspect)
    profile = _profile(category, representative_facecam, frames)
    max_motion = max((float(item["motion"]) for item in frames), default=0.0)
    average_motion = sum(float(item["motion"]) for item in frames) / max(len(frames), 1)
    average_edge_density = round(sum(float(item["edge_density"]) for item in frames) / len(frames), 4)
    average_border_dark = round(sum(float(item["dark_border_ratio"]) for item in frames) / len(frames), 4)
    average_browser = float(profile["browser_score"])
    max_face_ratio = max((float(item["dominant_face_ratio"]) for item in frames), default=0.0)
    zoom_ratio = round(1.0 / max(float(profile["main_w"]), 0.45), 3)
    penalties = {
        "blurred_filler": round(max(0.0, 0.82 - float(profile["occupancy"])) * 42.0, 2),
        "low_content_occupancy": round(max(0.0, 0.68 - float(profile["occupancy"])) * 35.0, 2),
        "small_faces": round(max(0.0, 0.045 - max_face_ratio) * 120.0, 2) if representative_facecam else 0.0,
        "browser_chrome": round(average_browser * 7.0, 2) if category == "BROWSER_REACTION" else 0.0,
        "artificial_bands": 0.0,
        "excessive_zoom": round(max(0.0, zoom_ratio - 1.75) * 28.0, 2),
        "low_motion": round(max(0.0, 0.045 - average_motion) * 320.0, 2),
        "tiny_content": round(max(0.0, 0.60 - float(profile["occupancy"])) * 24.0, 2),
    }
    score = round(max(0.0, 100.0 - sum(penalties.values())), 2)
    source_caption_region = _region(0.18, 0.58, 0.64, 0.14, role="possible_source_caption")
    source_caption_signal = sum(float(item["edge_density"]) for item in frames if float(item["time"]) >= duration * 0.45) / max(len(frames) // 2, 1)
    source_caption_detected = bool(source_caption_signal >= average_edge_density * 0.93 and average_edge_density >= 0.018)
    face_regions = []
    sample_width = 480.0
    sample_height = max(2.0, round(sample_width * height / max(width, 1)))
    if all_faces:
        for x, y, face_width, face_height in all_faces[:12]:
            face_regions.append(_region(x / sample_width, y / sample_height, face_width / sample_width, face_height / sample_height, role="face"))
    content_region = _region(float(profile["main_x"]), float(profile["main_y"]), float(profile["main_w"]), float(profile["main_h"]), role="major_content")
    action_region = content_region.copy()
    action_region["role"] = "motion_or_action_region"
    approved = category != "UNKNOWN" and float(profile["occupancy"]) >= 0.62 and score >= 55.0
    layout_name = {
        "GAMEPLAY_WITH_FACECAM": "adaptive_gameplay_facecam_stack",
        "FULLSCREEN_GAMEPLAY": "adaptive_fullscreen_gameplay_crop",
        "FACECAM_REACTION": "adaptive_facecam_reaction_crop",
        "BROWSER_REACTION": "adaptive_browser_reaction_stack",
        "GTA_RP_CONVERSATION": "adaptive_gta_rp_conversation_crop",
        "MULTI_SUBJECT": "adaptive_multi_subject_crop",
        "UNKNOWN": "review_required_fallback",
    }[category]
    return {
        "schema_version": 3, "path": str(path), "duration": round(duration, 3), "width": width, "height": height,
        "aspect_ratio": round(aspect, 4), "layout_category": category, "layout": layout_name,
        "layout_reason": reason, "layout_approved": approved,
        "facecam_detected": representative_facecam is not None,
        "face_detection_confidence": round(sum(facecam_confidences) / max(len(facecam_confidences), 1), 3),
        "motion_score": round(min(1.0, max_motion), 4), "average_motion_score": round(average_motion, 4),
        "average_edge_density": average_edge_density, "average_dark_border_ratio": average_border_dark,
        "browser_score": round(average_browser, 4), "content_occupancy": round(float(profile["occupancy"]), 3),
        "zoom_ratio": zoom_ratio, "crop_strategy": layout_name,
        "visual_quality_score": score, "visual_quality_threshold": 55.0, "visual_penalties": penalties,
        "important_regions_visible": approved,
        "source_caption_detected": source_caption_detected,
        "region_manifest": {
            "faces": face_regions,
            "facecam": representative_facecam,
            "major_content": content_region,
            "motion_action": action_region,
            "source_caption": {**source_caption_region, "detected": source_caption_detected},
            "subtitle": _region(0.08, 0.78, 0.84, 0.15, role="owned_subtitle_safe_zone"),
            "hook": {"x": 28, "y": 50, "width": 664, "height": 128, "role": "opening_hook"},
        },
        "render_profile": profile,
        "samples": frames,
    }
