"""Free/open-source Clip Radar transcription and editorial render stage."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import json
from pathlib import Path
from typing import Any

from framing import analyze_video
from media_tools import ffmpeg_binary, media_summary


def extract_audio(video: Path, wav: Path):
    subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )


def _segment_confidence(segment: Any) -> float:
    no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    logprob = getattr(segment, "avg_logprob", None)
    base = 0.72 if logprob is None else max(0.0, min(1.0, float(logprob) + 1.0))
    return round(max(0.0, min(1.0, base * (1.0 - min(0.85, no_speech))), 3))


def transcribe(video: Path, model_size: str = "tiny"):
    from faster_whisper import WhisperModel

    wav = video.with_suffix(".wav")
    model_size = os.getenv("WHISPER_MODEL", model_size)
    extract_audio(video, wav)
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(wav), language="en", vad_filter=True, word_timestamps=True)
        result = []
        for segment in segments:
            text = segment.text.strip()
            confidence = _segment_confidence(segment)
            if text and confidence >= 0.28:
                result.append({
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": text,
                    "confidence": confidence,
                    "no_speech_probability": round(float(getattr(segment, "no_speech_prob", 0.0) or 0.0), 3),
                })
        return result
    finally:
        wav.unlink(missing_ok=True)


def srt_timestamp(seconds: float):
    ms = max(0, int(round(float(seconds) * 1000)))
    h, ms = ms // 3600000, ms % 3600000
    m, ms = ms // 60000, ms % 60000
    s, ms = ms // 1000, ms % 1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _wrap_caption(text: str, width: int = 24) -> str:
    words = re.sub(r"\s+", " ", str(text or "").strip()).split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            word = word[: width - 1] + "…"
        proposed = f"{current} {word}".strip()
        if current and len(proposed) > width:
            lines.append(current)
            current = word
        else:
            current = proposed
    if current:
        lines.append(current)
    if len(lines) <= 2:
        return "\n".join(lines)
    # Keep both lines readable; distribute the tail onto the second line.
    first = ""
    second_words: list[str] = []
    for word in words:
        proposed = f"{first} {word}".strip()
        if not first or len(proposed) <= width:
            first = proposed
        else:
            second_words.append(word)
    second = " ".join(second_words)
    if len(second) > width:
        second = second[: width - 1].rstrip() + "…"
    return f"{first}\n{second}" if second else first


def write_srt(entries, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index, entry in enumerate(entries, 1):
            caption = _wrap_caption(entry.get("text", ""))
            if not caption:
                continue
            # Keep one subtitle system, but pin every transcript caption to a
            # known bottom-center safe-zone anchor. Relying on implicit SRT
            # placement caused captions to land in the upper matte.
            caption = "{\\an2\\pos(360,1060)}" + caption
            handle.write(
                f"{index}\n{srt_timestamp(entry['start'])} --> {srt_timestamp(entry['end'])}\n{caption}\n\n"
            )


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, centiseconds = centiseconds // 360000, centiseconds % 360000
    minutes, centiseconds = centiseconds // 6000, centiseconds % 6000
    whole, centiseconds = centiseconds // 100, centiseconds % 100
    return f"{hours}:{minutes:02}:{whole:02}.{centiseconds:02}"


def write_ass(entries, path: Path):
    """Write one explicit-resolution libass transcript layer."""

    path.parent.mkdir(parents=True, exist_ok=True)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ClipRadar,Arial,22,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,56,56,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        for entry in entries:
            caption = _wrap_caption(entry.get("text", ""))
            if not caption:
                continue
            # PlayResX/PlayResY and an explicit position keep captions in the
            # lower safe zone across ffmpeg/libass versions.
            caption = caption.replace("\\", "\\\\").replace("\n", r"\N")
            caption = r"{\an2\pos(360,1060)}" + caption
            handle.write(
                f"Dialogue: 0,{ass_timestamp(entry['start'])},{ass_timestamp(entry['end'])},ClipRadar,,0,0,0,,{caption}\n"
            )


def split_caption_entries(entries, max_words: int = 7):
    """Split ASR segments on words while preserving confidence and safe timing."""

    result = []
    for entry in entries:
        confidence = float(entry.get("confidence", 1.0))
        if confidence < 0.28 or float(entry.get("no_speech_probability", 0.0)) > 0.72:
            continue
        words = str(entry.get("text", "")).split()
        if not words:
            continue
        chunks = [words[index:index + max_words] for index in range(0, len(words), max_words)]
        start, end = float(entry.get("start", 0.0)), float(entry.get("end", 0.0))
        total = max(end - start, 0.25)
        weight = sum(max(len(" ".join(chunk)), 1) for chunk in chunks)
        cursor = start
        for index, chunk in enumerate(chunks):
            length = total * max(len(" ".join(chunk)), 1) / weight
            chunk_end = end if index == len(chunks) - 1 else cursor + length
            result.append({
                "start": round(cursor, 3), "end": round(max(chunk_end, cursor + 0.35), 3),
                "text": " ".join(chunk), "confidence": confidence,
            })
            cursor = chunk_end
    return result


def leading_silence_seconds(source: Path) -> float:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "info", "-i", str(source), "-af", "silencedetect=n=-45dB:d=0.35", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return 0.0
    match = re.search(r"silence_end:\s*([0-9.]+)", result.stderr or "")
    if not match:
        return 0.0
    seconds = float(match.group(1))
    return seconds if 0.5 <= seconds <= 5.0 else 0.0


def _filter_path(path: Path) -> str:
    return path.as_posix().replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _content_window(duration: float, entries: list[dict[str, Any]]) -> tuple[float, float]:
    if not entries:
        return 0.0, min(duration, 65.0)
    speech_start = min(float(item["start"]) for item in entries)
    speech_end = max(float(item["end"]) for item in entries)
    start = max(0.0, speech_start - 1.25)
    end = min(duration, speech_end + 1.75)
    if end - start < 8.0:
        center = (start + end) / 2.0
        start = max(0.0, center - 4.0)
        end = min(duration, max(8.0, center + 4.0))
        if end - start < 8.0:
            start, end = 0.0, min(duration, 8.0)
    return round(start, 3), round(end, 3)


def render_vertical(source: Path, output: Path, hook: str = "CLIP RADAR"):
    """Render one vertically framed video with exactly one transcript layer."""

    output.parent.mkdir(parents=True, exist_ok=True)
    summary = media_summary(source)
    duration = float(summary.get("duration") or 0.0)
    transcript = split_caption_entries(transcribe(source))
    start, end = _content_window(duration, transcript)
    entries = []
    for entry in transcript:
        if float(entry["end"]) <= start or float(entry["start"]) >= end:
            continue
        entries.append({
            **entry,
            "start": round(max(0.0, float(entry["start"]) - start), 3),
            "end": round(min(end - start, float(entry["end"]) - start), 3),
        })
    first_caption_start = min((float(entry["start"]) for entry in entries), default=2.0)
    hook_active = round(min(1.8, max(0.0, first_caption_start - 0.08)), 3)
    subs = output.with_suffix(".ass")
    write_ass(entries, subs)
    hook_text = re.sub(r"\s+", " ", (hook or "CLIP RADAR").replace("\n", " ")).strip()[:48]
    hook_file = output.with_suffix(".hook.txt")
    hook_file.write_text(_wrap_caption(hook_text, 32), encoding="utf-8")
    framing = analyze_video(source)
    manifest = {
        "schema_version": 2,
        "source": str(source),
        "source_duration": round(duration, 3),
        "content_window": {"start": start, "end": end, "duration": round(end - start, 3), "editorial_basis": "setup_action_payoff_from_speech"},
        "hook": {"text": hook_text, "type": "title_card_drawtext", "active_seconds": hook_active, "is_subtitle": False},
        "subtitles": {
            "system": "ffmpeg-libass-ass-transcript",
            "subtitle_systems": 1,
            "caption_layers": 1,
            "hook_is_subtitle": False,
            "file": str(subs),
            "count": len(entries),
            "max_lines": 2,
            "max_chars_per_line": 24,
            "safe_zone": {"left": 56, "right": 56, "bottom": 185, "top": 870},
            "positioning": "explicit_ass_bottom_center",
            "position": {"anchor": "bottom_center", "x": 360, "y": 1060},
            "entries": entries,
        },
        "framing": framing,
        "render_profile": {
            "canvas": "720x1280",
            "main_source": "full_frame_fit",
            "zoom_ratio": 1.0,
            "black_bars_allowed": False,
            "source_caption_policy": "central_lower_band_masked_before_single_transcript",
            "source_caption_mask": {"x": 116, "y": 672, "width": 488, "height": 47},
        },
    }
    manifest_path = output.with_suffix(".render.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    subtitle_path, hook_path = _filter_path(subs), _filter_path(hook_file)
    vf = (
        "split=2[bg][fg];"
        "[bg]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=24:12,eq=brightness=-0.35:saturation=0.9[bgv];"
        "[fg]scale=720:1280:force_original_aspect_ratio=decrease,setsar=1[fgv];"
        # Center the preserved source in the 9:16 canvas.  A fixed vertical
        # offset crops portrait Twitch clips and turns most of the render into
        # blurred/dark background; the expression handles both portrait and
        # landscape sources without a blind crop.
        "[bgv][fgv]overlay=(W-w)/2:(H-h)/2,"
        f"drawbox=x=116:y=672:w=488:h=47:color=black@0.82:t=fill,"
        f"subtitles='{subtitle_path}':original_size=720x1280:force_style='FontName=Arial,FontSize=22,Outline=3,Shadow=1,Alignment=2,MarginL=56,MarginR=56,MarginV=185,WrapStyle=2',"
        f"drawbox=x=28:y=50:w=664:h=128:color=black@0.48:t=fill:enable='between(t,0,{hook_active:.3f})',"
        f"drawtext=textfile='{hook_path}':fontcolor=white:fontsize=28:borderw=3:bordercolor=black:x=(w-text_w)/2:y=72:line_spacing=5:enable='between(t,0,{hook_active:.3f})',"
        "drawtext=text='CLIP RADAR':fontcolor=white:fontsize=20:borderw=2:bordercolor=black:x=w-text_w-20:y=18"
    )
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}", "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)]
    try:
        subprocess.run(command, check=True)
    finally:
        hook_file.unlink(missing_ok=True)
    return entries
