from pathlib import Path
from media_tools import MediaToolError, probe_media


def probe(path: Path):
    return probe_media(path)


def validate_final(path: Path):
    if not path.exists(): return False,"missing_output"
    try:
        p=probe(path)
    except MediaToolError as exc:
        return False,str(exc)
    fmt=p.get("format",{}); streams=p.get("streams",[])
    duration=float(fmt.get("duration",0)); size=int(fmt.get("size",0))
    video=next((s for s in streams if s.get("codec_type")=="video"),None)
    audio=next((s for s in streams if s.get("codec_type")=="audio"),None)
    if not video: return False,"no_video"
    if not audio: return False,"no_audio"
    if duration < 8 or duration > 65: return False,"bad_duration"
    if video.get("width") != 720 or video.get("height") != 1280: return False,"not_vertical_720x1280"
    if size < 250000: return False,"suspiciously_small"
    return True,"ready_for_publish_queue"
