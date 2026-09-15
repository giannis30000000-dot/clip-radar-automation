"""Free/open-source Clip Radar media processing stage.

Runs only after the source layer provides an authorized local media file.
Uses ffmpeg + faster-whisper; no paid transcription API is required.
"""
from pathlib import Path
import subprocess
import os
import re
import textwrap

from media_tools import ffmpeg_binary


def extract_audio(video: Path, wav: Path):
    subprocess.run([ffmpeg_binary(),"-hide_banner","-loglevel","error","-y","-i",str(video),"-vn","-ac","1","-ar","16000",str(wav)], check=True)


def transcribe(video: Path, model_size: str = "tiny"):
    from faster_whisper import WhisperModel
    wav=video.with_suffix(".wav")
    model_size=os.getenv("WHISPER_MODEL", model_size)
    extract_audio(video,wav)
    try:
        model=WhisperModel(model_size, device="cpu", compute_type="int8")
        segments,info=model.transcribe(str(wav), language="en", vad_filter=True, word_timestamps=True)
        result=[]
        for seg in segments:
            if seg.text.strip():
                result.append({"start":seg.start,"end":seg.end,"text":seg.text.strip()})
        return result
    finally:
        wav.unlink(missing_ok=True)


def srt_timestamp(seconds: float):
    ms=int(round(seconds*1000)); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(entries, path: Path):
    with path.open("w",encoding="utf-8") as f:
        for i,e in enumerate(entries,1):
            caption=textwrap.fill(e["text"], width=30, max_lines=2, placeholder="…")
            f.write(f"{i}\n{srt_timestamp(e['start'])} --> {srt_timestamp(e['end'])}\n{caption}\n\n")


def split_caption_entries(entries, max_words=7):
    """Keep captions readable by splitting long ASR segments with timed chunks."""
    result=[]
    for entry in entries:
        words=entry["text"].split()
        if len(words) <= max_words:
            result.append(entry)
            continue
        chunks=[words[index:index+max_words] for index in range(0,len(words),max_words)]
        total=max(entry["end"]-entry["start"],0.1)
        weight=sum(max(len(" ".join(chunk)),1) for chunk in chunks)
        cursor=entry["start"]
        for index,chunk in enumerate(chunks):
            length=total*max(len(" ".join(chunk)),1)/weight
            end=entry["end"] if index == len(chunks)-1 else cursor+length
            result.append({"start":cursor,"end":end,"text":" ".join(chunk)})
            cursor=end
    return result


def leading_silence_seconds(source: Path) -> float:
    """Detect a short silent lead so the final opens on content when present."""
    result=subprocess.run([
        ffmpeg_binary(),"-hide_banner","-loglevel","info","-i",str(source),"-af","silencedetect=n=-45dB:d=0.35","-f","null","-"
    ],capture_output=True,text=True)
    if result.returncode:
        return 0.0
    match=re.search(r"silence_end:\s*([0-9.]+)", result.stderr or "")
    if not match:
        return 0.0
    seconds=float(match.group(1))
    return seconds if 0.5 <= seconds <= 5.0 else 0.0


def _filter_path(path: Path) -> str:
    return path.as_posix().replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render_vertical(source: Path, output: Path, hook: str = "CLIP RADAR"):
    subs=output.with_suffix(".srt")
    start=leading_silence_seconds(source)
    entries=split_caption_entries(transcribe(source))
    if start:
        entries=[
            {"start":max(0, e["start"]-start),"end":max(0, e["end"]-start),"text":e["text"]}
            for e in entries if e["end"] > start
        ]
    write_srt(entries,subs)
    hook_file=output.with_suffix(".hook.txt")
    hook_file.write_text(textwrap.fill((hook or "CLIP RADAR").replace("\n", " "), width=28, max_lines=2, placeholder="…"), encoding="utf-8")
    subtitle_path=_filter_path(subs)
    hook_path=_filter_path(hook_file)
    # Fit the full source into a 9:16 canvas over a dimmed/blurred background.
    # This preserves face/gameplay edges instead of blindly center-cropping them.
    vf=("split=2[bg][fg];"
        "[bg]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=18:8,eq=brightness=-0.28[bgv];"
        "[fg]scale=720:1280:force_original_aspect_ratio=decrease[fgv];"
        "[bgv][fgv]overlay=(W-w)/2:(H-h)/2,"
        f"subtitles='{subtitle_path}':force_style='FontSize=14,Outline=2,Shadow=1,Alignment=2,MarginV=115,WrapStyle=2',"
        f"drawtext=textfile='{hook_path}':fontcolor=white:fontsize=27:borderw=3:bordercolor=black:x=(w-text_w)/2:y=70:line_spacing=5:enable='between(t,0,2.5)',"
        "drawtext=text='CLIP RADAR':fontcolor=white:fontsize=20:borderw=2:bordercolor=black:x=w-text_w-20:y=20")
    command=[ffmpeg_binary(),"-hide_banner","-loglevel","error","-y"]
    if start:
        command += ["-ss",f"{start:.3f}"]
    command += ["-i",str(source),"-vf",vf,"-map","0:v:0","-map","0:a:0?","-c:v","libx264","-preset","medium","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k","-movflags","+faststart",str(output)]
    try:
        subprocess.run(command,check=True)
    finally:
        hook_file.unlink(missing_ok=True)
    return entries


if __name__ == "__main__":
    print("Clip Radar media processor installed: ffmpeg + free local Whisper transcription")
