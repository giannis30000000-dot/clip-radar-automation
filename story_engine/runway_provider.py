"""Runway scene clips; task polling never resubmits an accepted generation."""
from __future__ import annotations

import math
import os
from pathlib import Path
import time
import requests

from media_tools import media_summary
from .costs import sanitize
from .history import write_json
from .provider_http import ProviderFailure, attempts, endpoint, rate, request_json
from .providers import VisualAsset


def scene_prompt(story, scene) -> str:
    index = scene["scene_number"] - 1
    previous = story["scenes"][index-1]["visual_description"] if index else "Opening scene"
    cast = "; ".join(c["name"] + ": " + c["description"] for c in story["characters"] if c["character_id"] in scene["characters_present"])
    art = story.get("art_direction") or {}
    text = (
        "Original fictional cinematic comedy. No text, logos or subtitles. "
        f"Action: {scene['video_prompt'][:320]}. Camera: {scene['camera_direction'][:100]}. "
        f"Cast continuity: {cast[:250]}. Style: {str(art.get('style', 'expressive stylized 3D animation'))[:110]}. "
        f"Environment: {str(art.get('environment', scene['visual_description']))[:110]}. Previously: {previous[:100]}"
    )
    return text.encode("utf-16-le")[:2000].decode("utf-16-le", errors="ignore")


class RunwayVisualProvider:
    def __init__(self, budget, session=None, download_session=None, sleep=time.sleep):
        self.budget = budget
        self.session = session or requests.Session()
        self.download_session = download_session or requests.Session()
        self.sleep = sleep

    def create(self, story: dict, scene: dict, directory: Path) -> VisualAsset:
        directory.mkdir(parents=True, exist_ok=True)
        base = "https://api.dev.runwayml.com/v1"
        model = os.getenv("RUNWAY_MODEL", "gen4.5")
        price = rate("RUNWAY_USD_PER_SECOND", ".12" if model == "gen4.5" else None)
        duration = max(2, min(10, math.ceil(scene["estimated_duration"])))
        ratio = os.getenv("RUNWAY_RATIO", "720:1280")
        if ratio not in {"720:1280", "1280:720"}:
            raise ProviderFailure("UNSUPPORTED_RUNWAY_RATIO")
        payload = {"model": model, "promptText": scene_prompt(story, scene), "ratio": ratio, "duration": duration}
        route = "/text_to_video"
        if scene.get("reference_image_url"):
            payload["promptImage"] = endpoint(scene["reference_image_url"])
            route = "/image_to_video"
        headers = {"Authorization": "Bearer " + os.environ["RUNWAYML_API_SECRET"], "X-Runway-Version": "2024-11-06"}
        report_path = directory / f"scene_{scene['scene_number']:02}.provider.json"
        report = {"provider": "runway", "model": model, "request": sanitize(payload), "attempts": []}
        output = directory / f"scene_{scene['scene_number']:02}.mp4"
        for retry in range(attempts()):
            record = self.budget.reserve("runway", model, price * duration, retry=retry, requested_seconds=duration, scene_number=scene["scene_number"])
            task_id = None
            try:
                try:
                    task = request_json(self.session, "POST", base + route, headers=headers, json=payload)
                except ProviderFailure as exc:
                    # A timeout/5xx may already have created a billable task.
                    # Only a definite rate-limit rejection is resubmitted.
                    raise ProviderFailure(str(exc), retryable=str(exc) == "HTTP_429") from None
                task_id = task.get("id")
                if not isinstance(task_id, str) or not task_id or not all(c.isalnum() or c in "-_" for c in task_id):
                    raise ProviderFailure("UNKNOWN_TASK_ID")
                self.budget.update(record, task_id=task_id, status="SUBMITTED")
                report["attempts"].append({"task_id": task_id, "status": "SUBMITTED"})
                write_json(report_path, sanitize(report))
                polls = max(1, min(120, int(os.getenv("RUNWAY_MAX_POLLS", "60"))))
                for poll in range(polls):
                    self.sleep(5)
                    try:
                        task = request_json(self.session, "GET", base + "/tasks/" + task_id, headers=headers)
                    except ProviderFailure as exc:
                        if exc.retryable or str(exc) == "NETWORK_RESULT_UNKNOWN":
                            continue  # Poll the same ID; no new paid request.
                        raise
                    state = task.get("status")
                    if state == "SUCCEEDED":
                        break
                    if state in {"FAILED", "CANCELED", "CANCELLED"}:
                        # Do not reattempt moderation failures or alter prompts.
                        failure = str(task.get("failureCode", ""))
                        raise ProviderFailure("TASK_" + state, retryable=state == "FAILED" and not any(w in failure.upper() for w in ("SAFETY", "MODERAT", "POLICY")))
                else:
                    raise ProviderFailure("TASK_TIMEOUT_RESULT_UNKNOWN")
                urls = task.get("output") or []
                if not urls or not isinstance(urls[0], str) or not urls[0].startswith("https://"):
                    raise ProviderFailure("MISSING_VIDEO_OUTPUT")
                self._download(urls[0], output)
                try:
                    media = media_summary(output)
                except RuntimeError:
                    raise ProviderFailure("INVALID_SCENE_VIDEO") from None
                if not media["has_video"] or media["duration"] < min(duration, scene["estimated_duration"]) - .2:
                    raise ProviderFailure("INVALID_SCENE_VIDEO")
                self.budget.update(record, status="SUCCEEDED", generated_seconds=media["duration"], generated_assets=1)
                report["attempts"][-1]["status"] = "SUCCEEDED"
                report.update(output_file=output.name, media={k: media[k] for k in ("duration", "width", "height", "size")})
                write_json(report_path, sanitize(report))
                return VisualAsset(output, "video", "runway")
            except ProviderFailure as exc:
                self.budget.update(record, status=str(exc))
                report["attempts"].append({"task_id": task_id, "status": str(exc)})
                write_json(report_path, sanitize(report))
                if not exc.retryable:
                    raise
            except (KeyError, TypeError, ValueError):
                self.budget.update(record, status="INVALID_PROVIDER_RESULT")
                raise ProviderFailure("INVALID_PROVIDER_RESULT") from None
            if retry + 1 < attempts():
                self.sleep(min(2 ** retry, 4))
        raise ProviderFailure("VISUAL_ATTEMPTS_EXHAUSTED")

    def _download(self, url, path):
        # The separate session never carries the provider Authorization header.
        temporary = path.with_suffix(".partial")
        try:
            with self.download_session.get(url, stream=True, timeout=(15, 90)) as response:
                if response.status_code != 200:
                    raise ProviderFailure("VIDEO_DOWNLOAD_FAILED")
                size = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > 100 * 1024 * 1024:
                            raise ProviderFailure("VIDEO_DOWNLOAD_TOO_LARGE")
                        handle.write(chunk)
            with temporary.open("rb") as handle:
                if b"ftyp" not in handle.read(64):
                    raise ProviderFailure("VIDEO_DOWNLOAD_NOT_MP4")
            os.replace(temporary, path)
        except requests.RequestException:
            raise ProviderFailure("VIDEO_DOWNLOAD_FAILED") from None
        finally:
            temporary.unlink(missing_ok=True)
