"""Dialogue schema helpers and inspectable, fail-closed editorial checks."""
from __future__ import annotations

import math
import os
import re

from .history import DuplicatePremise
from .schema import StoryValidationError, concept_fingerprint, estimate_duration

CATEGORIES = ("absurd_comedy", "situational_comedy", "visual_comedy", "cute_emotional", "mystery_twist", "everyday_absurd", "fantasy_scifi", "talking_food_objects")
CONCEPT_METRICS = ("hook", "curiosity", "interest", "dialogue", "visual", "escalation", "payoff", "originality", "retention")
QUALITY_METRICS = ("hook", "setup", "conflict", "natural_dialogue", "escalation", "payoff", "pacing", "visual_variety", "originality", "duration")


class StoryQualityFailure(ValueError):
    pass


def max_attempts():
    return max(1, min(3, int(os.getenv("STORY_QUALITY_MAX_ATTEMPTS", "3"))))


def duration_policy():
    minimum = float(os.getenv("STORY_MIN_DURATION_SECONDS", "65"))
    if not math.isfinite(minimum) or not 45 <= minimum <= 75:
        raise ValueError("STORY_MIN_DURATION_SECONDS must be 45-75; default 65")
    return {"minimum": minimum, "maximum": 75, "explicit_short_override": minimum < 60}


def select_concept(candidates, excluded, history=None):
    if not isinstance(candidates, list) or len(candidates) < 3:
        raise StoryQualityFailure("At least three distinct candidate ideas required")
    results, seen = [], set()
    recent = list(history.read()["stories"].values())[-3:] if history else []
    for candidate in candidates:
        reasons = []
        fingerprint = concept_fingerprint(candidate["concept"])
        scores = candidate["scores"]
        if not all(type(scores.get(k)) in (int, float) and math.isfinite(scores[k]) and 0 <= scores[k] <= 10 for k in CONCEPT_METRICS):
            raise StoryQualityFailure("Invalid candidate scores")
        score = sum(scores[k] for k in CONCEPT_METRICS) / len(CONCEPT_METRICS)
        if score < 7 or min(scores[k] for k in CONCEPT_METRICS) < 6 or scores["hook"] < 8 or scores["payoff"] < 7:
            reasons.append("weak_hook_interest_or_payoff")
        if candidate.get("category") not in CATEGORIES or not candidate.get("trope"):
            reasons.append("missing_category_or_trope")
        if fingerprint in seen or fingerprint in excluded:
            reasons.append("duplicate_concept")
        seen.add(fingerprint)
        if history:
            try:
                history.ensure_new({"story_id": "candidate-check", "concept": candidate["concept"]})
            except DuplicatePremise:
                reasons.append("history_duplicate")
        if sum(c.get("trope") == candidate.get("trope") for c in recent) >= 2:
            reasons.append("recent_trope_repeated")
        results.append({**candidate, "score": round(score, 2), "rejected_reasons": reasons})
    if len(seen) < 3:
        raise StoryQualityFailure("At least three distinct candidate ideas required")
    eligible = sorted((c for c in results if not c["rejected_reasons"]), key=lambda c: c["score"], reverse=True)
    if not eligible:
        raise StoryQualityFailure("No strong original candidate")
    return eligible[0], results


def normalize_dialogue(story):
    """Mechanical timing/text, never trust model IDs, timings or approval flags."""
    story["schema_version"] = 2
    story["duration_policy"] = duration_policy()
    story["target_duration_seconds"] = 70
    story["concept_fingerprint"] = concept_fingerprint(story["concept"])
    story.pop("actual_voice_duration_seconds", None)
    story.pop("hook_duration_seconds", None)
    story.pop("story_quality", None)
    story.pop("voice_assignments", None)  # Account-specific IDs come from configuration only.
    cursor = 0.0
    for index, line in enumerate(story["dialogue"], 1):
        duration = round(estimate_duration(line["text"]) + .12, 3)
        line.update(order=index, intended_start_time=round(cursor, 3), estimated_duration_seconds=duration)
        cursor += duration
    for scene in story["scenes"]:
        lines = [l for l in story["dialogue"] if l["scene_number"] == scene["scene_number"]]
        scene.update(narration=" ".join(l["text"] for l in lines), start_time=lines[0]["intended_start_time"], estimated_duration=round(sum(l["estimated_duration_seconds"] for l in lines), 3), active_speaker=lines[0]["speaker_id"])
        scene["video_prompt"] = scene["visual_prompt"]
        scene["image_prompt"] = scene["visual_prompt"]
    story["full_script"] = " ".join(l["text"] for l in story["dialogue"])
    story["estimated_voice_duration_seconds"] = round(cursor, 3)
    for metadata in story["platform_metadata"].values():
        metadata["publishing_enabled"] = False
    return story


