"""Opt-in, fail-closed production review; no publishing or scheduler integration."""
import json
import math
import os
import re

from .costs import BudgetExceeded, money
from .dialogue import evaluate_story
from .history import write_json
from .provider_http import ProviderFailure, rate
from .reference_images import IMAGE_COST_USD, RunwayReferenceImageProvider, build_visual_bible


def strict_production():
    return os.getenv("STORY_REQUIRE_PRODUCTION", "").lower() in {"true", "1"}


def production_preflight():
    """Presence only. Never return credentials or fall through to a paid partial run."""
    required = ("STORY_LLM_API_KEY", "RUNWAYML_API_SECRET", "ELEVENLABS_API_KEY", "STORY_VOICE_POOL")
    presence = {k: bool(os.getenv(k, "").strip()) for k in required}
    errors = ["Missing " + k for k, present in presence.items() if not present]
    try:
        pool = json.loads(os.getenv("STORY_VOICE_POOL") or "[]")
        if not isinstance(pool, list) or not 2 <= len(pool) <= 4 or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in pool) or len(set(pool)) != len(pool):
            raise ValueError("invalid pool")
    except (ValueError, TypeError):
        errors.append("STORY_VOICE_POOL must contain 2-4 distinct voice IDs as a JSON array")
    try:
        if not 0 < money(os.getenv("CLIP_RADAR_MAX_COST_USD_PER_VIDEO") or "0") <= 10:
            errors.append("An explicit positive per-video cost cap no greater than $10 is required")
    except (ValueError, ArithmeticError):
        errors.append("Invalid per-video cost cap")
    for key in ("STORY_PROVIDER", "VOICE_PROVIDER", "VISUAL_PROVIDER"):
        if os.getenv(key) != "production":
            errors.append(key + " must be production")
    if os.getenv("RUNWAY_MODEL") != "h3_max" or os.getenv("STORY_REFERENCE_PROVIDER") != "runway":
        errors.append("RUNWAY_MODEL=h3_max and STORY_REFERENCE_PROVIDER=runway required")
    if os.getenv("STORY_VOICE_MAP"):
        errors.append("Clear STORY_VOICE_MAP for this pool-assigned production review")
    try:
        minimum = float(os.getenv("STORY_MIN_DURATION_SECONDS", "65"))
        if not 65 <= minimum <= 75:
            raise ValueError("outside target")
    except ValueError:
        errors.append("This production review requires a 65-75 second target")
    return {"ready": not errors, "credential_presence": presence, "errors": errors}


def plan_visuals(story, budget):
    """Protect all reference frames, then spend motion on hook/payoff before other beats."""
    if evaluate_story(story)["status"] != "PASSED" or not story.get("actual_voice_duration_seconds"):
        raise ProviderFailure("STORY_AND_MEASURED_VOICE_REQUIRED_BEFORE_REFERENCES")
    image_cost = money(IMAGE_COST_USD) * (len(story["scenes"]) + 1)
    price = rate("RUNWAY_USD_PER_SECOND", ".08")
    available = budget.limit - budget.reserved - image_cost
    entries = [{"scene_number": s["scene_number"], "generated_seconds": max(2, min(10, math.ceil(s["estimated_duration"]))), "kind": "image"} for s in story["scenes"]]
    # First and final scene are essential motion. Then payoff beats, then others.
    priority = sorted(range(len(entries)), key=lambda i: (0 if i in {0, len(entries)-1} else 1 if story["scenes"][i].get("payoff_moment") else 2, i))
    for i in priority:
        cost = price * entries[i]["generated_seconds"]
        if available >= cost:
            entries[i]["kind"] = "video"
            available -= cost
        elif i in {0, len(entries)-1}:
            budget.event("visual-plan", "INSUFFICIENT_BUDGET_FOR_REFERENCES_AND_KEY_MOTION")
            raise BudgetExceeded("Cannot fit references plus hook/payoff motion; no visuals submitted")
    video_cost = sum((price * e["generated_seconds"] for e in entries if e["kind"] == "video"), money(0))
    return {"reference_images": len(entries) + 1, "reference_estimated_usd": float(image_cost), "motion_estimated_usd": float(video_cost), "total_remaining_visual_estimated_usd": float(image_cost + video_cost), "prior_reserved_usd": float(budget.reserved), "retry_policy": "one submission per visual; uncertain results never resubmitted", "scenes": entries}


def prepare_references(story, budget, folder, provider=None):
    # Re-evaluate the gates here as well, so callers cannot skip engine ordering.
    plan = plan_visuals(story, budget)
    story["visual_bible"] = build_visual_bible(story)
    write_json(folder / "visual_bible.json", story["visual_bible"])
    write_json(folder / "visual_plan.json", plan)
    provider = provider or RunwayReferenceImageProvider(budget)
    anchor = provider.create(story, None, folder / "references")
    assets = []
    for scene, entry in zip(story["scenes"], plan["scenes"]):
        asset = provider.create(story, scene, folder / "references", cast_reference=anchor.path)
        scene["reference_image_path"] = str(asset.path.resolve())
        scene["visual_plan"] = entry
        assets.append(asset)
    story["generation"]["visual_plan"] = plan
    return assets
