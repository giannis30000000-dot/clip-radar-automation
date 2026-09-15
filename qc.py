from pathlib import Path
import json, subprocess

def probe(path: Path):
    p=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration:stream=codec_type,width,height","-of","json",str(path)],capture_output=True,text=True,check=True)
    return json.loads(p.stdout)

def validate(path: Path):
    if not path.exists() or path.stat().st_size < 250_000: return False,"missing_or_tiny"
    data=probe(path); dur=float(data.get("format",{}).get("duration",0)); streams=data.get("streams",[])
    vids=[s for s in streams if s.get("codec_type")=="video"]
    aud=[s for s in streams if s.get("codec_type")=="audio"]
    if not vids or not aud: return False,"missing_audio_or_video"
    v=vids[0]
    if (v.get("width"),v.get("height")) != (720,1280): return False,"not_9x16"
    if not 8 <= dur <= 61: return False,"duration_bad"
    return True,"ready"
