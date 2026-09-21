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
    subtitle_time = max(0.0, min(duration - 0.05, caption_time))
    moments = {
        "opening": min(duration * 0.025, 0.45),
        "early_setup": min(max(duration * 0.18, 1.2), duration - 0.05),
        "middle": duration * 0.50,
        "subtitle_heavy": subtitle_time,
        "action_payoff": duration * 0.86,
        "ending": max(0.0, duration - min(0.35, duration * 0.04)),
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
        rows = [cv2.hconcat(images[index:index + 2]) for index in range(0, len(images), 2)]
        contact = cv2.vconcat(rows)
        contact_path = destination / "contact_sheet.png"
        cv2.imwrite(str(contact_path), contact)
        results["contact_sheet"] = str(contact_path)
    return results


def inspect_story_file(video: Path, destination: Path) -> dict[str, str]:
    """Portable review frames without requiring the legacy OpenCV detector."""
    from io import BytesIO
    import subprocess
    from PIL import Image, ImageDraw
    from media_tools import ffmpeg_binary

    destination.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(video.with_suffix(".render.json").read_text(encoding="utf-8"))
    scenes = manifest["scenes"]
    sheet = Image.new("RGB", (1080, 1320), "#182437")
    results = {}
    for index, scene in enumerate([scenes[i] for i in (0, 2, 4, 6, 9, len(scenes)-1)]):
        moment = scene["start_time"] + min(.8, scene["duration"] / 2)
        raw = subprocess.run([ffmpeg_binary(), "-v", "error", "-ss", str(moment), "-i", str(video), "-frames:v", "1", "-vf", "scale=360:640", "-f", "image2pipe", "-vcodec", "png", "-"], check=True, capture_output=True, timeout=30)
        image = Image.open(BytesIO(raw.stdout)).convert("RGB")
        target = destination / f"scene_{scene['scene_number']:02}.png"
        image.save(target)
        results[f"scene_{scene['scene_number']:02}"] = str(target)
        x, y = index % 3 * 360, index // 3 * 660
        sheet.paste(image, (x, y))
        ImageDraw.Draw(sheet).text((x+12, y+642), f"Scene {scene['scene_number']:02} / {moment:.2f}s", fill="white")
    target = destination / "contact_sheet.jpg"
    sheet.save(target, quality=92)
    results["contact_sheet"] = str(target)
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
