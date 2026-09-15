"""Extract representative frames and contact sheets for human QA in Actions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from framing import extract_frame
from media_tools import media_summary


def _cv():
    import cv2
    return cv2


def inspect_file(video: Path, destination: Path) -> dict[str, str]:
    cv2 = _cv()
    destination.mkdir(parents=True, exist_ok=True)
    duration = float(media_summary(video).get("duration") or 0.0)
    manifest = video.with_suffix(".render.json")
    captions = []
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        captions = list((data.get("subtitles") or {}).get("entries") or [])
    caption_time = float(captions[len(captions) // 2].get("start", duration / 2)) if captions else duration / 2
    moments = {
        "beginning": min(duration * 0.05, 1.0),
        "middle": duration * 0.50,
        "payoff": duration * 0.86,
        "subtitle": max(0.0, min(duration - 0.05, caption_time)),
    }
    images = []
    results: dict[str, str] = {}
    for label, seconds in moments.items():
        frame = extract_frame(video, seconds, width=360)
        path = destination / f"{label}.png"
        cv2.putText(frame, f"{label} {seconds:.1f}s", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.imwrite(str(path), frame)
        results[label] = str(path)
        images.append(frame)
    if images:
        sheet = cv2.hconcat(images[:2])
        bottom = cv2.hconcat(images[2:])
        contact = cv2.vconcat([sheet, bottom])
        contact_path = destination / "contact_sheet.png"
        cv2.imwrite(str(contact_path), contact)
        results["contact_sheet"] = str(contact_path)
    return results


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python inspection_frames.py <final-directory> <inspection-directory>")
        return 2
    source, destination = Path(sys.argv[1]), Path(sys.argv[2])
    videos = sorted(source.glob("*_clipradar_vertical.mp4"))
    if not videos:
        print("inspection | no final videos found")
        return 1
    for video in videos:
        result = inspect_file(video, destination / video.stem)
        print(f"inspection | {video.name} | frames={','.join(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
