"""Original story orchestration. No publishing or media-host client is invoked."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable

from media_processor import render_story
from inspection_frames import inspect_story_file
from publication_state import PublicationLedger
from quality_control import inspect_story_final

from .history import DuplicatePremise, HistoryBusy, StoryHistory, write_json
from .providers import DemoStoryProvider, NoNewPremise, load_provider
from .schema import validate_story
from .visuals import DevelopmentVisualProvider
from .voice import DevelopmentVoiceProvider
from .costs import BudgetExceeded, CostBudget, sanitize
from .dialogue import evaluate_story, max_attempts
from .provider_http import ProviderFailure
from .production_review import strict_production, production_preflight, prepare_references


def generate_story_video(
    output_dir: Path | str | None = None,
    state_file: Path | str | None = None,
    *,
    template: str | None = None,
    story_provider=None,
    visual_provider=None,
    voice_provider=None,
    regenerate_story_id: str | None = None,
    on_status: Callable[[dict], None] | None = None,
) -> dict:
    """Generate one review package; callbacks receive persisted dashboard status.

    A future publishing adapter must be a separate owner-approved operation.
    Even PUBLISHING_ENABLED=true cannot cause this function to send a post.
    Regeneration is explicit, versioned, and preserves prior review artifacts.
    """
    root = Path(output_dir or os.getenv("CLIP_RADAR_OUTPUT_DIR", "output/stories")).resolve()
    history = StoryHistory(Path(state_file or os.getenv("STORY_HISTORY_FILE", "state/story_history.json")).resolve())
    base = {"mode": "ORIGINAL_STORY_MODE", "publishing_enabled": False, "live_request_sent": False, "cloudinary_required": False, "outputs": []}

    def status(value, **fields):
        result = {**base, "status": value, **fields}
        write_json(root / "run_summary.json", result)
        if on_status:
            on_status(result)
        print(f"story | {value}", flush=True)
        return result

    story, folder, provider = None, None, None
    budget = None
    try:
        if strict_production():
            preflight = production_preflight()
            write_json(root / "production_preflight.json", preflight)
            if not preflight["ready"]:
                return status("CONFIGURATION_REQUIRED", preflight=preflight)
        with history.locked():
            budget = CostBudget(root / "attempts" / uuid.uuid4().hex / "cost_report.json")
            base["cost_report"] = str(budget.paths[0])
            try:
                status("GENERATING_IDEA")
                if regenerate_story_id:
                    previous = history.read()["stories"].get(regenerate_story_id)
                    if not previous:
                        raise ValueError("Cannot regenerate an unknown story_id")
                    story = validate_story(json.loads(Path(previous["metadata_path"]).read_text(encoding="utf-8")))
                    version = previous["generation_attempts"] + 1
                    folder = root / f"{story['story_id']}_v{version}"
                    history.update(story["story_id"], generation_attempts=version, status="GENERATING")
                else:
                    provider = story_provider or load_provider("script", DemoStoryProvider, budget=budget, history=history)
                    story = validate_story(provider.generate(excluded_concepts=history.fingerprints(), template=template or os.getenv("STORY_TEMPLATE") or None))
                    folder = root / story["story_id"]
                    if folder.exists():
                        raise DuplicatePremise("Output already exists; use explicit regeneration to preserve it")
                    history.reserve(story, folder / "final.mp4")
                folder.mkdir(parents=True, exist_ok=False)
                budget.attach(folder)
                base["cost_report"] = str(folder / "cost_report.json")
                story.setdefault("generation", {})
                for metadata in story["platform_metadata"].values():
                    metadata["publishing_enabled"] = False
                metadata_path = folder / "story.json"
                write_json(metadata_path, story)
                history.update(story["story_id"], metadata_path=str(metadata_path), output_path=str(folder / "final.mp4"))
                prepared_voice = None
                from .runway_provider import RunwayVisualProvider
                production_visual = (os.getenv("VISUAL_PROVIDER") or os.getenv("STORY_VISUAL_PROVIDER")) == "production"
                production_visual = production_visual or isinstance(visual_provider, RunwayVisualProvider) or isinstance(getattr(visual_provider, "production", None), RunwayVisualProvider)
                if "dialogue" in story or production_visual:
                    voice = voice_provider or load_provider("voice", DevelopmentVoiceProvider, budget=budget)
                    for attempt in range(max_attempts()):
                        quality_gate = evaluate_story(story)
                        if quality_gate["status"] == "PASSED":
                            status("SYNTHESIZING_VOICE", story_id=story["story_id"], title=story["title"])
                            prepared_voice = voice.synthesize(story, folder / "audio" / f"attempt_{attempt+1}")
                            if strict_production() and (prepared_voice[2].get("provider") != "elevenlabs-dialogue" or prepared_voice[2].get("development_only", True)):
                                raise ProviderFailure("PRODUCTION_DIALOGUE_VOICE_REQUIRED")
                            quality_gate = evaluate_story(story)
                        write_json(folder / f"story_quality_attempt_{attempt+1}.json", quality_gate)
                        if quality_gate["status"] == "PASSED":
                            story["story_quality"] = quality_gate
                            break
                        prepared_voice = None
                        if attempt + 1 == max_attempts() or not hasattr(provider, "rewrite"):
                            break
                        try:
                            revised = provider.rewrite(story, json.dumps(quality_gate))
                            if revised["concept"] != story["concept"]:
                                raise ValueError("A timing rewrite must retain the reserved concept")
                            revised["story_id"] = story["story_id"]
                            story = validate_story(revised)
                        except ProviderFailure:
                            break
                    write_json(folder / "story_quality.json", quality_gate)
                    write_json(metadata_path, story)
                    if quality_gate["status"] != "PASSED":
                        history.update(story["story_id"], status="REVIEW_REQUIRED", qc_result="NOT_RUN")
                        return status("REVIEW_REQUIRED", reason="STORY_QUALITY_REJECTED_BEFORE_VISUALS", story_quality=quality_gate)
                status("CREATING_SCENES", story_id=story["story_id"], title=story["title"])
                visual = visual_provider or load_provider("visual", DevelopmentVisualProvider, budget=budget)
                if os.getenv("STORY_REFERENCE_PROVIDER") == "runway":
                    if os.getenv("RUNWAY_MODEL") != "h3_max":
                        raise ProviderFailure("AUTOMATIC_REFERENCE_PLAN_REQUIRES_H3_MAX")
                    status("CREATING_REFERENCES", story_id=story["story_id"], title=story["title"])
                    references = prepare_references(story, budget, folder)
                    write_json(metadata_path, story)
                    assets = []
                    for scene, reference in zip(story["scenes"], references):
                        status("CREATING_SCENE", scene_number=scene["scene_number"], story_id=story["story_id"])
                        asset = visual.create(story, scene, folder / "scenes") if scene["visual_plan"]["kind"] == "video" else reference
                        if strict_production() and asset.provider not in {"runway", "runway-reference"}:
                            raise ProviderFailure("PRODUCTION_VISUAL_REQUIRED")
                        assets.append(asset)
                else:
                    assets = [visual.create(story, scene, folder / "scenes") for scene in story["scenes"]]
                if prepared_voice is None:
                    status("SYNTHESIZING_VOICE", story_id=story["story_id"], title=story["title"])
                    voice = voice_provider or load_provider("voice", DevelopmentVoiceProvider, budget=budget)
                    prepared_voice = voice.synthesize(story, folder / "audio")
                audio, captions, voice_metadata = prepared_voice
                story["generation"]["voice"] = voice_metadata
                story["generation"]["development_visuals"] = any(asset.provider == "deterministic-pillow-storyboard" for asset in assets)
                story["generation"]["provider_events"] = budget.events
                validate_story(story)
                write_json(metadata_path, story)
                status("RENDERING", story_id=story["story_id"], title=story["title"])
                final = folder / "final.mp4"
                render_story(story, assets, audio, captions, final)
                status("RUNNING_QC", story_id=story["story_id"], title=story["title"])
                quality = inspect_story_final(final, metadata_path)
                write_json(folder / "quality.json", quality)
                preview = inspect_story_file(final, folder / "review_frames")
                passed = quality["status"] == "QUALITY_CHECK_PASSED"
                final_status = "READY_FOR_REVIEW" if passed else "REVIEW_REQUIRED"
                ledger = PublicationLedger(folder / "publication_state.json")
                ledger.upsert_clip(story["story_id"], "REVIEW_REQUIRED", content_type="original_story", title=story["title"], qc_passed=passed, final_output_identifier=str(final), publication_allowed=False, source_basis="original_fiction_and_provider_assets", review_reason="Story, provider licensing, continuity and creative quality require owner review before publication")
                history.update(story["story_id"], status=final_status, qc_result=quality["status"], publication_state="REVIEW_REQUIRED")
                result = status(final_status, story_id=story["story_id"], title=story["title"], script=story["full_script"], scenes=story["scenes"], metadata=str(metadata_path), final=str(final), preview=preview, qc=quality, outputs=[{"story_id": story["story_id"], "final": str(final)}], review={"approved": False, "publish_available": False, "regenerate_story_id": story["story_id"]})
                write_json(folder / "generation.json", result)
                return result
            except (NoNewPremise, DuplicatePremise) as exc:
                return status("NO_NEW_PREMISE", reason=str(exc))
            except BudgetExceeded:
                if story and story["story_id"] in history.read()["stories"]:
                    history.update(story["story_id"], status="BUDGET_EXCEEDED", qc_result="NOT_RUN", cost_report=str(budget.paths[-1]))
                return status("BUDGET_EXCEEDED", reason="Paid generation stopped before exceeding the configured ceiling", costs=budget.report())
            except Exception:
                if story and story["story_id"] in history.read()["stories"]:
                    history.update(story["story_id"], status="FAILED", qc_result="NOT_PASSED")
                raise
    except HistoryBusy as exc:
        # Do not overwrite a running generation's dashboard status.
        return {**base, "status": "BUSY", "reason": str(exc)}
    except Exception as exc:
        status("FAILED", reason=sanitize(f"{type(exc).__name__}: {exc}"))
        raise
