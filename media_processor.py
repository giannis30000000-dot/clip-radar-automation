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


def write_srt(entries, path: Path, position: tuple[int, int] = (360, 1060)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index, entry in enumerate(entries, 1):
            caption = _wrap_caption(entry.get("text", ""))
            if not caption:
                continue
            # Keep one subtitle system, but pin every transcript caption to a
            # known bottom-center safe-zone anchor. Relying on implicit SRT
            # placement caused captions to land in the upper matte.
            caption = f"{{\\an2\\pos({int(position[0])},{int(position[1])})}}" + caption
            handle.write(
                f"{index}\n{srt_timestamp(entry['start'])} --> {srt_timestamp(entry['end'])}\n{caption}\n\n"
            )


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, centiseconds = centiseconds // 360000, centiseconds % 360000
    minutes, centiseconds = centiseconds // 6000, centiseconds % 6000
    whole, centiseconds = centiseconds // 100, centiseconds % 100
    return f"{hours}:{minutes:02}:{whole:02}.{centiseconds:02}"


def write_ass(entries, path: Path, position: tuple[int, int] = (360, 1060), font_size: int = 22):
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
    header = header.replace("ClipRadar,Arial,22,", f"ClipRadar,Arial,{int(font_size)},")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        for entry in entries:
            caption = _wrap_caption(entry.get("text", ""))
            if not caption:
                continue
            # PlayResX/PlayResY and an explicit position keep captions in the
            # lower safe zone across ffmpeg/libass versions.
            caption = caption.replace("{", "(").replace("}", ")").replace("\\", "\\\\").replace("\n", r"\N")
            caption = f"{{\\an2\\pos({int(position[0])},{int(position[1])})}}" + caption
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


def _crop_source(label: str, region: dict[str, Any], width: int, height: int, output_label: str) -> str:
    x = max(0.0, min(0.96, float(region.get("x", 0.0))))
    y = max(0.0, min(0.96, float(region.get("y", 0.0))))
    crop_width = max(0.04, min(1.0 - x, float(region.get("width", 1.0))))
    crop_height = max(0.04, min(1.0 - y, float(region.get("height", 1.0))))
    return (
        f"[{label}]crop=w=iw*{crop_width:.4f}:h=ih*{crop_height:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[{output_label}]"
    )


def _adaptive_filter(framing: dict[str, Any], subtitle_path: str, hook_path: str, hook_active: float) -> str:
    """Build a category-specific composition with only small support mattes."""

    category = str(framing.get("layout_category") or "UNKNOWN")
    profile = framing.get("render_profile") or {}
    main = {
        "x": float(profile.get("main_x", 0.0)), "y": float(profile.get("main_y", 0.0)),
        "width": float(profile.get("main_w", 1.0)), "height": float(profile.get("main_h", 1.0)),
    }
    facecam = framing.get("region_manifest", {}).get("facecam")
    panel_y = int(profile.get("panel_y", 438))
    panel_h = int(profile.get("panel_h", 405))
    if category == "UNKNOWN":
        # This path is review-only and is never publish-eligible. A direct
        # deterministic scale avoids ffmpeg's odd-dimension pad failures on
        # unusual source encodings while QC still rejects the low-occupancy
        # fallback via layout_approved and visual_quality_score.
        main_branch = "[main_src]scale=720:405,setsar=1[main]"
    else:
        main_branch = _crop_source("main_src", main, 720, panel_h, "main")
    needs_cam = category in {"GAMEPLAY_WITH_FACECAM", "BROWSER_REACTION"} and bool(facecam)
    split_labels = "[bgsrc][main_src][cam_src]" if needs_cam else "[bgsrc][main_src]"
    parts = [f"[0:v]split={3 if needs_cam else 2}{split_labels};", "[bgsrc]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=18:8,eq=brightness=0.10:contrast=1.04:saturation=1.05[bg];", main_branch + ";"]
    parts.append(f"[bg][main]overlay=0:{panel_y}[layout0];")
    layout_label = "layout0"
    if needs_cam:
        cam_region = {"x": float(facecam.get("x", 0.0)), "y": float(facecam.get("y", 0.0)), "width": float(facecam.get("width", 0.25)), "height": float(facecam.get("height", 0.3))}
        parts.append(_crop_source("cam_src", cam_region, 300, 260, "cam") + ";")
        parts.append(f"[layout0][cam]overlay=24:54[layout1];")
        layout_label = "layout1"
    mask = profile.get("mask") if framing.get("source_caption_detected") else None
    masked_label = layout_label
    if mask:
        mask_x, mask_y = int(mask.get("x", 94)), int(mask.get("y", 900))
        mask_w, mask_h = int(mask.get("width", 532)), int(mask.get("height", 64))
        parts.append(f"[{layout_label}]split=2[composed][band_source];[band_source]crop={mask_w}:{mask_h}:{mask_x}:{mask_y},boxblur=10:2[caption_band];[composed][caption_band]overlay={mask_x}:{mask_y}[masked];")
        masked_label = "masked"
    parts.append(f"[{masked_label}]subtitles='{subtitle_path}':original_size=720x1280:force_style='FontName=Arial,FontSize=22,Outline=3,Shadow=1,Alignment=2,MarginL=56,MarginR=56,MarginV=170,WrapStyle=2'")
    if hook_active > 0.0:
        parts.append(f",drawbox=x=28:y=50:w=664:h=128:color=black@0.48:t=fill:enable='between(t,0,{hook_active:.3f})',drawtext=textfile='{hook_path}':fontcolor=white:fontsize=28:borderw=3:bordercolor=black:x=(w-text_w)/2:y=72:line_spacing=5:enable='between(t,0,{hook_active:.3f})'")
    parts.append(",drawtext=text='CLIP RADAR':fontcolor=white:fontsize=20:borderw=2:bordercolor=black:x=w-text_w-20:y=18[vout]")
    return "".join(parts)


def render_vertical(source: Path, output: Path, hook: str = "CLIP RADAR"):
    """Render one adaptive vertical video with exactly one transcript layer."""

    output.parent.mkdir(parents=True, exist_ok=True)
    summary = media_summary(source)
    duration = float(summary.get("duration") or 0.0)
    framing = analyze_video(source)
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
    # ASR segments occasionally overlap after short chunks are expanded to a
    # readable minimum duration. Keep exactly one readable timeline: later
    # captions start at the prior caption's end, and unusably short remnants
    # are dropped instead of being rendered on top of each other.
    entries.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    timed_entries: list[dict[str, Any]] = []
    for entry in entries:
        caption_start = float(entry["start"])
        caption_end = float(entry["end"])
        if timed_entries:
            caption_start = max(caption_start, float(timed_entries[-1]["end"]))
        if caption_end - caption_start < 0.18:
            continue
        timed_entries.append({**entry, "start": round(caption_start, 3), "end": round(caption_end, 3)})
    entries = timed_entries
    first_caption_start = min((float(entry["start"]) for entry in entries), default=2.0)
    hook_active = round(min(1.1, max(0.0, first_caption_start - 0.08)), 3)
    if hook_active < 0.35:
        hook_active = 0.0
    profile = framing.get("render_profile") or {}
    subtitle_y = int(profile.get("subtitle_y", 1060))
    subs = output.with_suffix(".ass")
    write_ass(entries, subs, position=(360, subtitle_y))
    hook_text = re.sub(r"\s+", " ", (hook or "CLIP RADAR").replace("\n", " ")).strip()[:48]
    hook_file = output.with_suffix(".hook.txt")
    hook_file.write_text(_wrap_caption(hook_text, 32), encoding="utf-8")
    mask = profile.get("mask") if framing.get("source_caption_detected") else None
    manifest = {
        "schema_version": 3,
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
            "safe_zone": {"left": 56, "right": 56, "bottom": 150, "top": 900},
            "positioning": "adaptive_ass_bottom_center",
            "position": {"anchor": "bottom_center", "x": 360, "y": subtitle_y},
            "entries": entries,
        },
        "framing": framing,
        "render_profile": {
            "canvas": "720x1280",
            "layout_category": framing.get("layout_category"),
            "main_source": framing.get("layout"),
            "zoom_ratio": framing.get("zoom_ratio", 1.0),
            "black_bars_allowed": False,
            "blurred_background_role": "support_only_for_unused_canvas_area",
            "source_caption_policy": "detected_region_local_blur_before_single_transcript",
            "source_caption_mask": mask or {"detected": False, "reason": "no_reliable_source_caption_region"},
            "source_caption_mask_style": "blurred_texture_localized",
            "subtitle_position": {"x": 360, "y": subtitle_y, "safe_zone": "adaptive_layout_lower_safe_zone"},
        },
    }
    manifest_path = output.with_suffix(".render.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    subtitle_path, hook_path = _filter_path(subs), _filter_path(hook_file)
    vf = _adaptive_filter(framing, subtitle_path, hook_path, hook_active)
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}", "-filter_complex", vf, "-map", "[vout]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)]
    try:
        subprocess.run(command, check=True)
    except Exception:
        # ffmpeg can create a zero-byte or truncated output before reporting a
        # filter/download failure. Never leave that artifact for later QC.
        output.unlink(missing_ok=True)
        raise
    finally:
        hook_file.unlink(missing_ok=True)
    return entries


