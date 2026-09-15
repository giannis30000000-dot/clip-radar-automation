"""Clip Radar's safe acquisition -> edit -> QC orchestrator.

This workflow prepares artifacts only. There is intentionally no publishing
call here; a human must inspect the source and final artifacts before any
social publishing integration is enabled.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from acquisition import AcquisitionError, acquire_candidate, candidate_clip_id
from dedupe import DedupeStore
from media_processor import render_vertical
from pipeline import Candidate, eligible_for_edit
from publish_plan import build_metadata
from quality_control import validate_final
from rights_gate import pick_eligible
from scanner import print_candidates, scan_candidates


def _as_candidate(raw: dict[str, Any]) -> Candidate:
    return Candidate(
        clip_id=str(raw.get("id") or raw.get("clip_id") or ""),
        streamer=str(raw.get("streamer") or raw.get("broadcaster_name") or ""),
        title=str(raw.get("title") or "Untitled Twitch clip"),
        url=str(raw.get("url") or ""),
        score=float(raw.get("score") or 0),
        views=int(raw.get("view_count") or raw.get("views") or 0),
        duration=float(raw.get("duration") or 0),
    )


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_live(
    output_dir: Path | None = None,
    state_file: Path | None = None,
    max_outputs: int | None = None,
    max_candidates: int | None = None,
) -> dict[str, Any]:
    output_dir = output_dir or Path(os.getenv("CLIP_RADAR_OUTPUT_DIR", "output"))
    state_file = state_file or Path(
        os.getenv("CLIP_RADAR_STATE_FILE", "state/processed_clips.json")
    )
    max_outputs = max_outputs or int(os.getenv("MAX_OUTPUTS", "1"))
    max_candidates = max_candidates or int(os.getenv("MAX_CANDIDATES", "20"))
    source_dir = output_dir / "sources"
    final_dir = output_dir / "final"
    store = DedupeStore(state_file)
    now_candidates = scan_candidates()
    print_candidates(now_candidates)
    eligible, skipped = pick_eligible(now_candidates, limit=max_candidates)
    for item in skipped:
        candidate = item["candidate"]
        print(
            f"rights | SKIP | {candidate.get('id')} | {candidate.get('streamer')} | "
            f"{item['reason']}"
        )

    summary: dict[str, Any] = {
        "status": "NO_SUCCESS",
        "eligible_candidates": len(eligible),
        "rights_skipped": len(skipped),
        "attempts": [],
        "outputs": [],
        "publishing_enabled": False,
    }
    for raw in eligible:
        clip_id = candidate_clip_id(raw)
        candidate = _as_candidate(raw)
        if store.contains(clip_id):
            print(f"dedupe | SKIP | {clip_id} | already_prepared_or_published")
            summary["attempts"].append({"clip_id": clip_id, "status": "DEDUPED"})
            continue
        edit_ok, edit_reason = eligible_for_edit(candidate)
        if not edit_ok:
            print(f"quality | SKIP | {clip_id} | {edit_reason}")
            summary["attempts"].append(
                {"clip_id": clip_id, "status": "SKIP", "reason": edit_reason}
            )
            continue

        print(
            f"candidate | TRY | clip_id={clip_id} | broadcaster={candidate.streamer} | "
            f"title={candidate.title} | views={candidate.views} | age_h={raw.get('age_h', 0):.2f} | "
            f"vph={raw.get('vph', 0):.2f} | duration={candidate.duration} | viral_score={candidate.score}"
        )
        attempt: dict[str, Any] = {"clip_id": clip_id, "status": "FAILED"}
        try:
            acquired = acquire_candidate(raw, source_dir, rights_verified=True)
            attempt.update(
                {
                    "status": "ACQUIRED",
                    "broadcaster": candidate.streamer,
                    "title": candidate.title,
                    "views": candidate.views,
                    "age_hours": raw.get("age_h"),
                    "views_per_hour": raw.get("vph"),
                    "duration": acquired.duration,
                    "viral_score": candidate.score,
                    "acquisition_method": acquired.method,
                    "resolution": acquired.resolution,
                    "file_size": acquired.file_size,
                    "validation": "valid_video_and_audio_landscape_mp4",
                    "source": str(acquired.path),
                }
            )
            print(
                f"acquisition | SUCCESS | clip_id={clip_id} | broadcaster={candidate.streamer} | "
                f"title={candidate.title} | views={candidate.views} | age_h={raw.get('age_h', 0):.2f} | "
                f"vph={raw.get('vph', 0):.2f} | duration={acquired.duration:.3f}s | "
                f"viral_score={candidate.score} | method={acquired.method} | "
                f"resolution={acquired.resolution} | file_size={acquired.file_size} | "
                "validation=valid_video_and_audio_landscape_mp4"
            )
            final_dir.mkdir(parents=True, exist_ok=True)
            final_path = final_dir / f"{clip_id}_clipradar_vertical.mp4"
            render_vertical(acquired.path, final_path, hook=candidate.title[:55])
            ok, reason = validate_final(final_path)
            if not ok:
                final_path.unlink(missing_ok=True)
                raise AcquisitionError(f"final QC failed: {reason}")
            attempt.update(
                {
                    "status": "READY",
                    "final": str(final_path),
                    "final_validation": reason,
                    "metadata": build_metadata(candidate.streamer, candidate.title),
                }
            )
            store.record(
                clip_id,
                "prepared",
                broadcaster=candidate.streamer,
                source=str(acquired.path),
                final=str(final_path),
                viral_score=candidate.score,
            )
            summary["outputs"].append(attempt)
            summary["attempts"].append(attempt)
            print(
                f"qc | PASS | clip_id={clip_id} | final={final_path} | "
                "resolution=720x1280 | audio=present | subtitles=present | branding=present"
            )
            if len(summary["outputs"]) >= max_outputs:
                break
        except Exception as exc:
            attempt["reason"] = str(exc)
            print(f"candidate | FAIL | clip_id={clip_id} | reason={exc}")
            summary["attempts"].append(attempt)

    if summary["outputs"]:
        summary["status"] = "READY"
    elif not eligible:
        summary["status"] = "NO_ELIGIBLE_CANDIDATES"
    _write_summary(output_dir / "run_summary.json", summary)
    return summary


def process_candidates(candidates, output_dir: Path = Path("output")):
    """Compatibility wrapper for callers that already have Candidate objects."""
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    for chosen in ranked:
        ok, reason = eligible_for_edit(chosen)
        print(f"eligibility | {chosen.streamer} | {chosen.title} | score={chosen.score} | {reason}")
        if not ok:
            continue
        if not chosen.authorized_source_url:
            print(f"quality | SKIP | {chosen.clip_id} | no_authorized_source")
            continue
        source = Path(chosen.authorized_source_url)
        final = output_dir / f"{chosen.clip_id}_final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        render_vertical(source, final, hook=chosen.title[:55])
        valid, final_reason = validate_final(final)
        if valid:
            return {"status": "READY", "file": str(final), "metadata": build_metadata(chosen.streamer, chosen.title)}
        final.unlink(missing_ok=True)
        print(f"quality | SKIP | {chosen.clip_id} | {final_reason}")
    return {"status": "SKIP", "reason": "no_high_quality_authorized_source"}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        result = run_live()
    except Exception as exc:
        output_dir = Path(os.getenv("CLIP_RADAR_OUTPUT_DIR", "output"))
        summary = {
            "status": "BLOCKED",
            "reason": str(exc),
            "outputs": [],
            "publishing_enabled": False,
        }
        _write_summary(output_dir / "run_summary.json", summary)
        print(f"run | status=BLOCKED | reason={exc} | publishing_enabled=False")
        raise SystemExit(2) from exc
    print(f"run | status={result['status']} | outputs={len(result['outputs'])} | publishing_enabled=False")
