"""Strict machine QC for Clip Radar renders."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from framing import analyze_video
from media_tools import MediaToolError, probe_media


def probe(path: Path):
    return probe_media(path)


def _read_captions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".ass":
        result = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line.split(",", 9)
            if len(fields) < 10:
                continue
            def ass_seconds(value: str) -> float:
                hours, minutes, rest = value.strip().split(":")
                whole, centiseconds = rest.split(".")
                return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(centiseconds) / 100
            text = re.sub(r"\{[^}]+\}", "", fields[9]).replace(r"\N", "\n").strip()
            try:
                start, end = ass_seconds(fields[1]), ass_seconds(fields[2])
            except (ValueError, IndexError):
                continue
            result.append({"start": start, "end": end, "text": text})
        return result
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace").strip())
    result = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        match = re.search(r"([0-9:,]+)\s+-->\s+([0-9:,]+)", lines[1])
        if not match:
            continue
        def seconds(value: str) -> float:
            hours, minutes, rest = value.split(":")
            whole, millis = rest.split(",")
            return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(millis) / 1000
        text = re.sub(r"\{\\[^}]+\}", "", "\n".join(lines[2:]).strip())
        result.append({"start": seconds(match.group(1)), "end": seconds(match.group(2)), "text": text})
    return result


def inspect_final(path: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"schema_version": 2, "path": str(path), "status": "REVIEW_REQUIRED", "checks": {}}
    if not path.exists():
        report["reason"] = "missing_output"
        return report
    try:
        data = probe(path)
    except MediaToolError as exc:
        report["reason"] = str(exc)
        return report
    format_data, streams = data.get("format", {}), data.get("streams", [])
    duration, size = float(format_data.get("duration") or 0), int(format_data.get("size") or path.stat().st_size)
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    report["media"] = {"duration": round(duration, 3), "size": size, "width": (video or {}).get("width"), "height": (video or {}).get("height"), "has_audio": audio is not None}
    report["checks"].update({
        "video": bool(video), "audio": bool(audio), "duration": 8 <= duration <= 65,
        "canvas": bool(video and video.get("width") == 720 and video.get("height") == 1280),
        "size": size >= 250_000,
    })
    if not all(report["checks"].values()):
        report["reason"] = "container_or_canvas_check_failed"
        return report
    manifest_path = manifest_path or path.with_suffix(".render.json")
    if not manifest_path.exists():
        report["reason"] = "missing_render_manifest"
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report["reason"] = "invalid_render_manifest"
        return report
    subtitles = manifest.get("subtitles") or {}
    framing = manifest.get("framing") or {}
    render_profile = manifest.get("render_profile") or {}
    caption_path = Path(str(subtitles.get("file") or path.with_suffix(".srt")))
    captions = _read_captions(caption_path)
    max_lines = max((len(item["text"].splitlines()) for item in captions), default=0)
    max_chars = max((max(len(line) for line in item["text"].splitlines()) for item in captions), default=0)
    overlaps = any(right["start"] < left["end"] - 0.05 for left, right in zip(captions, captions[1:]))
    low_confidence = [float(item.get("confidence", 1.0)) for item in subtitles.get("entries", []) if item.get("confidence") is not None and float(item.get("confidence")) < 0.30]
    total_chars = sum(len(item["text"].replace("\n", " ")) for item in captions)
    density = total_chars / max(duration, 1.0)
    hook_active = float((manifest.get("hook") or {}).get("active_seconds", 0.0) or 0.0)
    report["subtitle"] = {"count": len(captions), "max_lines": max_lines, "max_chars_per_line": max_chars, "density_chars_per_second": round(density, 3), "low_confidence_count": len(low_confidence)}
    report["checks"].update({
        "one_subtitle_system": subtitles.get("subtitle_systems") == 1 and subtitles.get("caption_layers") == 1,
        "hook_not_subtitle": subtitles.get("hook_is_subtitle") is False and (manifest.get("hook") or {}).get("is_subtitle") is False,
        "caption_safe_lines": max_lines <= 2 and max_chars <= 24,
        "caption_timing": bool(captions) and not overlaps and all(0 <= item["start"] < item["end"] <= duration + 0.1 for item in captions),
        "caption_manifest_matches": len(captions) == int(subtitles.get("count", len(captions))),
        "hook_caption_timing_distinct": all(item["start"] >= hook_active for item in captions),
        "caption_density": density <= 22.0,
        "transcription_confidence": bool(captions) and not low_confidence,
        "subtitle_safe_position_configured": (
            subtitles.get("positioning") in {"explicit_ass_bottom_center", "adaptive_ass_bottom_center"}
            and (subtitles.get("position") or {}).get("anchor") == "bottom_center"
            and int((subtitles.get("position") or {}).get("x", -1)) == 360
            and 900 <= int((subtitles.get("position") or {}).get("y", -1)) <= 1150
        ),
    })
    try:
        visual = analyze_video(path)
        report["visual"] = visual
        report["checks"].update({
            "important_regions_visible": framing.get("important_regions_visible") is True and float(framing.get("content_occupancy", 0.0)) >= 0.62,
            "adaptive_layout_approved": framing.get("layout_approved") is True,
            "no_blind_center_crop": framing.get("crop_strategy") != "blind_center_crop" and float(framing.get("zoom_ratio", 1.0)) <= 2.2,
            "black_bar_control": float(visual.get("average_dark_border_ratio", 1.0)) < 0.85,
            "visual_variation": float(visual.get("average_edge_density", 0.0)) > 0.005,
            "visual_quality_score": float(framing.get("visual_quality_score", 0.0)) >= float(framing.get("visual_quality_threshold", 55.0)),
            "blurred_filler_control": str((render_profile.get("blurred_background_role") or "")).startswith("support_only"),
            "artificial_band_control": float((framing.get("visual_penalties") or {}).get("artificial_bands", 1.0)) <= 0.0,
        })
    except Exception as exc:
        report["visual_error"] = str(exc)
        report["checks"].update({"important_regions_visible": False, "adaptive_layout_approved": False, "no_blind_center_crop": False, "black_bar_control": False, "visual_variation": False, "visual_quality_score": False, "blurred_filler_control": False, "artificial_band_control": False})
    report["checks"]["hook_spatially_distinct"] = (manifest.get("hook") or {}).get("type") == "title_card_drawtext"
    report["checks"]["source_caption_policy_configured"] = (
        render_profile.get("source_caption_policy") in {
            "central_lower_band_masked_before_single_transcript",
            "detected_region_local_blur_before_single_transcript",
        }
        and render_profile.get("source_caption_mask_style") in {"blurred_texture", "blurred_texture_localized"}
        and bool(render_profile.get("source_caption_mask"))
    )
    report["checks"]["pacing_window"] = 8 <= float((manifest.get("content_window") or {}).get("duration", duration)) <= 65
    report["status"] = "QUALITY_CHECK_PASSED" if all(report["checks"].values()) else "REVIEW_REQUIRED"
    report["reason"] = "all_container_subtitle_framing_and_pacing_checks_passed" if report["status"] == "QUALITY_CHECK_PASSED" else "strict_quality_check_failed"
    return report


def validate_final(path: Path, manifest_path: Path | None = None):
    report = inspect_final(path, manifest_path)
    return report["status"] == "QUALITY_CHECK_PASSED", report["reason"]