def render_story(story: dict, assets, audio: Path, captions: list[dict], output: Path) -> dict:
    """Assemble provider images or clips using the existing ffmpeg/ASS helpers.

    All scene boundaries are quantized from the narration timeline, avoiding
    cumulative frame rounding drift across independent scene encodes.
    """
    import math
    import tempfile

    output.parent.mkdir(parents=True, exist_ok=True)
    if len(assets) != len(story["scenes"]):
        raise ValueError("one visual asset is required per scene")
    duration = float(story["actual_voice_duration_seconds"])
    subtitles = output.with_suffix(".ass")
    write_ass(captions, subtitles, position=(360, 1060), font_size=38)
    write_srt(captions, output.with_suffix(".srt"))
    scene_reports = []
    temporary_output = output.with_name(output.stem + ".partial.mp4")
    try:
        with tempfile.TemporaryDirectory(prefix="story-render-", dir=output.parent) as scratch:
            work = Path(scratch)
            frame_boundaries = [round(s["start_time"] * 30) for s in story["scenes"]] + [math.ceil(duration * 30)]
            for index, (scene, asset) in enumerate(zip(story["scenes"], assets)):
                if asset.kind not in {"image", "video"} or not asset.path.is_file() or not asset.path.stat().st_size:
                    raise ValueError(f"scene {index+1}: empty or unsupported visual asset")
                frames = frame_boundaries[index+1] - frame_boundaries[index]
                if frames < 1:
                    raise ValueError("empty scene timeline")
                segment = work / f"scene_{index:02}.mp4"
                source_args = ["-loop", "1", "-framerate", "30"] if asset.kind == "image" else []
                filters = "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1"
                if asset.kind == "image":
                    direction = "on" if index % 2 == 0 else f"({frames}-on)"
                    filters += f",zoompan=z='1+0.025*{direction}/{frames}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=720x1280:fps=30"
                else:
                    # A generated clip may be shorter than narration; hold its
                    # last frame explicitly, and leave the decision in metadata.
                    filters += f",fps=30,tpad=stop_mode=clone:stop_duration={frames/30:.6f}"
                    filters += ",drawtext=text='CLIP RADAR':fontcolor=white:fontsize=18:borderw=2:bordercolor=black@0.6:x=48:y=80"
                command = [ffmpeg_binary(), "-v", "error", "-y", *source_args, "-i", str(asset.path.resolve()), "-an", "-vf", filters, "-frames:v", str(frames), "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", str(segment)]
                subprocess.run(command, check=True, capture_output=True, timeout=180)
                measured = media_summary(segment)
                if not measured["has_video"] or abs(measured["duration"] - frames / 30) > .1:
                    raise ValueError(f"scene {index+1} failed render validation")
                scene_reports.append({"scene_number": index+1, "start_time": frame_boundaries[index]/30, "duration": frames/30, "frames": frames, "asset": os.path.relpath(asset.path, output.parent).replace("\\", "/"), "asset_kind": asset.kind, "provider": asset.provider, "encoded_duration": measured["duration"]})
            concat = work / "scenes.txt"
            # Relative generated filenames contain no user-controlled quoting.
            concat.write_text("".join(f"file 'scene_{i:02}.mp4'\n" for i in range(len(assets))), encoding="utf-8")
            joined = work / "joined.mp4"
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(concat), "-c", "copy", str(joined)], check=True, capture_output=True, timeout=120)
            subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-i", str(joined), "-i", str(audio), "-vf", f"subtitles='{_filter_path(subtitles.resolve())}':original_size=720x1280", "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.6f}", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(temporary_output)], check=True, capture_output=True, timeout=300)
        os.replace(temporary_output, output)
    finally:
        temporary_output.unlink(missing_ok=True)
    manifest = {
        "schema_version": 1, "mode": "ORIGINAL_STORY_MODE", "story_id": story["story_id"],
        "duration": duration, "canvas": [720, 1280], "fps": 30, "scenes": scene_reports,
        "subtitles": {"file": subtitles.name, "count": len(captions), "entries": captions, "caption_layers": 1, "font_size": 38, "position": {"x": 360, "y": 1060}, "timing_basis": story.get("generation", {}).get("voice", {}).get("timing_basis", "synthesized_phrase_pcm")},
        "branding": "subtle_scene_overlay" if any(a.kind == "video" for a in assets) else "subtle_scene_footer", "audio": os.path.relpath(audio, output.parent).replace("\\", "/"), "publishing_enabled": False,
    }
    output.with_suffix(".render.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
