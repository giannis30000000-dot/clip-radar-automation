"""Original story orchestration. No publishing or media-host client is invoked."""
from __future__ import annotations

import json
import os
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

    story, folder = None, None
    try:
        with history.locked():
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
                    provider = story_provider or load_provider("script", DemoStoryProvider)
                    story = validate_story(provider.generate(excluded_concepts=history.fingerprints(), template=template or os.getenv("STORY_TEMPLATE") or None))
                    folder = root / story["story_id"]
                    if folder.exists():
                        raise DuplicatePremise("Output already exists; use explicit regeneration to preserve it")
                    history.reserve(story, folder / "final.mp4")
                folder.mkdir(parents=True, exist_ok=False)
                story.setdefault("generation", {})
                for metadata in story["platform_metadata"].values():
                    metadata["publishing_enabled"] = False
                metadata_path = folder / "story.json"
                write_json(metadata_path, story)
                history.update(story["story_id"], metadata_path=str(metadata_path), output_path=str(folder / "final.mp4"))
                status("SYNTHESIZING_VOICE", story_id=story["story_id"], title=story["title"])
                voice = voice_provider or load_provider("voice", DevelopmentVoiceProvider)
                audio, captions, voice_metadata = voice.synthesize(story, folder / "audio")
                story["generation"]["voice"] = voice_metadata
                validate_story(story)
                write_json(metadata_path, story)
                status("CREATING_SCENES", story_id=story["story_id"], title=story["title"])
                visual = visual_provider or load_provider("visual", DevelopmentVisualProvider)
                assets = [visual.create(story, scene, folder / "scenes") for scene in story["scenes"]]
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
                ledger.upsert_clip(story["story_id"], "REVIEW_REQUIRED", content_type="original_story", title=story["title"], qc_passed=passed, final_output_identifier=str(final), publication_allowed=False, source_basis="authored_fiction_and_generated_development_assets", review_reason="Development visuals and voice require owner review before future publication")
                history.update(story["story_id"], status=final_status, qc_result=quality["status"], publication_state="REVIEW_REQUIRED")
                result = status(final_status, story_id=story["story_id"], title=story["title"], script=story["full_script"], scenes=story["scenes"], metadata=str(metadata_path), final=str(final), preview=preview, qc=quality, outputs=[{"story_id": story["story_id"], "final": str(final)}], review={"approved": False, "publish_available": False, "regenerate_story_id": story["story_id"]})
                write_json(folder / "generation.json", result)
                return result
            except (NoNewPremise, DuplicatePremise) as exc:
                return status("NO_NEW_PREMISE", reason=str(exc))
            except Exception:
                if story and story["story_id"] in history.read()["stories"]:
                    history.update(story["story_id"], status="FAILED", qc_result="NOT_PASSED")
                raise
    except HistoryBusy as exc:
        # Do not overwrite a running generation's dashboard status.
        return {**base, "status": "BUSY", "reason": str(exc)}
    except Exception as exc:
        status("FAILED", reason=f"{type(exc).__name__}: {exc}")
        raise
