"""One Runway image adapter, a frozen per-story bible, and local first frames."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import time

from PIL import Image
import requests

from .history import write_json
from .provider_http import ProviderFailure, request_json
from .providers import VisualAsset

IMAGE_MODEL = "gen4_image"
IMAGE_COST_USD = ".05"  # 720p, 5 credits; pricing checked 2026-09-21.
IDENTITY_FIELDS = ("appearance", "proportions", "clothing_accessories", "colors", "facial_traits")
NO_TEXT = "No text, logos, watermarks, letters, signage, subtitles or UI overlays."
ACTING = "Dubbed acting; wide/medium reactions, no mouth closeups or lip-sync."


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def build_visual_bible(story):
    """Keep every identity field, never silently truncate continuity information."""
    cast = []
    for character in story["characters"]:
        if character["character_id"] == "narrator":
            continue
        identity = character.get("visual_identity") or {}
        if not all(isinstance(identity.get(k), str) and identity[k].strip() for k in IDENTITY_FIELDS):
            raise ValueError("VISUAL_BIBLE_REQUIRED: each character needs five compact visual_identity fields")
        cast.append({"character_id": character["character_id"], **{k: identity[k].strip() for k in IDENTITY_FIELDS}})
    art = story["art_direction"]
    if not all(isinstance(art.get(k), str) and art[k].strip() for k in ("style", "environment")):
        raise ValueError("VISUAL_BIBLE_REQUIRED: style and environment")
    # The fixed field order is named in the prompt and in the saved JSON.
    text = "Cast (ID: appearance / proportions / outfit / colors / face): "
    text += "; ".join(c["character_id"] + ": " + " / ".join(c[k] for k in IDENTITY_FIELDS) for c in cast)
    text += f". Style: {art['style']}. Environment: {art['environment']}."
    if utf16_length(text) > 600:
        raise ValueError("VISUAL_BIBLE_TOO_LONG: shorten identity/style fields; complete bible must fit 600 UTF-16 units")
    return {"version": 1, "characters": cast, "art_style": art["style"], "environment_style": art["environment"], "prompt_text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def reference_prompt(story, scene=None, *, video=False):
    bible = story["visual_bible"]["prompt_text"]
    prefix = f"{bible} {NO_TEXT} {ACTING} "
    if scene is None:
        detail = "Vertical full-body cast tableau, all characters separated, in their shared environment; no labels."
    else:
        detail = f"Only {','.join(scene['characters_present'])}. Camera: {scene['camera_direction']}. Action: {scene['action_direction']}. Reaction: {scene['reaction_direction']}. Setup: {scene['visual_prompt']}"
        if not video:
            prefix += "Match @cast identities. "
    # Gen-4 Image limit is 1000 UTF-16 units. H3 keeps full action directions.
    if not video:
        remaining = 1000 - utf16_length(prefix)
        if remaining < 120:
            raise ValueError("VISUAL_BIBLE_TOO_LONG")
        detail = detail.encode("utf-16-le")[:remaining * 2].decode("utf-16-le", errors="ignore")
    return prefix + detail


def image_input(path):
    """Runway accepts a base64 data URI; no signed URL or manual upload required."""
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 3_700_000:
        raise ProviderFailure("REFERENCE_IMAGE_INPUT_SIZE")
    try:
        with Image.open(path) as image:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
            if not mime or min(image.size) < 256:
                raise ValueError("unsupported image")
            image.verify()
    except (OSError, ValueError):
        raise ProviderFailure("INVALID_REFERENCE_IMAGE") from None
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def image_descriptor(path):
    path = Path(path)
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


class RunwayReferenceImageProvider:
    """One submission per image. Unknown results are never paid for twice."""
    def __init__(self, budget, session=None, download_session=None, sleep=time.sleep):
        self.budget = budget
        self.session = session or requests.Session()
        self.download_session = download_session or requests.Session()
        self.sleep = sleep

    def create(self, story, scene, directory, *, cast_reference=None):
        directory.mkdir(parents=True, exist_ok=True)
        name = "cast" if scene is None else f"scene_{scene['scene_number']:02}"
        payload = {"model": IMAGE_MODEL, "ratio": "720:1280", "promptText": reference_prompt(story, scene)}
        report = {"provider": "runway-reference", "model": IMAGE_MODEL, "request": dict(payload), "bible_sha256": story["visual_bible"]["sha256"]}
        if cast_reference:
            payload["referenceImages"] = [{"uri": image_input(cast_reference), "tag": "cast"}]
            report["reference_image"] = image_descriptor(cast_reference)
        elif scene is not None:
            raise ProviderFailure("CAST_REFERENCE_REQUIRED")
        headers = {"Authorization": "Bearer " + os.environ["RUNWAYML_API_SECRET"], "X-Runway-Version": "2024-11-06"}
        report_path = directory / (name + ".provider.json")
        record = self.budget.reserve("runway-reference", IMAGE_MODEL, IMAGE_COST_USD, reference_name=name)
        started = time.monotonic()
        try:
            task = request_json(self.session, "POST", "https://api.dev.runwayml.com/v1/text_to_image", headers=headers, json=payload)
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id or not all(c.isalnum() or c in "-_" for c in task_id):
                raise ProviderFailure("UNKNOWN_IMAGE_TASK_ID")
            report.update(task_id=task_id, status="SUBMITTED")
            self.budget.update(record, task_id=task_id, status="SUBMITTED")
            write_json(report_path, report)
            for _ in range(max(1, min(120, int(os.getenv("RUNWAY_MAX_POLLS", "60"))))):
                self.sleep(5)
                try:
                    task = request_json(self.session, "GET", "https://api.dev.runwayml.com/v1/tasks/" + task_id, headers=headers)
                except ProviderFailure as exc:
                    if exc.retryable or str(exc) == "NETWORK_RESULT_UNKNOWN":
                        continue
                    raise
                if task.get("status") == "SUCCEEDED":
                    break
                if task.get("status") in {"FAILED", "CANCELED", "CANCELLED"}:
                    raise ProviderFailure("IMAGE_TASK_" + task["status"])
            else:
                raise ProviderFailure("IMAGE_TASK_TIMEOUT_RESULT_UNKNOWN")
            urls = task.get("output") or []
            if len(urls) != 1 or not isinstance(urls[0], str) or not urls[0].startswith("https://"):
                raise ProviderFailure("INVALID_IMAGE_OUTPUT_COUNT")
            output = self._download(urls[0], directory / name)
            image_input(output)  # Verify H3 input compatibility before any video spend.
            report.update(status="SUCCEEDED", output=image_descriptor(output))
            self.budget.update(record, status="SUCCEEDED", generated_assets=1)
            return VisualAsset(output, "image", "runway-reference")
        except ProviderFailure as exc:
            report["status"] = str(exc)
            self.budget.update(record, status=str(exc))
            raise
        finally:
            report["generation_seconds"] = round(time.monotonic() - started, 3)
            write_json(report_path, report)

    def _download(self, url, stem):
        temporary = stem.with_suffix(".partial")
        try:
            with self.download_session.get(url, stream=True, timeout=(15, 90)) as response:
                if response.status_code != 200:
                    raise ProviderFailure("IMAGE_DOWNLOAD_FAILED")
                size = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 3_700_000:
                            raise ProviderFailure("IMAGE_DOWNLOAD_TOO_LARGE")
                        handle.write(chunk)
            with Image.open(temporary) as image:
                extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image.format)
                if not extension or image.width < 720 or image.height < 1280 or abs(image.width / image.height - 9 / 16) > .01:
                    raise ProviderFailure("INVALID_REFERENCE_DIMENSIONS")
                image.verify()
            output = stem.with_suffix(extension)
            os.replace(temporary, output)
            return output
        except (requests.RequestException, OSError, ValueError):
            raise ProviderFailure("IMAGE_DOWNLOAD_INVALID") from None
        finally:
            temporary.unlink(missing_ok=True)
