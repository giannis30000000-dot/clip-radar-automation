# Story prototype validation — 2026-09-21

Implementation commit: [25144a5](https://github.com/giannis30000000-dot/clip-radar-automation/commit/25144a5117bba613e2dd492184b6dea4117f680a).
Existing repository and main branch retained. Default mode is ORIGINAL_STORY_MODE.

## Executed evidence

| Validation | Result |
| --- | --- |
| Existing critical regression suite | All 27 existing tests retained and passing |
| Full suite including story tests | 44/44 passed on both GitHub-hosted Linux runs |
| Local Windows narration and complete render | Passed; 68.0 seconds, 720x1280, H.264/AAC |
| Explicit local regeneration | Passed; separate toaster-lawyer_v2 directory preserved the first render |
| Cloud run 35596374701 | Success; My Toaster Hired a Lawyer; QUALITY_CHECK_PASSED |
| Cloud run 35596513804 | Success; My Cat Bought the Moon; QUALITY_CHECK_PASSED |
| Persistent cloud dedupe | Second runner restored history and selected a different premise; both records persisted |
| Downloaded first cloud artifact | Full local decode and all 17 story QC checks passed again |
| Visual inspection | Actual rendered opening, middle and payoff frames inspected for both cloud videos |
| Public uploads/posts | None; publishing false, no service credentials used during generation |

Both cloud videos are 68.0 seconds at 720x1280, with 12 scenes, audible synthetic
narration, complete timed captions, subtle branding and playable H.264/AAC MP4.
The first MP4 is 5,434,765 bytes; the second is 5,861,808 bytes.
No cloud-only code failure occurred in these two runs.

- [First successful run and downloadable artifacts](https://github.com/giannis30000000-dot/clip-radar-automation/actions/runs/35596374701)
- [Second successful run and downloadable artifacts](https://github.com/giannis30000000-dot/clip-radar-automation/actions/runs/35596513804)
- [Persistent premise history](https://github.com/giannis30000000-dot/clip-radar-automation/blob/clip-radar-story-state/.clip-radar/story_history.json)

Artifact names:

- clip-radar-original-story-35596374701 (artifact ID 10636782395)
- clip-radar-original-story-35596513804 (artifact ID 10637007203)

Each contains final.mp4, story JSON, captions, render manifest, quality report,
development audio/scene assets, publication review state and inspection frames.
Separate history-audit artifacts also exist. GitHub review artifacts retain
30 days; the premise ledger persists in its dedicated Git branch.

## Changes that matter

The existing orchestrator, ffmpeg wrappers, ASS/SRT writers, inspection tooling,
QC module and PublicationLedger were extended. Twitch acquisition remains an
explicit legacy mode. Buffer/Metricool and scheduling logic were retained.
The old hourly Twitch trigger was removed; story generation is manual-only.
All workflow publication flags are false.

Buffer dry-run no longer creates Cloudinary assets. At capacity, a future live
upload attempts bounded cleanup of expired assets first. Cleanup excludes queued
or publishing records and media outside the configured folder. Empty cleanup
does not require credentials. No existing Cloudinary assets were deleted during
this prototype task.

## Acceptance boundaries

Cloud generation is independent of the owner's PC and works without any paid AI
service. These are development storyboard visuals and synthetic voices, requiring
owner review; machine QC is not a claim of final entertainment/visual acceptance.

The demo has three authored premises, not unlimited LLM generation. Installed
provider factories can replace script, visual and voice generation without
changing the orchestrator. Lexical concept dedupe is implemented; semantic
originality review and a real production provider remain future work.

No large dashboard, approval mutation, daily schedule or public posting is
enabled. A future publishing milestone needs owner-approved visual/voice
providers and a story-specific approval/eligibility adapter before using the
preserved Buffer/Metricool state and schedule infrastructure.

See [STORY_ENGINE.md](STORY_ENGINE.md) for commands, API and provider contracts.
