"""Clip Radar's acquisition -> edit -> QC -> gated publication orchestrator.

Publishing is still inert unless explicitly requested by configuration.  The
normal Action run uses Buffer dry-run mode to create a sanitized plan without
making any external post mutation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from acquisition import AcquisitionError, acquire_candidate, candidate_clip_id
from content_safety import check_third_party_content
from dedupe import DedupeStore
from media_processor import render_vertical
from pipeline import Candidate, eligible_for_edit
from publish_plan import build_metadata
from publication_state import PublicationLedger
from publication_types import ValidatedClip
from publisher_factory import configured_backend, create_publisher
from quality_control import validate_final
from rights_gate import pick_eligible, rights_evidence
from scanner import print_candidates, scan_candidates
from transformation_check import check_transformation


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


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _record_publication_state(
    ledger: PublicationLedger | None,
    raw: dict[str, Any],
    status: str,
    **metadata: Any,
) -> None:
    if not ledger:
        return
    clip_id = candidate_clip_id(raw)
    if not clip_id:
        return
    existing = ledger.get(clip_id) or {}
    if existing.get("status") in {"QUEUED", "PUBLISHING", "PUBLISHED"} and status in {"DISCOVERED", "ELIGIBLE"}:
        return
    ledger.upsert_clip(
        clip_id,
        status,
        broadcaster=str(raw.get("streamer") or raw.get("broadcaster_name") or ""),
        source_url=str(raw.get("url") or ""),
        **metadata,
    )


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
    publishing_enabled = _env_bool("PUBLISHING_ENABLED")
    publishing_dry_run = _env_bool("PUBLISHING_DRY_RUN")
    publishing_backend = configured_backend()
    publisher = None
    publication_ledger = None
    if publishing_enabled or publishing_dry_run:
        publisher = create_publisher()
        if publisher is None:
            raise RuntimeError("publishing is enabled but PUBLISHER_BACKEND is not configured")
        publication_ledger = PublicationLedger(
            Path(os.getenv("CLIP_RADAR_PUBLICATION_STATE_FILE", "state/publications.json"))
        )
    now_candidates = scan_candidates()
    print_candidates(now_candidates)
    eligible, skipped = pick_eligible(now_candidates, limit=max_candidates)
    for item in skipped:
        candidate = item["candidate"]
        _record_publication_state(
            publication_ledger,
            candidate,
            "SKIPPED",
            skip_reason=item["reason"],
        )
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
        "publishing_enabled": publishing_enabled,
        "publishing_dry_run": publishing_dry_run,
        "publishing_backend": publishing_backend,
        "publishing_plans": [],
    }
    if publisher:
        summary["publishing"] = {
            "configuration": publisher.config.safe_summary(),
            "plans": [],
            "results": [],
        }
    for raw in eligible:
        clip_id = candidate_clip_id(raw)
        candidate = _as_candidate(raw)
        retry_failed_publication = bool(
            publisher
            and publication_ledger
            and (
                publication_ledger.has_failed_network(clip_id)
                or (
                    publishing_dry_run
                    and publication_ledger.needs_publication_retry(clip_id)
                )
            )
        )
        if store.contains(clip_id) and not retry_failed_publication:
            print(f"dedupe | SKIP | {clip_id} | already_prepared_or_published")
            summary["attempts"].append({"clip_id": clip_id, "status": "DEDUPED"})
            continue
        _record_publication_state(publication_ledger, raw, "DISCOVERED")
        _record_publication_state(publication_ledger, raw, "ELIGIBLE")
        edit_ok, edit_reason = eligible_for_edit(candidate)
        if not edit_ok:
            _record_publication_state(publication_ledger, raw, "SKIPPED", skip_reason=edit_reason)
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
            _record_publication_state(
                publication_ledger,
                raw,
                "ACQUIRED",
                acquisition_method=acquired.method,
                source=str(acquired.path),
            )
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
            caption_entries = render_vertical(
                acquired.path,
                final_path,
                hook=f"{candidate.streamer}: {candidate.title}"[:55],
            )
            _record_publication_state(
                publication_ledger,
                raw,
                "RENDERED",
                final_output_identifier=str(final_path),
            )
            ok, reason = validate_final(final_path)
            if not ok:
                final_path.unlink(missing_ok=True)
                raise AcquisitionError(f"final QC failed: {reason}")
            metadata = build_metadata(
                candidate.streamer,
                candidate.title,
                game_name=str(raw.get("game_name") or ""),
                source_url=candidate.url,
                transcript_entries=caption_entries,
            )
            third_party_check = check_third_party_content(raw, caption_entries)
            transformation_check = check_transformation(raw, final_path, caption_entries, metadata)
            attempt.update(
                {
                    "status": "READY",
                    "final": str(final_path),
                    "final_validation": reason,
                    "metadata": metadata,
                    "third_party_check": third_party_check,
                    "transformation_check": transformation_check,
                }
            )
            if third_party_check["status"] != "THIRD_PARTY_CHECK_PASSED" or transformation_check["status"] != "TRANSFORMATION_CHECK_PASSED":
                attempt["status"] = "REVIEW_REQUIRED"
                _record_publication_state(
                    publication_ledger,
                    raw,
                    "REVIEW_REQUIRED",
                    rights_basis=rights_evidence(raw).get("rights_basis"),
                    third_party_check=third_party_check,
                    transformation_check=transformation_check,
                )
                final_path.unlink(missing_ok=True)
                summary["attempts"].append(attempt)
                print(f"safety | REVIEW_REQUIRED | clip_id={clip_id} | third_party={third_party_check['status']} | transformation={transformation_check['status']}")
                continue
            store.record(
                clip_id,
                "prepared",
                broadcaster=candidate.streamer,
                source=str(acquired.path),
                final=str(final_path),
                viral_score=candidate.score,
            )
            _record_publication_state(
                publication_ledger,
                raw,
                "QC_PASSED",
                final_output_identifier=str(final_path),
                viral_score=candidate.score,
                views=candidate.views,
                game=str(raw.get("game_name") or ""),
            )
            summary["outputs"].append(attempt)
            summary["attempts"].append(attempt)
            print(
                f"qc | PASS | clip_id={clip_id} | final={final_path} | "
                "resolution=720x1280 | audio=present | subtitles=present | branding=present"
            )
            if publisher and publication_ledger:
                validated_clip = ValidatedClip(
                    candidate=raw,
                    final_path=final_path,
                    qc_status=reason,
                    rights_basis=rights_evidence(raw)["rights_basis"],
                    third_party_check=third_party_check["status"],
                    transformation_check=transformation_check["status"],
                )
                plan = publisher.build_plan(validated_clip, metadata)
                publication_ledger.upsert_clip(
                    clip_id,
                    "PUBLISH_ELIGIBLE",
                    broadcaster=candidate.streamer,
                    source_url=candidate.url,
                    final_output_identifier=str(final_path),
                    publication_timestamp=None,
                    viral_score=candidate.score,
                    views=candidate.views,
                    game=str(raw.get("game_name") or ""),
                    publication_slot=plan["publication_slot"],
                    rights_basis=validated_clip.rights_basis,
                    third_party_check=validated_clip.third_party_check,
                    transformation_check=validated_clip.transformation_check,
                )
                summary["publishing_plans"].append(plan)
                summary["publishing"]["plans"].append(plan)
                if publishing_enabled:
                    result = publisher.publish(validated_clip, metadata, publication_ledger, plan=plan)
                    summary["publishing"]["results"].append(result)
                    attempt["publication"] = result
            if len(summary["outputs"]) >= max_outputs:
                break
        except Exception as exc:
            _record_publication_state(publication_ledger, raw, "FAILED", error=str(exc))
            attempt["reason"] = str(exc)
            print(f"candidate | FAIL | clip_id={clip_id} | reason={exc}")
            summary["attempts"].append(attempt)

    if summary["outputs"]:
        summary["status"] = "READY"
    elif not eligible:
        summary["status"] = "NO_ELIGIBLE_CANDIDATES"
    publishing_plan_path = output_dir / "publishing_plan.json"
    if publisher:
        _write_summary(publishing_plan_path, {
            "schema_version": 1,
            "status": "DRY_RUN_READY" if publishing_dry_run and not publishing_enabled else "QUEUED",
            "plans": summary["publishing_plans"],
            "configuration": publisher.config.safe_summary(),
            "live_request_sent": bool(summary["publishing"].get("results")),
        })
    else:
        publishing_plan_path.unlink(missing_ok=True)
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
            "publishing_enabled": _env_bool("PUBLISHING_ENABLED"),
            "publishing_dry_run": _env_bool("PUBLISHING_DRY_RUN"),
        }
        _write_summary(output_dir / "run_summary.json", summary)
        print(f"run | status=BLOCKED | reason={exc} | publishing_enabled={_env_bool('PUBLISHING_ENABLED')}")
        raise SystemExit(2) from exc
    print(f"run | status={result['status']} | outputs={len(result['outputs'])} | publishing_enabled={result['publishing_enabled']}")
