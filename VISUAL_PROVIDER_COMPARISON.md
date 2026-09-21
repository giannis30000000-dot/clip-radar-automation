# One-scene visual provider comparison

Run `python compare_visual_providers.py` from this repository. This standalone
harness never generates a story, narration, edited reel or publication. Existing
production provider defaults and fallbacks remain available to the story engine.
No workflow, scheduler or dashboard was added.

## Models and cost

One identical 10-second scene is sent to each selected model, using the same
prompt and first-frame image. Ten seconds is the common comparison duration;
Gen-4 Turbo requires image input. The fixed original comedy scene is in SCENE
and STORY in compare_visual_providers.py. A matching reference should show the
teal refrigerator detective, table and yellow envelope described there; change
these two constants together if comparing a different scene.

| Model | API provider | Output setting | USD/second | One 10s attempt |
| --- | --- | --- | --- | --- |
| gen4.5 | Runway | 720x1280 | 0.12 | $1.20 |
| gen4_turbo | Runway | 720x1280 | 0.05 | $0.50 |
| h3_max (MiniMax H3 Max) | Runway | 768p, image aspect ratio | 0.08 | $0.80 |

Total: **$2.50** for all three without retries, excluding taxes/account fees.
H3 Max is a cheaper alternative to Gen-4.5, not cheaper than Turbo. It reuses the
existing Runway transport, credentials, polling, MP4 validation and budget
controller. Its different native resolution is recorded, not hidden or upscaled.
Prompt expansion is disabled on H3 Max to preserve the submitted prompt.

Rates checked 2026-09-21 against [official API pricing](https://docs.dev.runwayml.com/guides/pricing/).
Input contracts: [model catalog](https://docs.dev.runwayml.com/guides/models/) and
[official image-to-video schema](https://github.com/runwayml/sdk-python/blob/main/src/runwayml/types/image_to_video_create_params.py).
Access to a model still depends on the provider account. No paid quality claim is
made from mocks. Confirm account rates before spending; estimates are not invoices.

## Configuration and command

Only **RUNWAYML_API_SECRET** is needed for all three models, including H3 Max
through Runway. No separate MiniMax, OpenAI, ElevenLabs, Buffer or Cloudinary key
is used. Keep secrets in the environment, never in files or command arguments.

| Environment variable | Behavior |
| --- | --- |
| RUNWAYML_API_SECRET | Existing Runway developer API credential |
| CLIP_RADAR_PROVIDER_TEST_IMAGE_URL | Same owned/permitted public HTTPS first-frame image for all models; use a stable URL without query credentials; vertical image, at least 256px per side |
| CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD | Mandatory explicit total comparison ceiling; unset/empty/0 means no paid calls |
| CLIP_RADAR_PROVIDER_TEST_MAX_ATTEMPTS | Default 1; optionally 2 or 3; retries share the same total ceiling |
| RUNWAY_MAX_POLLS | Existing polling limit; default 60 |
| FFPROBE_BINARY | Optional explicit ffprobe path if not on PATH |

After securely configuring the key and shared image URL, PowerShell:

```powershell
$env:CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD='2.50'
python compare_visual_providers.py
```

Optional subset: `python compare_visual_providers.py --models gen4.5 gen4_turbo`
(first-attempt total $1.70). The full-story budget and global model/price/retry
settings do not override comparison settings. No paid requests are made without
the dedicated test ceiling, key, image and local ffprobe. No comparison was paid
for during implementation because the key and test budget were absent.

Before starting, the entire first-attempt comparison must fit the ceiling. Every
generation/retry also reserves cost before its POST. Failed or uncertain requests
retain their reservations; polling an accepted task never resubmits it. A retry
can consume the remaining budget and leave later models BUDGET_EXCEEDED. Defaults
allow no retries. No cheaper placeholder silently replaces a failed model.

## Outputs and interpretation

Each invocation gets a unique output/provider-comparison/<run>/ folder; previous
outputs are never overwritten. comparison.json contains the canonical prompt/spec
and hash, per-model outcome, provider, model, measured duration, resolution,
generation request count, retries, reserved estimate, actual cost (null when not
reported by the API), wall time including polls/download/validation, and output
path. Each model has its own scene_01.mp4, scene_01.provider.json and result.json.
cost_report.json contains the shared persisted request ledger. Skipped models
remain in comparison.json with zero requests; failed models have no usable output.

Generation request count excludes free task polls and downloads. An accepted task
whose completion is unknown may still finish at the provider; its ID and cost
reservation remain in the ledger. Actual cost is never invented from an estimate.
Review downloaded clips manually for adherence, motion, continuity and artifacts;
the harness does not pretend to assign an objective creative-quality winner.

Changed files: compare_visual_providers.py, story_engine/runway_provider.py,
tests/test_visual_comparison.py, VISUAL_PROVIDER_COMPARISON.md.

Validation: full suite 76/76 passed (66 existing plus 10 comparison tests), with
FFmpeg enabled and no skips. Mocked HTTP verifies identical prompt/image/duration,
model-specific payloads, isolated outputs, redaction, retries and shared budget
stops. The actual command returned PAID_TEST_DISABLED with zero paid requests and
$0 cost because credentials/test budget were absent. No full story or cloud
generation workflow was dispatched for this task.
