# Clip Radar automatic acquisition milestone

The production path is now:

`Twitch Helix discovery -> viral ranking -> sharing_whitelist.json -> public Twitch clip acquisition -> source validation -> local Whisper transcription -> 9:16 edit -> QC -> artifacts`

`orchestrator.py` never publishes. It writes the original landscape MP4 to
`output/sources/`, the edited vertical MP4 to `output/final/`, and a structured
`output/run_summary.json`. The GitHub Action uploads those as separate
artifacts for inspection.

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

## Required Action secrets

Configure `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` as GitHub Actions
secrets. The existing scanner uses those credentials only for Twitch Helix
discovery. They are never printed or passed to the public media downloader.
