# Clip Radar automatic acquisition and gated publication

The production path is now:

`Twitch Helix discovery -> viral ranking -> sharing_whitelist.json -> public Twitch clip acquisition -> source validation -> local Whisper transcription -> 9:16 edit -> QC -> gated Metricool plan -> artifacts`

`orchestrator.py` writes the original landscape MP4 to
`output/sources/`, the edited vertical MP4 to `output/final/`, and a structured
`output/run_summary.json`. With `PUBLISHING_ENABLED=false` and
`PUBLISHING_DRY_RUN=true`, it also writes `output/publishing_plan.json`. The
GitHub Action uploads all four as separate artifacts for inspection and does
not contact Metricool.

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
silently forgets an ID. Publishing remains intentionally absent from the
workflow.

## Dedupe and publishing safety

`state/processed_clips.json` is keyed by immutable Twitch clip ID. The Action
restores and saves the ledger using a unique per-run cache key with a branch
prefix, so later hourly runs skip clips already prepared or published. The
cache is durable under GitHub's cache retention policy; the ledger never
silently forgets an ID. `state/publications.json` is a separate publication
ledger with independent per-network state and analytics-ready fields. Its
state machine is:

`DISCOVERED -> ELIGIBLE -> ACQUIRED -> RENDERED -> QC_PASSED -> QUEUED -> PUBLISHING -> PUBLISHED`

with `FAILED` and `SKIPPED` terminal paths. The publisher refuses unverified
broadcasters, missing immutable clip IDs, failed QC, duplicate active or
successful states, and YouTube unless `ENABLE_YOUTUBE_PUBLISHING=true`. It
uses bounded retries for transient scheduler failures and marks ambiguous
requests failed instead of replaying indefinitely.

## Metricool dry run and production schedule

`metricool_publisher.py` generates platform-specific metadata from the Twitch
title, broadcaster, game, source URL, and transcript context. The plan includes
TikTok and Instagram Reel payloads and still generates YouTube Shorts metadata
for review, while YouTube posting stays disabled. The next local-time slot is
selected from `12:00, 15:30, 19:00, 22:00` in `Europe/Athens`. The publisher
reserves a different next slot for each plan and enforces
`MAX_DAILY_PUBLICATIONS=4` by local date. Hourly discovery can therefore find
a stronger eligible clip without forcing weak content to fill a quota.

Live Metricool scheduling is deliberately not enabled in the checked-in
workflow. When authorized later, it requires all three repository secrets
`METRICOOL_USER_TOKEN`, `METRICOOL_USER_ID`, and `METRICOOL_BLOG_ID`, plus an
explicit `PUBLISHING_ENABLED=true` change. The token is sent only in the
`X-Mc-Auth` header and is never written to logs or artifacts. The first live
test remains limited to TikTok and Instagram; YouTube is off until the account
is recovered.

## Required Action secrets

Configure `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` as GitHub Actions
secrets. The existing scanner uses those credentials only for Twitch Helix
discovery. They are never printed or passed to the public media downloader.
