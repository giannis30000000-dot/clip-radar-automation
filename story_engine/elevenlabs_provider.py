"""Single natural narration request, aligned from returned character timestamps."""
from __future__ import annotations

import base64
import copy
import math
import os
from pathlib import Path
import re
import subprocess
import time
import requests

from media_tools import ffmpeg_binary
from .history import write_json
from .provider_http import ProviderFailure, attempts, rate, request_json
from .voice import caption_phrases, wav_duration
from .schema import validate_story
from .costs import sanitize


def aligned_captions(story, alignment, duration):
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    text = "".join(chars)
    if text != story["full_script"] or not len(chars) == len(starts) == len(ends):
        raise ValueError("alignment does not match narration")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in [*starts, *ends]):
        raise ValueError("nonfinite alignment")
    if not all(0 <= start <= end <= duration + .1 for start, end in zip(starts, ends)):
        raise ValueError("alignment outside audio")
    if any(b < a for a, b in zip(starts, starts[1:])) or any(b < a for a, b in zip(ends, ends[1:])):
        raise ValueError("nonmonotonic alignment")
    captions, cursor = [], 0
    for scene in story["scenes"]:
        for phrase in caption_phrases(scene["narration"]):
            start = text.index(phrase, cursor)
            end = start + len(phrase) - 1
            cursor = end + 1
            caption_start = max(starts[start], captions[-1]["end"] if captions else 0)
            if ends[end] <= caption_start:
                raise ValueError("empty aligned caption")
            captions.append({"text": phrase, "scene_number": scene["scene_number"], "start": round(caption_start, 3), "end": round(ends[end], 3), "confidence": 1.0})
    for index, scene in enumerate(story["scenes"]):
        start = 0 if index == 0 else next(c["start"] for c in captions if c["scene_number"] == index + 1)
        end = duration if index == len(story["scenes"])-1 else next(c["start"] for c in captions if c["scene_number"] == index + 2)
        scene.update(start_time=round(start, 3), estimated_duration=round(end - start, 3))
    story["estimated_voice_duration_seconds"] = round(duration, 3)
    story["actual_voice_duration_seconds"] = round(duration, 3)
    return captions


class ElevenLabsVoiceProvider:
    def __init__(self, budget, session=None):
        self.budget = budget
        self.session = session or requests.Session()

    def synthesize(self, story, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        voice = os.environ["ELEVENLABS_VOICE_ID"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
            raise ProviderFailure("INVALID_VOICE_ID")
        model = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
        price = rate("ELEVENLABS_USD_PER_1K_CHARACTERS", ".10" if model == "eleven_multilingual_v2" else None)
        text = story["full_script"]
        raw, audio = directory / "elevenlabs.mp3", directory / "narration.wav"
        for retry in range(attempts()):
            record = self.budget.reserve("elevenlabs", model, price * len(text) / 1000, retry=retry, characters=len(text))
            try:
                response = request_json(self.session, "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, params={"output_format": "mp3_44100_128"}, json={"text": text, "model_id": model})
                # Only approved metadata is persisted; never save the raw response.
                data = base64.b64decode(response["audio_base64"], validate=True)
                if not data or len(data) > 25 * 1024 * 1024:
                    raise ValueError("invalid audio size")
                raw.write_bytes(data)
                self._decode(raw, audio)
                duration = wav_duration(audio)
                if not 60 <= duration <= 75:
                    raise ProviderFailure("NARRATION_DURATION_REQUIRES_REVIEW")
                timed_story = copy.deepcopy(story)
                captions = aligned_captions(timed_story, response["alignment"], duration)
                validate_story(timed_story)
                metadata = sanitize({"provider": "elevenlabs", "model": model, "voice_id": voice, "development_only": False, "duration": round(duration, 3), "timing_basis": "provider_character_alignment", "characters": len(text)})
                write_json(directory / "voice.provider.json", sanitize(metadata))
                self.budget.update(record, status="SUCCEEDED", generated_seconds=duration, generated_assets=1)
                story.update(timed_story)
                return audio, captions, metadata
            except ProviderFailure as exc:
                self.budget.update(record, status=str(exc))
                if not exc.retryable:
                    raise
            except (ValueError, KeyError, TypeError, IndexError):
                self.budget.update(record, status="INVALID_AUDIO_OR_ALIGNMENT")
            if retry + 1 < attempts():
                time.sleep(min(2 ** retry, 4))
        raise ProviderFailure("VOICE_ATTEMPTS_EXHAUSTED")

    @staticmethod
    def _decode(raw, audio):
        try:
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=7", "-ac", "1", "-ar", "44100", str(audio)], capture_output=True, check=True, timeout=120)
        except subprocess.SubprocessError:
            raise ProviderFailure("VOICE_DECODE_FAILED") from None