def validate_dialogue(story):
    def require(value, reason):
        if not value:
            raise StoryValidationError(reason)
    cast = {c["character_id"]: c for c in story["characters"]}
    require(all(isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", k) for k in cast), "unsafe speaker ID")
    require(2 <= sum(k != "narrator" for k in cast) <= 4, "dialogue requires 2-4 characters plus optional narrator")
    for c in cast.values():
        require(all(isinstance(c.get(k), str) and c[k].strip() for k in ("personality", "speaking_style", "visual_description", "voice_profile_hint", "description")), "incomplete dialogue character")
    lines = story.get("dialogue")
    require(isinstance(lines, list) and bool(lines), "dialogue lines required")
    cursor, scene_number = 0.0, 1
    for order, line in enumerate(lines, 1):
        require(line.get("speaker_id") in cast, "unknown dialogue speaker")
        require(line.get("order") == order, "dialogue order must be contiguous")
        require(line.get("scene_number") in (scene_number, scene_number + 1), "dialogue scenes must be ordered")
        scene_number = line["scene_number"]
        require(1 <= scene_number <= len(story["scenes"]), "unknown dialogue scene")
        present = story["scenes"][scene_number - 1]["characters_present"]
        require(line["speaker_id"] == "narrator" or line["speaker_id"] in present, "speaker absent from scene")
        require(all(isinstance(line.get(k), str) and line[k].strip() for k in ("text", "emotion", "action")), "incomplete dialogue line")
        require(isinstance(line.get("listeners"), list) and all(c in present and c != line["speaker_id"] for c in line["listeners"]), "invalid listeners")
        start, duration = line.get("intended_start_time"), line.get("estimated_duration_seconds")
        require(all(type(v) in (float, int) and math.isfinite(v) for v in (start, duration)), "invalid line timing")
        require(abs(start - cursor) < .03 and .1 <= duration <= 12, "line gap, overlap or excessive duration")
        cursor += duration
    require(abs(cursor - story["estimated_voice_duration_seconds"]) < .05, "dialogue duration mismatch")
    for scene in story["scenes"]:
        group = [l for l in lines if l["scene_number"] == scene["scene_number"]]
        require(bool(group) and scene.get("active_speaker") == group[0]["speaker_id"], "scene active speaker mismatch")
        require(scene["narration"] == " ".join(l["text"] for l in group), "dialogue/scene mismatch")
        require(abs(scene["start_time"] - group[0]["intended_start_time"]) < .03 and abs(scene["estimated_duration"] - sum(l["estimated_duration_seconds"] for l in group)) < .03, "scene/line timing mismatch")
        require(all(isinstance(scene.get(k), str) and scene[k].strip() for k in ("visual_prompt", "action_direction", "reaction_direction", "background_music_mood")), "missing dialogue scene direction")
    require(story["full_script"] == " ".join(l["text"] for l in lines), "full dialogue mismatch")


def evaluate_story(story):
    from .schema import validate_story
    try:
        validate_story(story)
        validate_dialogue(story)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        return {"status": "REJECTED", "failed_checks": ["schema"], "reason": type(exc).__name__}
    lines, scenes = story["dialogue"], story["scenes"]
    beats = story.get("story_beats", {})
    policy = duration_policy()  # A saved/model-written permissive policy cannot bypass current limits.
    duration = story.get("actual_voice_duration_seconds", story["estimated_voice_duration_seconds"])
    text = [re.sub(r"\W+", " ", l["text"].lower()).strip() for l in lines]
    speakers = {l["speaker_id"] for l in lines if l["speaker_id"] != "narrator"}
    checks = {
        "hook": 0 < len(story["hook"].split()) <= 5 and story.get("hook_duration_seconds", estimate_duration(story["hook"])) <= 2,
        "duration": policy["minimum"] <= duration <= policy["maximum"],
        "setup_goal_conflict": all(isinstance(beats.get(k), str) and len(beats[k].split()) >= 3 for k in ("setup", "goal", "conflict", "payoff")),
        "escalation": isinstance(beats.get("escalation"), list) and len(set(beats["escalation"])) >= 2,
        "reaction_payoffs": sum(bool(s.get("payoff_moment")) for s in scenes) >= 3 and bool(scenes[-1].get("payoff_moment")),
        "dialogue_dominant": len(speakers) >= 2 and sum(len(l["text"].split()) for l in lines if l["speaker_id"] != "narrator") / max(1, len(story["full_script"].split())) >= .8,
        "no_dead_sections": all(l["estimated_duration_seconds"] <= 10 and len(l["text"].split()) <= 28 for l in lines),
        "no_repetition": len(set(text)) == len(text) and all(len({l["speaker_id"] for l in lines[i:i+3]}) > 1 for i in range(len(lines)-2)),
        "visual_variety": len({s["visual_prompt"].strip().lower() for s in scenes}) >= .75 * len(scenes),
        "conversational": not any(p in story["full_script"].lower() for p in ("as you already know", "let me explain everything", "the moral of the story")),
        "category": story.get("content_category") in CATEGORIES,
    }
    review = story.get("generation", {}).get("editorial_review")
    if story.get("generation", {}).get("provider") == "dialogue-chat":
        checks["editorial_review"] = isinstance(review, dict) and all(type(review.get(k)) in (int, float) and math.isfinite(review[k]) and 7 <= review[k] <= 10 for k in QUALITY_METRICS)
    failed = [k for k, v in checks.items() if not v]
    return {"status": "REJECTED" if failed else "PASSED", "checks": checks, "failed_checks": failed, "duration_seconds": duration, "basis": "deterministic checks plus separate model critique for production; not a guarantee of audience engagement"}
