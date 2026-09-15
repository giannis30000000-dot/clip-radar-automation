# Clip Radar automatic acquisition and gated publication

The production path is now:

`Twitch Helix discovery -> viral ranking -> sharing_whitelist.json -> public Twitch clip acquisition -> source validation -> local Whisper transcription -> 9:16 edit -> QC -> rights/content safety -> gated Buffer plan -> artifacts`

`orchestrator.py` writes the original landscape MP4 to
`output/sources/`, the edited vertical MP4 to `output/final/`, and a structured
`output/run_summary.json`. With `PUBLISHING_ENABLED=false` and
`PUBLISHING_DRY_RUN=true`, it also writes `output/publishing_plan.json`. The
GitHub Action uploads the source, final, run summary, Buffer plan, and state
audit as separate artifacts for inspection. Dry-run performs read-only Buffer
account/channel discovery and does not create posts.

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

`state/processed_clips.json` is keyed by immutable Twitch clip ID. The Action
restores and saves the ledger using a unique per-run cache key with a branch
prefix, so later hourly runs skip clips already prepared or published. The
cache is durable under GitHub's cache retention policy; the ledger never
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

Buffer's API does not upload local files. Live mode therefore requires
`BUFFER_MEDIA_URL`, a stable public HTTPS MP4 URL that remains reachable until
the scheduled post publishes. A GitHub Actions artifact URL is not suitable.

## Required Action secrets

Configure `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, and `BUFFER_API_KEY` as
GitHub Actions secrets. The Twitch credentials are used only for Helix
discovery; the Buffer key is sent only as a Bearer token to `api.buffer.com`.
Secret values are never printed, written to artifacts, or committed.
