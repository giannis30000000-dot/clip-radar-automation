"""Provider-neutral story contract, validated before spending render work."""
from __future__ import annotations

import hashlib
import math
import re


class StoryValidationError(ValueError):
    pass


def concept_fingerprint(concept: str) -> str:
    canonical = " ".join(re.findall(r"[a-z0-9]+", concept.casefold()))
    return hashlib.sha256(canonical.encode()).hexdigest()


def estimate_duration(script: str, words_per_minute: float = 165) -> float:
    return round(len(script.split()) * 60 / words_per_minute, 3)


def validate_story(story: dict) -> dict:
    def require(condition, message):
        if not condition:
            raise StoryValidationError(message)

    require(isinstance(story, dict), "story must be an object")
    for name in ("story_id", "title", "concept", "content_category", "hook", "full_script", "ending_type"):
        require(isinstance(story.get(name), str) and bool(story[name].strip()), f"missing {name}")
    require(bool(re.fullmatch(r"[a-z0-9_-]{1,100}", story["story_id"])), "unsafe story_id")
    require(story["ending_type"] in {"standalone", "cliffhanger"}, "invalid ending_type")
    require(isinstance(story.get("sequel_possible"), bool), "sequel_possible must be boolean")
    for name in ("target_duration_seconds", "estimated_voice_duration_seconds"):
        value = story.get(name)
        require(isinstance(value, (int, float)) and math.isfinite(value) and 45 <= value <= 90, f"invalid {name}")
    require(60 <= story["target_duration_seconds"] <= 75, "target must be 60-75 seconds")
    characters = story.get("characters")
    require(isinstance(characters, list) and bool(characters), "characters required")
    require(all(isinstance(c, dict) and c.get("character_id") and c.get("name") and c.get("description") for c in characters), "invalid character")
    ids = {c["character_id"] for c in characters}
    require(len(ids) == len(characters), "duplicate character_id")
    scenes = story.get("scenes")
    require(isinstance(scenes, list) and 8 <= len(scenes) <= 24, "expected 8-24 scenes")
    cursor = 0.0
    for number, scene in enumerate(scenes, 1):
        require(isinstance(scene, dict) and scene.get("scene_number") == number, "scene numbering must be contiguous")
        for name in ("narration", "visual_description", "image_prompt", "video_prompt", "camera_direction", "sound_effect_hint", "transition_hint"):
            require(isinstance(scene.get(name), str) and bool(scene[name].strip()), f"scene {number}: missing {name}")
        present = scene.get("characters_present")
        require(isinstance(present, list) and all(c in ids for c in present), "unknown scene character")
        start, duration = scene.get("start_time"), scene.get("estimated_duration")
        require(all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, duration)), "non-finite scene timing")
        require(abs(start - cursor) < 0.03 and 0.5 <= duration <= 12, "scene gap, overlap or unreasonable length")
        cursor += duration
    require(abs(cursor - story["estimated_voice_duration_seconds"]) < 0.05, "scene/voice duration mismatch")
    require(story["full_script"] == " ".join(s["narration"] for s in scenes), "script/scene text mismatch")
    require(story["full_script"].startswith(story["hook"]), "hook must open the narration")
    metadata = story.get("platform_metadata")
    require(isinstance(metadata, dict) and all(isinstance(metadata.get(p), dict) and metadata[p].get("caption") for p in ("instagram", "tiktok", "youtube")), "platform metadata required")
    if story.get("schema_version") == 2 or "dialogue" in story:
        from .dialogue import validate_dialogue
        validate_dialogue(story)
    return story
