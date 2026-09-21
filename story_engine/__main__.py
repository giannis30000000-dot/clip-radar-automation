import argparse
import json
import os

from . import generate_story_video

parser = argparse.ArgumentParser(description="Generate an original Clip Radar review video")
parser.add_argument("--output-dir", default=None)
parser.add_argument("--state-file", default=None)
parser.add_argument("--template", default=None)
parser.add_argument("--regenerate", default=None, help="Existing story_id; creates a new review version")
parser.add_argument("--production-review", action="store_true", help="Strict H3 + automatic references + ElevenLabs; explicit environment budget required, no fallback")
args = parser.parse_args()
if args.production_review:
    os.environ.update(STORY_REQUIRE_PRODUCTION="true", STORY_REFERENCE_PROVIDER="runway", RUNWAY_MODEL="h3_max", STORY_PROVIDER="production", VOICE_PROVIDER="production", VISUAL_PROVIDER="production")
result = generate_story_video(args.output_dir, args.state_file, template=args.template, regenerate_story_id=args.regenerate)
print(json.dumps({k: result[k] for k in ("status", "publishing_enabled", "final", "metadata", "preflight") if k in result}, indent=2))
if args.production_review and result["status"] != "READY_FOR_REVIEW":
    raise SystemExit(2)
