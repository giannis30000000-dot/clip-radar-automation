"""Offline development narration with captions measured from the actual PCM.

Each readable phrase is synthesized once. Its exact audio interval is its
caption interval; no word-count approximation is used for rendered timing.
"""
from __future__ import annotations

from array import array
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import wave

from media_tools import ffmpeg_binary


def caption_phrases(text: str) -> list[str]:
    phrases, current = [], []
    for word in text.split():
        proposed = " ".join([*current, word])
        if current and (len(proposed) > 42 or len(current) >= 6):
            phrases.append(" ".join(current))
            current = []
        if len(word) > 24:
            raise ValueError("Narration contains a word too long for mobile captions")
        current.append(word)
        if re.search(r'[.!?;:]$', word):
            phrases.append(" ".join(current))
            current = []
    if current:
        phrases.append(" ".join(current))
    return phrases


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def trimmed_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("development TTS must return mono 16-bit PCM")
        rate = source.getframerate()
        samples = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    voiced = [i for i, sample in enumerate(samples) if abs(sample) > 180]
    if not voiced:
        raise ValueError("TTS returned silent narration")
    # Preserve consonants and a small natural tail; remove synthesis padding.
    start = max(0, voiced[0] - int(rate * .025))
    end = min(len(samples), voiced[-1] + int(rate * .07))
    trimmed = samples[start:end]
    if sys.byteorder != "little":
        trimmed.byteswap()
    return trimmed.tobytes(), rate


class DevelopmentVoiceProvider:
    def synthesize(self, story: dict, directory: Path) -> tuple[Path, list[dict], dict]:
        if "dialogue" in story:
            from .dialogue_voice import synthesize_dialogue
            return synthesize_dialogue(story, directory)
        directory.mkdir(parents=True, exist_ok=True)
        segments = []
        for scene in story["scenes"]:
            for text in caption_phrases(scene["narration"]):
                segments.append({"text": text, "scene_number": scene["scene_number"], "path": str((directory / f"phrase_{len(segments):03}.wav").resolve())})
        executable = os.getenv("ESPEAK_BINARY") or shutil.which("espeak-ng") or shutil.which("espeak")
        if executable:
            provider = "espeak-ng-development"
            for segment in segments:
                subprocess.run([executable, "-v", "en-us", "-s", "175", "-w", segment["path"], "--stdin"], input=segment["text"], text=True, capture_output=True, check=True, timeout=30)
        elif os.name == "nt":
            provider = "windows-sapi-development"
            manifest = directory / "speech_input.json"
            manifest.write_text(json.dumps(segments), encoding="utf-8")
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("sapi_voice.ps1")), "-Manifest", str(manifest.resolve())], check=True, capture_output=True, text=True, timeout=180)
        else:
            raise RuntimeError("Install espeak-ng for free offline development narration")
        chunks, captions, frames, sample_rate = [], [], 0, None
        for segment in segments:
            pcm, rate = trimmed_pcm(Path(segment["path"]))
            if sample_rate is not None and sample_rate != rate:
                raise ValueError("inconsistent TTS sample rates")
            sample_rate = rate
            start = frames / rate
            frames += len(pcm) // 2
            end = frames / rate
            captions.append({"text": segment["text"], "scene_number": segment["scene_number"], "start": start, "end": end, "confidence": 1.0})
            chunks.append(pcm)
            pause = int(rate * (.12 if segment["text"].endswith((".", "!", "?")) else .045))
            chunks.append(b"\0\0" * pause)
            frames += pause
        raw = directory / "narration_raw.wav"
        with wave.open(str(raw), "wb") as output:
            output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            output.writeframes(b"".join(chunks))
        raw_duration = frames / sample_rate
        speed = max(.8, min(1.3, raw_duration / story["target_duration_seconds"]))
        final = directory / "narration.wav"
        subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-i", str(raw), "-af", f"atempo={speed:.8f},loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", str(sample_rate), "-ac", "1", str(final)], check=True, capture_output=True, timeout=120)
        duration = wav_duration(final)
        # atempo changes the PCM length slightly. Scale to measured output,
        # rather than trusting the requested factor or a text estimate.
        factor = duration / raw_duration
        for item in captions:
            item["start"] = round(item["start"] * factor, 3)
            item["end"] = round(item["end"] * factor, 3)
        for index, scene in enumerate(story["scenes"]):
            start = next(c["start"] for c in captions if c["scene_number"] == scene["scene_number"])
            end = duration if index == len(story["scenes"]) - 1 else next(c["start"] for c in captions if c["scene_number"] == scene["scene_number"] + 1)
            scene["start_time"] = round(start, 3)
            scene["estimated_duration"] = round(end - start, 3)
        story["estimated_voice_duration_seconds"] = round(duration, 3)
        story["actual_voice_duration_seconds"] = round(duration, 3)
        metadata = {"provider": provider, "development_only": True, "timing_basis": "measured_synthesized_phrase_pcm", "raw_duration": round(raw_duration, 3), "duration": round(duration, 3), "tempo_factor": round(speed, 4)}
        return final, captions, metadata
