"""One shared 10-second scene; no story generation, TTS, edits or publishing."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from media_tools import ffprobe_binary, media_summary
from story_engine.costs import BudgetExceeded, CostBudget, sanitize
from story_engine.history import write_json
from story_engine.provider_http import ProviderFailure, endpoint
from story_engine.runway_provider import RunwayVisualProvider, scene_prompt


# Runway developer API USD rates checked 2026-09-21; standard MP4 only.
MODELS = {"gen4.5": ".12", "gen4_turbo": ".05", "h3_max": ".08"}
SCENE = {
    "scene_number": 1, "estimated_duration": 10,
    "video_prompt": "A small teal refrigerator detective slides open a yellow evidence envelope. A single cheese cube rolls out. The detective freezes, glances at the cheese, then quietly closes the envelope. One continuous shot.",
    "visual_description": "A teal refrigerator detective at a tidy wooden kitchen table, a yellow envelope and a cheese cube.",
    "camera_direction": "Locked medium shot, slight slow push-in; all action stays in frame",
    "characters_present": ["detective"],
}
STORY = {
    "characters": [{"character_id": "detective", "name": "Detective", "description": "Small teal refrigerator with two black eyes, tiny arms and a navy tie; no text or logos"}],
    "art_direction": {"style": "expressive polished clay animation, warm soft lighting", "environment": "A tidy miniature kitchen, wooden table, cream walls"},
    "scenes": [SCENE],
}


def compare(output_dir="output/provider-comparison", models=None):
    models = list(MODELS) if models is None else list(models)
    if not models or len(set(models)) != len(models) or any(m not in MODELS for m in models):
        raise ValueError("Choose distinct supported models")
    root = Path(output_dir).resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    # Never inherit the full-story budget or production model/price settings.
    budget = CostBudget(root / "cost_report.json", limit=os.getenv("CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD") or "0")
    retries = int(os.getenv("CLIP_RADAR_PROVIDER_TEST_MAX_ATTEMPTS") or "1")
    if not 1 <= retries <= 3:
        raise ValueError("CLIP_RADAR_PROVIDER_TEST_MAX_ATTEMPTS must be 1-3")
    story, scene = copy.deepcopy(STORY), copy.deepcopy(SCENE)
    image_url = os.getenv("CLIP_RADAR_PROVIDER_TEST_IMAGE_URL", "").strip()
    if image_url:
        scene["reference_image_url"] = endpoint(image_url)
    prompt = scene_prompt(story, scene)
    specification = {"story": story, "scene": scene, "prompt": prompt}
    spec_hash = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()
    planned = sum((Decimal(MODELS[m]) * 10 for m in models), Decimal(0))
    report = {
        "version": 1, "status": "PENDING", "publishing_enabled": False, "scheduling_enabled": False,
        "report_path": str(root / "comparison.json"), "specification": specification,
        "specification_sha256": spec_hash, "planned_first_attempt_cost_usd": float(planned),
        "max_attempts_per_model": retries, "results": [],
    }

    def save():
        report["costs"] = budget.report()
        write_json(root / "comparison.json", sanitize(report))

    blocked = None
    if budget.limit == 0:
        blocked = "PAID_TEST_DISABLED"
    elif not os.getenv("RUNWAYML_API_SECRET", "").strip():
        blocked = "MISSING_CREDENTIALS"
    elif not image_url:
        blocked = "SHARED_REFERENCE_IMAGE_REQUIRED"
    elif planned > budget.limit:
        budget.stopped = True
        budget.event("comparison", "BUDGET_EXCEEDED", planned_first_attempt_cost_usd=float(planned))
        blocked = "BUDGET_EXCEEDED"
    else:
        try:
            ffprobe_binary()  # Missing local validation tooling must not waste a paid call.
        except RuntimeError:
            blocked = "FFPROBE_REQUIRED"

    for model in models:
        row = {
            "provider": "runway", "model": model, "status": blocked or "PENDING",
            "requested_duration_seconds": 10, "generated_duration_seconds": 0,
            "resolution_setting": "768p (input aspect ratio)" if model == "h3_max" else "720:1280",
            "request_count": 0, "retries": 0, "estimated_cost_usd": 0, "actual_cost_usd": 0,
            "generation_time_seconds": 0, "output_path": None, "specification_sha256": spec_hash,
        }
        report["results"].append(row)
        save()
        if blocked:
            continue
        folder = root / model.replace(".", "_")
        first_record, started = len(budget.requests), time.monotonic()
        try:
            provider = RunwayVisualProvider(budget, model=model, usd_per_second=MODELS[model], max_attempts=retries, ratio="720:1280")
            asset = provider.create(copy.deepcopy(story), copy.deepcopy(scene), folder)
            media = media_summary(asset.path)
            if not media["has_video"] or abs(media["duration"] - 10) > .5:
                raise ProviderFailure("COMPARISON_DURATION_MISMATCH")
            row.update(status="SUCCEEDED", generated_duration_seconds=media["duration"], output_path=str(asset.path.resolve()), width=media["width"], height=media["height"])
        except BudgetExceeded:
            row["status"] = blocked = "BUDGET_EXCEEDED"
        except ProviderFailure as exc:
            row.update(status="FAILED", error_code=str(exc))
        except Exception as exc:
            # Unexpected failures remain visible, but never persist raw API errors.
            row.update(status="FAILED", error_code=type(exc).__name__)
        finally:
            records = budget.requests[first_record:]
            row.update(
                request_count=len(records), retries=sum(int(r["retry"] > 0) for r in records),
                estimated_cost_usd=float(sum((Decimal(r["reserved_usd"]) for r in records), Decimal(0))),
                actual_cost_usd=None if records else 0,
                generation_time_seconds=round(time.monotonic() - started, 3),
            )
            write_json(folder / "result.json", sanitize(row))
            save()
    report["status"] = blocked or ("COMPLETED" if all(r["status"] == "SUCCEEDED" for r in report["results"]) else "COMPLETED_WITH_FAILURES")
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--output-dir", default="output/provider-comparison")
    args = parser.parse_args()
    try:
        result = compare(args.output_dir, args.models)
    except (ValueError, ProviderFailure) as exc:
        parser.error(sanitize(str(exc)))
    print(json.dumps({k: result[k] for k in ("status", "report_path")}, indent=2))
    return 1 if result["status"] == "COMPLETED_WITH_FAILURES" else 0


if __name__ == "__main__":
    raise SystemExit(main())
