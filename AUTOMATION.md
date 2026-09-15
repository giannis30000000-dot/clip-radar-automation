# Clip Radar automatic acquisition and gated publication

The production path is now:

`Twitch Helix discovery -> viral ranking -> sharing_whitelist.json -> multi-layer dedupe -> public Twitch clip acquisition -> source validation -> local Whisper transcription -> content-aware 9:16 edit -> strict QC -> rights/content safety -> PUBLISH_ELIGIBLE -> Cloudinary temporary delivery -> gated Buffer plan -> artifacts`

`orchestrator.py` writes the original landscape MP4 to `output/sources/`, the
edited vertical MP4 to `output/final/`, and structured run, delivery, and
publication reports. With `PUBLISHING_ENABLED=false` and
`PUBLISHING_DRY_RUN=true`, the cloud test uploads only the final vertical MP4
to Cloudinary, verifies its public HTTPS delivery, and creates a sanitized
Buffer plan without creating posts. The GitHub Action uploads the source,
final, run summary, Cloudinary delivery report, Buffer plan, and state audit as
separate artifacts for inspection.

## Acquisition and rights

The current provider is `TwitchPublicClipProvider` in `acquisition.py`. It uses
the maintained `yt-dlp` Twitch extractor against the public clip URL only. No
browser cookies, private endpoints, DRM workarounds, or access-control bypass
are used. The rights gate runs first, and only broadcasters with
`viewer_social_sharing: true` in `sharing_whitelist.json` are eligible. xQc is
the current verified entry; all other listed creators remain unverified.

The provider boundary accepts additional explicitly authorized providers later,
such as an official creator/editor download URL or a licensed content API,
without changing the ranking or QC stages.

## Dedupe and publishing safety

`state/processed_clips.json` is a versioned ledger. It checks immutable Twitch
clip ID first, then broadcaster/VOD/timestamp overlap, then sanitized
publication-history aliases, and finally decoded source/final audio and visual
fingerprints. It is keyed by media identity, not filenames. The Action restores
and saves the ledger using a unique per-run cache key with a branch prefix, so
later hourly runs skip clips already prepared or published. The seed file
`historical_publications.json` includes the four accidental TikTok posts and
the earlier Jean Paul delivery publication alias. `buffer-audit.yml` imports
later read-only Buffer history into the same ledger without network mutations.
The cache is durable under GitHub's cache retention policy; the ledger never
silently forgets an ID. `state/publications.json` is a separate publication
ledger with independent per-network state and analytics-ready fields. Its
state machine is:

`DISCOVERED -> ELIGIBLE -> ACQUIRED -> RENDERED -> QC_PASSED -> PUBLISH_ELIGIBLE -> QUEUED -> PUBLISHING -> PUBLISHED`

with `FAILED`, `SKIPPED`, and `REVIEW_REQUIRED` paths. Every candidate must
have an accepted documented rights basis. The conservative content check
routes obvious movie/TV/music/broadcast/rebroadcast signals to review, and
the transformation check requires the editorial hook, pacing policy, reframed
composition, transcript subtitles, branding, and attribution profile. Unknown
or uncertain rights never publish.

## Editorial quality hardening

`media_processor.py` transcribes once and emits one FFmpeg/libass ASS transcript
subtitle layer. The short factual hook is a separate title-card drawtext
element, disabled whenever a transcript caption is active; it is never added
to the subtitle file. Known Twitch burned-in caption bands are neutralized with
a blurred texture in the central lower source band before the Clip Radar layer
is added. Captions are
confidence-filtered, timed, wrapped to at most two mobile-readable lines, and
kept inside the configured safe margins. The edit window is derived from
speech setup/action/payoff rather than a fixed blind duration.

`framing.py` samples decoded frames for face presence, motion, edge activity,
and dark borders. Landscape sources use a full-frame fit over a blurred 9:16
background so facecam and gameplay remain visible; there is no blind center
crop. Each render writes a `.render.json` manifest. `quality_control.py`
checks the manifest, subtitle count/timing/density/confidence, canvas/audio,
visual variation, black-bar risk, framing strategy, and hook separation. A
failed or low-confidence check is `REVIEW_REQUIRED`, not publishable.

The manual-only `quality-validation.yml` workflow runs a fresh broad scan with
`PUBLISHING_ENABLED=false`, requires three distinct real outputs, extracts
beginning/middle/payoff/subtitle-heavy frames, and uploads sources, finals,
manifests, QC reports, and inspection frames. It does not configure Buffer or
Cloudinary credentials and cannot create a post.

## Cloudinary delivery, cleanup, and Buffer dry run

`cloudinary_media_host.py` is the temporary delivery adapter between GitHub
Actions and Buffer. It authenticates a video upload server-side with the
Cloudinary API secrets, uses deterministic IDs under `clipradar/buffer/`,
refuses oversized files or too many active bridge objects, verifies the
returned HTTPS URL with an MP4 byte-range probe, and reuses a verified upload
for the same final-file hash. Only the final vertical MP4 is hosted; the
source landscape file remains a GitHub artifact. `cloudinary-cleanup.yml` runs
daily and deletes only expired assets that have no queued or in-progress
network state, with bounded work per run. Successful or queued Buffer results
retain the object for the configured retention period; abandoned uploads use
the shorter cleanup window.

## Buffer dry run and production schedule

`buffer_publisher.py` is the active backend; `metricool_publisher.py` remains an
optional inactive adapter. Buffer discovery queries the authenticated account,
organizations, and channels, then selects the unique Clip Radar-named or
otherwise unique Instagram/TikTok channel. Ambiguous channels fail closed and
require an explicit channel ID; IDs are cached in `state/buffer_channels.json`
without the API key. The plan contains the discovered channel ID and name,
platform-specific metadata, and the GraphQL `createPost` input.

The target slots are `12:00, 15:30, 19:00, 22:00` in `Europe/Athens`. The
publisher reserves distinct slots and enforces `MAX_DAILY_PUBLICATIONS=4` by
local date. These are an upper bound, not quotas; hourly discovery still uses
the strongest currently available eligible candidate and never forces category
diversity. YouTube remains disabled until the account is recovered and its
Buffer channel is connected.

Buffer receives the verified Cloudinary HTTPS URL in both the Instagram Reel
and TikTok `createPost` request plans. YouTube remains disabled.

`controlled-buffer-publication.yml` is manual-only and requires the explicit
`confirm_live=true` dispatch input. It is the only workflow configured to set
`PUBLISHING_ENABLED=true`; it schedules one fresh, non-duplicate candidate
seven minutes ahead on Instagram and TikTok, with YouTube disabled. The
hourly `scan.yml` workflow remains permanently in Buffer dry-run mode.

## Required Action secrets

Configure `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `BUFFER_API_KEY`,
`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` as
GitHub Actions secrets. Twitch credentials are used only for Helix discovery;
the Buffer key is sent only as a Bearer token to `api.buffer.com`; Cloudinary
credentials are used only for the server-side upload and signed destroy
adapter. Secret values are never printed, written to artifacts, or committed.
