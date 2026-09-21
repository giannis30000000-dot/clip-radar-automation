"""Narrow interfaces for a future LLM and replaceable image/video providers."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
from typing import Protocol

from .schema import concept_fingerprint, estimate_duration, validate_story
from .templates import TEMPLATES


@dataclass(frozen=True)
class VisualAsset:
    path: Path
    kind: str  # image or video; the media assembler owns all timing
    provider: str


class StoryProvider(Protocol):
    def generate(self, *, excluded_concepts: set[str], template: str | None = None) -> dict: ...


class VisualProvider(Protocol):
    def create(self, story: dict, scene: dict, directory: Path) -> VisualAsset: ...


class ReferenceImageProvider(Protocol):
    def create(self, story: dict, scene: dict | None, directory: Path, *, cast_reference: Path | None = None) -> VisualAsset: ...


class NoNewPremise(RuntimeError):
    """An expected catalogue exhaustion, not an automation failure."""


class DemoStoryProvider:
    def generate(self, *, excluded_concepts: set[str], template: str | None = None) -> dict:
        if template == "dialogue-demo":
            from .dialogue_demo import DialogueDemoProvider
            return DialogueDemoProvider().generate(excluded_concepts=excluded_concepts)
        available = [t for t in TEMPLATES if template is None or t["key"] == template]
        if not available:
            raise ValueError(f"unknown story template: {template}")
        selected = next((t for t in available if concept_fingerprint(t["concept"]) not in excluded_concepts), None)
        if selected is None:
            raise NoNewPremise("Demo catalogue exhausted. Add a new premise or configure a story provider.")
        t = selected
        scenes, cursor = [], 0.0
        for number, (narration, headline, visual) in enumerate(t["beats"], 1):
            duration = estimate_duration(narration)
            scenes.append({
                "scene_number": number, "start_time": round(cursor, 3), "estimated_duration": duration,
                "narration": narration, "headline": headline, "visual_description": visual,
                "image_prompt": f"Original fictional comedy, graphic illustration, vertical 9:16. {visual} No logos or embedded captions.",
                "video_prompt": f"{visual} Subtle push-in, clear silhouette, readable physical comedy.",
                "characters_present": [t["character"].lower()], "camera_direction": "gentle push-in, alternating pan",
                "sound_effect_hint": "optional soft comic accent; narration remains primary", "transition_hint": "hard cut on narration beat",
            })
            cursor += duration
        full_script = " ".join(s["narration"] for s in scenes)
        story = {
            "schema_version": 1, "story_id": t["key"], "template_id": t["key"], "title": t["title"],
            "concept": t["concept"], "concept_fingerprint": concept_fingerprint(t["concept"]),
            "universe_id": t["universe_id"], "content_category": "original_fictional_comedy",
            "hook": scenes[0]["narration"].split(". ")[0] + ".", "full_script": full_script,
            "target_duration_seconds": 68, "estimated_voice_duration_seconds": round(cursor, 3),
            "characters": [{"character_id": t["character"].lower(), "name": t["character"], "description": f"Recurring fictional {t['prop']} with absurdly human ambitions."}],
            "scenes": scenes, "ending_type": "standalone", "sequel_possible": True,
            "art_direction": {"prop": t["prop"], "accent": t["accent"]},
            "platform_metadata": {p: {"caption": t["title"] + " | An original fictional Clip Radar story.", "hashtags": ["ClipRadar", "OriginalComedy", "Fiction"], "publishing_enabled": False} for p in ("tiktok", "instagram", "youtube")},
            "generation": {"provider": "authored-demo-templates", "development_visuals": True, "review_required": True},
        }
        return validate_story(story)


def load_provider(kind: str, default, *, budget=None, history=None):
    """Load an explicit module:factory for an installed future provider.

    Story adapters implement generate(excluded_concepts, template); visual
    adapters implement create(story, scene, directory). Credentials stay inside
    the adapter and never belong in returned story metadata.
    """
    alias = {"script": "STORY_PROVIDER", "visual": "VISUAL_PROVIDER", "voice": "VOICE_PROVIDER"}[kind]
    configured = os.getenv(alias) or os.getenv(f"STORY_{kind.upper()}_PROVIDER", "demo")
    configured = configured.strip()
    if configured in {"demo", "development"}:
        if kind == "script" and os.getenv("STORY_FORMAT") == "dialogue":
            from .dialogue_demo import DialogueDemoProvider
            return DialogueDemoProvider()
        return default()
    if configured == "production":
        if budget is None:
            raise ValueError("Production providers require a shared per-video CostBudget")
        from .provider_selection import production_provider
        return production_provider(kind, default, budget, history=history)
    module, separator, factory = configured.partition(":")
    if not separator or not module or not factory:
        raise ValueError(f"STORY_{kind.upper()}_PROVIDER must be demo or module:factory")
    return getattr(importlib.import_module(module), factory)()
