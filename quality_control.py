from pathlib import Path
import json, subprocess


def probe(path: Path):
    r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration,size:stream=codec_type,width,height","-of","json",str(path)],capture_output=True,text=True,check=True)
    return json.loads(r.stdout)


def validate_final(path: Path):
    if not path.exists(): return False,"missing_output"
    p=probe(path); fmt=p.get("format",{}); streams=p.get("streams",[])
    duration=float(fmt.get("duration",0)); size=int(fmt.get("size",0))
    video=next((s for s in streams if s.get("codec_type")=="video"),None)
    audio=next((s for s in streams if s.get("codec_type")=="audio"),None)
    if not video: return False,"no_video"
    if not audio: return False,"no_audio"
    if duration < 8 or duration > 65: return False,"bad_duration"
    if video.get("width") != 720 or video.get("height") != 1280: return False,"not_vertical_720x1280"
    if size < 250000: return False,"suspiciously_small"
    return True,"ready_for_publish_queue"
