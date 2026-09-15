"""Clip Radar safe unattended orchestrator skeleton.

Production rule: never publish random/weak media and never bypass source permissions.
If no authorized high-quality source is available, the run exits without publishing.
"""
from pathlib import Path
from pipeline import Candidate, choose_next
from media_processor import render_vertical
from quality_control import validate_final
from publish_plan import build_metadata


def process_candidates(candidates):
    chosen=choose_next(candidates)
    if not chosen:
        return {"status":"SKIP","reason":"no_high_quality_authorized_source"}
    source=Path(chosen.authorized_source_url)
    out=Path("output")/f"{chosen.clip_id}_final.mp4"
    out.parent.mkdir(exist_ok=True)
    render_vertical(source,out,hook=chosen.title[:55])
    ok,reason=validate_final(out)
    if not ok:
        out.unlink(missing_ok=True)
        return {"status":"SKIP","reason":reason}
    return {"status":"READY","file":str(out),"metadata":build_metadata(chosen.streamer,chosen.title)}


if __name__ == "__main__":
    print("Clip Radar orchestrator installed. Safe default: SKIP unless source + quality gates pass.")
