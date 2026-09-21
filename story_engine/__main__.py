import argparse
import json

from . import generate_story_video

parser = argparse.ArgumentParser(description="Generate an original Clip Radar review video")
parser.add_argument("--output-dir", default=None)
parser.add_argument("--state-file", default=None)
parser.add_argument("--template", default=None)
parser.add_argument("--regenerate", default=None, help="Existing story_id; creates a new review version")
args = parser.parse_args()
result = generate_story_video(args.output_dir, args.state_file, template=args.template, regenerate_story_id=args.regenerate)
print(json.dumps({k: result[k] for k in ("status", "publishing_enabled", "final", "metadata") if k in result}, indent=2))
