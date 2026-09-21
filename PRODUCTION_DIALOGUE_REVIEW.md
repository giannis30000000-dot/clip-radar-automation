# First H3 Max dialogue review

This extends the existing story/voice/render path. Publishing, scheduling, dashboard,
legacy acquisition, provider comparison, and the default development fallbacks are unchanged.
`--production-review` is an explicit fail-closed mode, not a production schedule.

## Run one video

Required process environment (never put secret values in Git or reports):

- `STORY_LLM_API_KEY`: existing production story API key.
- `RUNWAYML_API_SECRET`: Runway API key, used for images and H3 Max.
- `ELEVENLABS_API_KEY`: production speech key.
- `STORY_VOICE_POOL`: JSON array of 2-4 distinct permitted ElevenLabs voice IDs.
- `CLIP_RADAR_MAX_COST_USD_PER_VIDEO`: explicit positive budget, at most `10`.

Windows environment changes do not update an already-running terminal/Codex process.
Open a fresh terminal, or import only these named user variables into the process.
Do not print them. No `.env` file is automatically loaded.

```powershell
$env:CLIP_RADAR_MAX_COST_USD_PER_VIDEO = '10'
py -3 -m story_engine --production-review --output-dir output/production-review
```

This selects the existing production writer and ElevenLabs dialogue provider, H3 Max,
Runway reference images, and strict production checks. Missing credentials, invalid
voice pool or cap stop before the first paid request and return exit code 2.
`STORY_VOICE_MAP` must be unset for this pool-based review. Default duration remains
65-75 seconds. The existing shared concept history is used; it is not reset.

Do not rerun an interrupted paid job blindly. Inspect the saved cost report and
provider task IDs first: an unknown/timeout result might still be billable. This
milestone deliberately does not add a recovery framework or refund assumptions.

## Pipeline and evidence

1. Existing candidate ranking, persistent dedupe, script validation and independent
   editorial critique select a new story. The bounded writer also creates compact
   visual identities and limits speakers to the configured pool size.
2. Each speaker receives a different configured voice ID, in first-speaking order.
   ElevenLabs line audio/alignment and measured 65-75s duration must pass the existing
   gate. A failed production voice cannot silently switch to development audio.
3. A complete visual bible is saved once. All character identity fields, style and
   environment are included unchanged in every image/H3 prompt. Missing/oversize
   identities enter the existing script-rewrite loop; they are never silently cut.
4. Before images, plan the remaining spend: one cast image plus one frame per scene,
   then H3 motion prioritized for the opening/final payoff and other reaction beats.
   If the plan is tight, lower-priority shots use their production image with the
   existing gentle camera animation. No dialogue, timing or story beats are removed.
   If even references plus key motion cannot fit, stop before spending on images.
5. Runway `gen4_image` produces a 720x1280 cast reference, then scene frames using
   that same `@cast` image. Scene setup, camera, art and continuity are prompted.
   No text/logos/watermarks/letters/signage/UI/subtitles are requested. The cast
   reference has no written labels. Prompt limits preserve the whole bible first.
6. Validated local frames become base64 data URIs for H3 `promptImage`; no manual
   upload or hosting is required. Metadata stores hashes/filenames, not base64 or
   signed URLs. H3 uses 768p, 2-10s scene clips, prompt expansion disabled.
7. Image/video submissions are single-attempt in strict mode. Task polling can retry
   reads of the same ID, never an uncertain paid submission. Every paid submission,
   including existing writer/voice retries, reserves the shared budget first.
8. The existing assembler discards **all** scene audio and maps only the master
   ElevenLabs track. It adds one speaker-aware caption layer and Clip Radar branding,
   then runs full decode/media QC and produces a contact sheet.

Output folder: `output/production-review/<story_id>/`

- `final.mp4`, `final.ass`, `final.srt`, `final.render.json`
- `story.json`, `visual_bible.json`, `visual_plan.json`, `story_quality*.json`
- `references/cast.*`, `references/scene_*.{png,jpg,webp}`, provider metadata
- `scenes/scene_*.mp4`, provider task/validation metadata
- `audio/attempt_*/line_*/`, master WAV, `dialogue_timing.json` with voice assignments
- `cost_report.json`, `quality.json`, `generation.json`, `review_frames/`

## Cost assumptions and limitations

Runway's current published rates: Gen-4 Image 720p **$0.05/image** and H3 Max 768p
**$0.08/generated second**. A 10-scene plan with 11 images and 75 generated motion
seconds is **$6.55 for visuals**, plus writer/voice reservations and any earlier
failed calls. The planner uses actual measured scene lengths rounded up, not this
example. The hard cap uses conservative reservations; the provider invoice is not
available. Existing explicit LLM/TTS rate settings still apply. Recheck rates before
future paid use, especially a different model/account tier.

References: [Runway pricing](https://docs.dev.runwayml.com/guides/pricing/),
[image request schema](https://raw.githubusercontent.com/runwayml/sdk-python/main/src/runwayml/types/text_to_image_create_params.py),
[H3 first-frame schema](https://raw.githubusercontent.com/runwayml/sdk-python/main/src/runwayml/types/image_to_video_create_params.py).
Checked 2026-09-21.

The OpenAI Docs guidance informed validation of the added visual identity fields:
[JSON mode does not enforce schema](https://developers.openai.com/api/docs/guides/structured-outputs).
The existing API/model and bounded validation/rewrite architecture are preserved.

Lip sync is **not implemented**. Prompts use wide/medium acting and reaction shots
that tolerate dubbing. Emotional hints remain in per-line metadata; the existing
TTS endpoint does not guarantee each direction. A shared reference/bible improves
consistency, but does not prove it. Automated QC does not certify humor, perfect
character continuity, absence of generated lettering, licensing, or owner acceptance.
Inspect actual images, motion, sound and captions before any upload.

## Validation / current external blocker (2026-09-21)

- Existing 98 tests preserved; reference/budget/strict-mode tests added, including
  a real FFmpeg two-tone test proving generated scene audio cannot enter the master.
- Local checks found `RUNWAYML_API_SECRET` in Windows **user** environment only.
- `STORY_LLM_API_KEY` (also checked `OPENAI_API_KEY`), `ELEVENLABS_API_KEY`, and
  `STORY_VOICE_POOL` were absent from process/user/machine environments. No repository
  environment/configuration file containing these was found by filename checks.
- The production command is therefore blocked before paid generation. No new
  production story, images, H3 clips, voices or final video are claimed.
- Secrets are neither printed nor copied into this report. Publishing stays disabled.
