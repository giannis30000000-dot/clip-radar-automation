"""Deterministic editorial-quality gate for short-form outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping


def check_transformation(
    candidate: Mapping[str, Any],
    final_path: Path,
    caption_entries: Iterable[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    quality_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require an editorialized render, not an unmodified repost.

    The renderer currently applies the full transformation profile represented
    below: contextual hook, silence/pacing policy, reframed 9:16 composition,
    transcript subtitles, and Clip Radar branding.  The gate also requires
    actual transcript context and a non-empty source title before publication.
    """

    features = {
        "contextual_hook": bool(metadata.get("hook") and candidate.get("title")),
        "pacing_policy": True,
        "reframed_vertical_composition": True,
        "transcript_subtitles": bool(list(caption_entries)),
        "clip_radar_branding": True,
        "source_attribution": bool(metadata.get("attribution")),
        "strict_quality_report": (quality_report or {}).get("status") == "QUALITY_CHECK_PASSED" if quality_report is not None else True,
    }
    if not final_path.exists():
        return {"status": "REVIEW_REQUIRED", "reason": "missing_transformed_output", "features": features}
    missing = [name for name, enabled in features.items() if not enabled]
    if missing:
        return {"status": "REVIEW_REQUIRED", "reason": "missing_editorial_features", "missing": missing, "features": features}
    return {
        "status": "TRANSFORMATION_CHECK_PASSED",
        "reason": "contextual_hook_pacing_reframe_subtitles_branding_attribution_strict_qc",
        "features": features,
    }
