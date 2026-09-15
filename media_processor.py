"""Free/open-source Clip Radar media processing stage.

Runs only after the source layer provides an authorized local media file.
Uses ffmpeg + faster-whisper; no paid transcription API is required.
"""
from pathlib import Path
import subprocess


def extract_audio(video: Path, wav: Path):
    subprocess.run(["ffmpeg","-y","-i",str(video),"-vn","-ac","1","-ar","16000",str(wav)], check=True)


def transcribe(video: Path, model_size: str = "tiny"):
    from faster_whisper import WhisperModel
    wav=video.with_suffix(".wav")
    extract_audio(video,wav)
    model=WhisperModel(model_size, device="cpu", compute_type="int8")
    segments,info=model.transcribe(str(wav), language="en", vad_filter=True, word_timestamps=True)
    result=[]
    for seg in segments:
        result.append({"start":seg.start,"end":seg.end,"text":seg.text.strip()})
    wav.unlink(missing_ok=True)
    return result


def srt_timestamp(seconds: float):
    ms=int(round(seconds*1000)); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms//1000; ms%=1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(entries, path: Path):
    with path.open("w",encoding="utf-8") as f:
        for i,e in enumerate(entries,1):
            f.write(f"{i}\n{srt_timestamp(e['start'])} --> {srt_timestamp(e['end'])}\n{e['text']}\n\n")


def render_vertical(source: Path, output: Path, hook: str = "CLIP RADAR"):
    subs=source.with_suffix(".srt")
    entries=transcribe(source)
    write_srt(entries,subs)
    # Generic center crop. A later visual-QC stage will choose subject-aware framing.
    vf=("scale=-2:1280,crop=720:1280:(iw-720)/2:0,"+
        f"subtitles='{subs.as_posix()}':force_style='FontSize=18,Outline=2,Alignment=2,MarginV=110',"+
        f"drawtext=text='{hook}':fontcolor=white:fontsize=38:borderw=3:bordercolor=black:x=(w-text_w)/2:y=75:enable='between(t,0,2.5)',"+
        "drawtext=text='CLIP RADAR':fontcolor=white:fontsize=20:borderw=2:bordercolor=black:x=w-text_w-20:y=20")
    subprocess.run(["ffmpeg","-y","-i",str(source),"-vf",vf,"-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","160k",str(output)],check=True)
    return entries


if __name__ == "__main__":
    print("Clip Radar media processor installed: ffmpeg + free local Whisper transcription")
