# Original story engine

Production adapters and budget controls are now available as optional upgrades;
see [PRODUCTION_PROVIDERS.md](PRODUCTION_PROVIDERS.md). Development remains the
default. Historical prototype details below describe the no-paid-API path.

This is a working review prototype, using three original authored comedy
templates, deterministic illustrated placeholder scenes and offline development
speech. It does not claim to be a production AI video generator or to have
owner-approved visuals/voice. No expensive provider or Cloudinary is required.

## Generate and review

Install Python 3.12+, requirements-story.txt, ffmpeg and ffprobe.
On Linux install espeak-ng and fonts-dejavu-core. Windows can use an installed
English System.Speech voice; its helper runs with a process-local execution policy
and does not change the machine's policy. If needed, set FFMPEG_BINARY,
FFPROBE_BINARY, ESPEAK_BINARY, or STORY_FONT to explicit executable/font paths.

Run python orchestrator.py (defaults to ORIGINAL_STORY_MODE), or:

    python -m story_engine --template toaster-lawyer
    python -m story_engine --regenerate toaster-lawyer
    python -m unittest discover -v

Regeneration uses the existing saved script, creates a versioned output folder,
and preserves the previous render. It is not treated as a new premise. Use the
same output/state paths when regenerating a custom local run.

The default output is output/stories/<story_id>/:

- final.mp4: H.264/AAC 720x1280, about 68 seconds, captions burned in.
- story.json: concept, full script, character/universe IDs, scene descriptions,
  prompts, narration-derived timing, platform captions and generation provenance.
- final.ass, final.srt, final.render.json: subtitle and render evidence.
- audio/narration.wav, scenes/*.png: replaceable development source assets.
- quality.json, generation.json, publication_state.json: result and review state.
- review_frames/contact_sheet.jpg: actual opening, middle and payoff frames.

output/stories/run_summary.json is a lightweight status document for a future
dashboard. A local path in a run report refers to that runner; inside downloaded
artifacts use final.mp4 and the relative manifest asset paths. The public API is:

    from orchestrator import generate_story_video
    result = generate_story_video(on_status=lambda event: print(event["status"]))

Callbacks receive stage, title, script/scenes when ready, metadata path, preview,
QC and final path. review.approved and review.publish_available are false.
Approve/publish actions are intentionally not implemented for this milestone.
Even an inherited PUBLISHING_ENABLED=true does not enable posting in this API.

## Providers and timing

StoryProvider.generate(excluded_concepts=..., template=...) returns validated
story JSON. VisualProvider.create(story, scene, directory) returns a
VisualAsset(path, kind, provider), where kind is image or video.
The assembler handles narration timing and vertical formatting for either kind.
A future model adapter can replace generation without replacing the orchestrator.

STORY_SCRIPT_PROVIDER, STORY_VISUAL_PROVIDER, and STORY_VOICE_PROVIDER
default to demo, or accept an installed module:factory implementation.
The production adapters are opt-in; no hidden paid API fallback exists. Voice providers
implement synthesize(story, directory) returning (wav_path, captions, metadata) and
update the story's measured scene/voice timing. See voice.py for the contract.
Provider credentials must stay inside the adapter, never in metadata.

The development voice synthesizes short readable phrases, trims synthesis
silence and measures PCM durations. These intervals drive captions and scenes.
A bounded tempo adjustment aims for 68 seconds; no silence is added to stretch
the story to a target. The voice is intentionally synthetic development audio.
Hard cuts land on narration beats and placeholder images get subtle motion.

Template catalogue:

- My Toaster Hired a Lawyer: the kitchen union negotiates a bread-free vacation.
- My Cat Bought the Moon: moonlight rent meets an overdue invoice from the sun.
- My Fridge Joined Witness Protection: a cheese theft becomes an appliance conspiracy.

All belong to the replaceable almost-normal universe. Explicit character IDs
support future recurring casts; these standalone endings do not force sequels.

## State, QC and cloud

state/story_history.json reserves concept fingerprints before synthesis and
records generation date, characters, output path, QC and future publication state.
It also rejects near-identical premise wording. This is lexical dedupe, not a
claim of semantic originality detection; a future LLM catalogue needs semantic
premise review. Corrupt state is rejected, never silently reset.

The finite demo catalogue returns NO_NEW_PREMISE with exit 0 when exhausted.
BUSY is a normal result for a locked local generation. Review-required QC also
remains a normal, explicitly labelled result. Real setup/render/state errors
remain errors, so a green workflow is not a substitute for checking QC status.

GitHub Actions uses ffmpeg, eSpeak NG and Python on a hosted Linux runner.
The default development run needs no PC, Twitch, Cloudinary, Buffer, Metricool or paid AI service.
One concurrency group serializes story runs. History is restored/saved through
the repository token on a dedicated clip-radar-story-state branch, rather than
depending on an evictable Actions cache. State writes use the previous file SHA
to reject stale updates. Run artifacts expire after 30 days; history in Git does
not. The branch and its known history path are the only cloud-state write target.

QC validates story schema, 60-75-second duration, 9:16 MP4, complete scene timeline,
audible audio, caption timing/text/placement and full ffmpeg decode. Actual frames
from every scene are checked for non-empty visual content and variation.
Machine QC does not certify that the story is funny or the voice sounds natural.
Review the preview before approving any future production milestone.

The future daily job can call this same entry point, then route approved results
through the existing schedule_slots.py and per-network PublicationLedger.
Existing Athens slots and Buffer/Metricool adapters remain available, but need a
story-specific publishing eligibility adapter and approval lifecycle before use.
There is no daily/4-post schedule and no social upload enabled in this prototype.

## Cloudinary maintenance

Legacy Buffer dry-runs no longer instantiate a Cloudinary host or upload assets.
Story generation never uses it. Existing maintenance is bounded by expiry and
folder, protects top-level/per-network QUEUED or PUBLISHING records, and needs no
credentials for an empty cleanup. A live upload at capacity first attempts this
safe expired cleanup, then retains the existing limit if capacity is still full.
This does not bulk-delete old media or infer that a pending Buffer post finished.

## References

Placeholder drawing uses [Pillow ImageDraw](https://pillow.readthedocs.io/en/stable/reference/ImageDraw.html).
Cloud speech uses the [eSpeak NG CLI](https://github.com/espeak-ng/espeak-ng/blob/master/docs/espeak-ng.1.ronn).
