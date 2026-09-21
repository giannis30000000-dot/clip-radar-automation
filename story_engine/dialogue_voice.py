"""Per-character voices, measured per-line timing, one speaker-aware caption track."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import wave

from media_tools import ffmpeg_binary
from .costs import sanitize
from .history import write_json
from .provider_http import ProviderFailure, attempts, rate, request_json
from .voice import caption_phrases, trimmed_pcm, wav_duration


def assign_voices(story, production=False):
    ids = list(dict.fromkeys(l["speaker_id"] for l in story["dialogue"]))
    if not production:
        return {speaker: f"development-{i}" for i, speaker in enumerate(ids)}
    try:
        configured = json.loads(os.getenv("STORY_VOICE_MAP") or "{}")
        pool = json.loads(os.getenv("STORY_VOICE_POOL") or "[]")
        if not isinstance(configured, dict) or not isinstance(pool, list):
            raise ValueError("invalid voice map")
        mapping = {speaker: configured.get(speaker) or (os.getenv("ELEVENLABS_VOICE_ID") if speaker == "narrator" else None) for speaker in ids}
        available = [v for v in pool if v not in mapping.values()]
        for speaker in ids:
            if not mapping[speaker]:
                mapping[speaker] = available.pop(0) if available else None
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in mapping.values()) or len(set(mapping.values())) != len(mapping):
            raise ValueError("distinct voice IDs required")
        return mapping
    except (ValueError, TypeError):
        raise ProviderFailure("DIALOGUE_VOICE_MAP_REQUIRED") from None


def _paid_line(provider, line, voice, directory):
    from .elevenlabs_provider import aligned_captions
    model = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    price = rate("ELEVENLABS_USD_PER_1K_CHARACTERS", ".10" if model == "eleven_multilingual_v2" else None)
    raw, audio = directory / "line.mp3", directory / "line.wav"
    for retry in range(attempts()):
        record = provider.budget.reserve("elevenlabs", model, price * len(line["text"]) / 1000, retry=retry, characters=len(line["text"]), speaker_id=line["speaker_id"], line_order=line["order"])
        try:
            response = request_json(provider.session, "POST", f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, params={"output_format": "mp3_44100_128"}, json={"text": line["text"], "model_id": model})
            data = base64.b64decode(response["audio_base64"], validate=True)
            if not data or len(data) > 5 * 1024 * 1024:
                raise ValueError("invalid line audio")
            raw.write_bytes(data)
            provider._decode(raw, audio)
            duration = wav_duration(audio)
            if not .1 <= duration <= 12:
                raise ValueError("line too long")
            timed = {"full_script": line["text"], "scenes": [{"scene_number": 1, "narration": line["text"]}]}
            captions = aligned_captions(timed, response["alignment"], duration)
            with wave.open(str(audio), "rb") as source:
                if source.getsampwidth() != 2 or source.getnchannels() != 1 or source.getframerate() != 44100:
                    raise ValueError("unexpected decoded audio format")
                pcm = source.readframes(source.getnframes())
            provider.budget.update(record, status="SUCCEEDED", generated_seconds=duration, generated_assets=1)
            write_json(directory / "voice.provider.json", sanitize({"voice_id": voice, "model": model, "speaker_id": line["speaker_id"], "duration": duration, "emotion_hint": line["emotion"]}))
            return pcm, 44100, captions
        except ProviderFailure as exc:
            provider.budget.update(record, status=str(exc))
            if not exc.retryable:
                raise
        except (ValueError, KeyError, TypeError, IndexError):
            provider.budget.update(record, status="INVALID_LINE_AUDIO_OR_ALIGNMENT")
    raise ProviderFailure("DIALOGUE_VOICE_ATTEMPTS_EXHAUSTED")


def _development_lines(story, directory, mapping):
    segments = []
    for line in story["dialogue"]:
        for phrase in caption_phrases(line["text"]):
            segments.append({"text": phrase, "line": line["order"], "path": str((directory / f"phrase_{len(segments):03}.wav").resolve()), "voice_index": int(mapping[line["speaker_id"]].split("-")[-1]), "rate": 1})
    executable = os.getenv("ESPEAK_BINARY") or shutil.which("espeak-ng") or shutil.which("espeak")
    if executable:
        variants = ("en-us+m1", "en-us+f2", "en-us+m3", "en-us+f4", "en-us")
        for item in segments:
            subprocess.run([executable, "-v", variants[item["voice_index"] % len(variants)], "-s", "165", "-w", item["path"], "--stdin"], input=item["text"], text=True, capture_output=True, check=True, timeout=30)
    elif os.name == "nt":
        manifest = directory / "speech_input.json"
        write_json(manifest, segments)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("sapi_voice.ps1")), "-Manifest", str(manifest)], check=True, capture_output=True, timeout=180)
    else:
        raise RuntimeError("Install espeak-ng for development dialogue")
    results = []
    for line in story["dialogue"]:
        chunks, captions, frames, sample_rate = [], [], 0, None
        for item in (s for s in segments if s["line"] == line["order"]):
            pcm, hz = trimmed_pcm(Path(item["path"]))
            if sample_rate is not None and hz != sample_rate:
                raise ValueError("inconsistent development sample rate")
            sample_rate = hz
            start = frames / hz
            chunks.append(pcm)
            frames += len(pcm) // 2
            captions.append({"text": item["text"], "start": start, "end": frames / hz, "confidence": 1.0})
            pause = int(hz * .04)
            chunks.append(b"\0\0" * pause)
            frames += pause
        results.append((b"".join(chunks), sample_rate, captions))
    return results


def apply_timeline(story, durations):
    cursor = 0.0
    for line, duration in zip(story["dialogue"], durations):
        line.update(intended_start_time=round(cursor, 3), estimated_duration_seconds=round(duration, 3))
        cursor += duration
    for scene in story["scenes"]:
        group = [l for l in story["dialogue"] if l["scene_number"] == scene["scene_number"]]
        scene.update(start_time=group[0]["intended_start_time"], estimated_duration=round(sum(l["estimated_duration_seconds"] for l in group), 3))
    story["actual_voice_duration_seconds"] = story["estimated_voice_duration_seconds"] = round(cursor, 3)


def synthesize_dialogue(story, directory, production=None):
    directory.mkdir(parents=True, exist_ok=True)
    mapping = assign_voices(story, production=production is not None)
    if production:
        results = []
        for line in story["dialogue"]:
            folder = directory / f"line_{line['order']:03}"
            folder.mkdir(parents=True, exist_ok=True)
            results.append(_paid_line(production, line, mapping[line["speaker_id"]], folder))
    else:
        results = _development_lines(story, directory, mapping)
    chunks, captions, durations, frames = [], [], [], 0
    hz = results[0][1]
    for line, (pcm, rate_hz, local_captions) in zip(story["dialogue"], results):
        if rate_hz != hz:
            raise ValueError("mixed dialogue audio rates")
        offset = frames / hz
        for caption in local_captions:
            captions.append({**caption, "start": round(offset + caption["start"], 3), "end": round(offset + caption["end"], 3), "scene_number": line["scene_number"], "speaker_id": line["speaker_id"], "line_order": line["order"]})
        pause = int(hz * .12)
        chunks.extend([pcm, b"\0\0" * pause])
        count = len(pcm) // 2 + pause
        frames += count
        durations.append(count / hz)
    raw, audio = directory / "dialogue_raw.wav", directory / "narration.wav"
    with wave.open(str(raw), "wb") as output:
        output.setparams((1, 2, hz, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(chunks))
    subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", str(hz), "-ac", "1", str(audio)], capture_output=True, check=True, timeout=120)
    duration = wav_duration(audio)
    factor = duration / (frames / hz)  # Only measured resampling length, never time stretching.
    if abs(factor - 1) > .005:
        raise ValueError("unexpected audio duration change")
    for c in captions:
        c["start"], c["end"] = round(c["start"] * factor, 3), round(c["end"] * factor, 3)
        c["speaker_color"] = ("FFFFFF", "80E6FF", "FFD0A0", "B0FFB0", "E0B0FF")[list(mapping).index(c["speaker_id"]) % 5]
    apply_timeline(story, [d * factor for d in durations])
    if story["dialogue"][0]["text"] == story["hook"]:
        story["hook_duration_seconds"] = max(c["end"] for c in captions if c["line_order"] == 1)
    story["voice_assignments"] = mapping
    metadata = {"provider": "elevenlabs-dialogue" if production else "development-dialogue", "development_only": production is None, "voice_assignments": mapping, "timing_basis": "provider_character_alignment" if production else "measured_synthesized_phrase_pcm", "duration": duration, "tempo_factor": 1.0, "line_count": len(results)}
    write_json(directory / "dialogue_timing.json", sanitize({"voice": metadata, "dialogue": story["dialogue"], "captions": captions}))
    return audio, captions, metadata
