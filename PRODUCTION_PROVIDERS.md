# Production provider upgrade

Production APIs are implemented behind the existing contracts. Development is
still the default. No scheduler, dashboard or publishing was enabled. Actual
production quality must be judged from a paid sample; mocks cannot establish it.

## Choices and current API references

- **Story:** vendor-isolated OpenAI-compatible JSON Chat Completions. Default
  gpt-4.1-mini is inexpensive and supports JSON output. Endpoint/model are
  configurable; other vendors must support this request contract. The adapter
  validates the existing story schema, retries invalid/duplicate output, passes
  prior premise text into the prompt and uses the persistent dedupe guard.
  Official OpenAI documentation was used to verify the JSON API and pricing.
  [API contract](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
  [default model/rates](https://developers.openai.com/api/docs/models/gpt-4.1-mini).
- **Visual:** Runway Gen-4.5. Its API generates 2-10-second vertical clips from
  text, with task status polling and per-second pricing. Each scene includes
  character descriptions, style, environment, action and prior-scene context.
  An optional scene reference_image_url uses image-to-video. This improves the
  available continuity inputs but does not guarantee identical faces across clips.
  [Getting started](https://docs.dev.runwayml.com/guides/using-the-api/),
  [official SDK request schema](https://github.com/runwayml/sdk-python/blob/main/src/runwayml/types/text_to_video_create_params.py).
- **Voice:** ElevenLabs multilingual v2 with the timestamps endpoint: a whole
  natural narration request with character alignment, avoiding separately spoken
  phrases. Voice ID/model are configurable. Misaligned or out-of-range narration
  falls back explicitly; the system does not pretend approximate captions are
  provider timestamps.
  [Timestamp API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps).

## Configuration

No keys are stored in the repository. Add these as GitHub Actions secrets only
when ready for a paid test:

| Secret | Purpose |
| --- | --- |
| STORY_LLM_API_KEY | Key for the selected Chat Completions endpoint; OpenAI by default |
| RUNWAYML_API_SECRET | Runway developer API key |
| ELEVENLABS_API_KEY | ElevenLabs API key |

Environment variables (GitHub repository variables where mapped in the workflow):

| Variable | Default / behavior |
| --- | --- |
| STORY_PROVIDER | development; production opts into the story adapter |
| VISUAL_PROVIDER | development; production opts into Runway |
| VOICE_PROVIDER | development; production opts into ElevenLabs |
| CLIP_RADAR_MAX_COST_USD_PER_VIDEO | 0; an explicit positive ceiling is required to spend |
| ELEVENLABS_VOICE_ID | Required to use paid voice; choose a voice accessible to your account |
| STORY_LLM_BASE_URL | https://api.openai.com/v1 |
| STORY_LLM_MODEL | gpt-4.1-mini |
| STORY_LLM_MAX_OUTPUT_TOKENS | 6000; clamped to 2000-12000 |
| STORY_LLM_INPUT_USD_PER_MILLION | 0.40 for the known default model/endpoint |
| STORY_LLM_OUTPUT_USD_PER_MILLION | 1.60 for the known default model/endpoint |
| RUNWAY_MODEL | gen4.5; other models require an explicit rate and a compatible API schema |
| RUNWAY_RATIO | 720:1280; 1280:720 also accepted and cropped by the existing vertical assembler |
| RUNWAY_USD_PER_SECOND | 0.12 for gen4.5 standard MP4 |
| RUNWAY_MAX_POLLS | 60, every 5 seconds; clamped to 1-120 |
| ELEVENLABS_MODEL | eleven_multilingual_v2 |
| ELEVENLABS_USD_PER_1K_CHARACTERS | 0.10 for the default model |
| STORY_PROVIDER_MAX_ATTEMPTS | 2 total attempts per operation; clamped to 1-3 |
| PUBLISHING_ENABLED | false; story generation cannot post even if inherited as true |

The existing STORY_SCRIPT_PROVIDER, STORY_VISUAL_PROVIDER and
STORY_VOICE_PROVIDER demo/module:factory settings still work. The new short
selectors take precedence when nonempty. Custom factories retain responsibility
for their own implementation; the shared budget is enforced by the built-in
production adapters, not arbitrary third-party Python plugins.

The manual workflow exposes provider_mode (development by default) and
max_cost_usd (0 by default). Model/rate/voice variables are read from repository
variables. Missing production keys or a missing voice ID selects development
without an API request. Keys alone do not activate paid generation.

## Cost, retries and fallback

The estimate for one video is:

    sum(ceil(scene seconds) * Runway dollars/second)
      + input tokens * LLM input rate / 1,000,000
      + output tokens * LLM output rate / 1,000,000
      + narration characters * ElevenLabs rate / 1,000
      + each retry's full reservation

Runway durations are bounded to 2-10 seconds per call. Longer narration scenes
are handled by the existing assembler's hold-last-frame behavior and need
editorial inspection. Quality here is configured by the chosen model/resolution;
expensive HDR/ProRes formats are deliberately not requested.

At the rates checked on 2026-09-21, the existing toaster script requests 75
rounded scene seconds: $9.00 in video, about $0.113 for 1,130 TTS characters,
plus story tokens. This is approximately $9.13 before retries/taxes, depending on
LLM token usage. A different 60-75-second script may cost more or less because
individual scene duration rounding is billable.

[Runway API pricing](https://docs.dev.runwayml.com/guides/pricing/) is 12 credits
per second for Gen-4.5, at $0.01/credit. [ElevenLabs API pricing](https://elevenlabs.io/pricing/api)
lists multilingual TTS at $0.10/1K characters. Confirm your account rates before
paying: account plans, voice fees, taxes and future model pricing can differ.
All three production services require API access and usage billing/credits.
An existing consumer/chat subscription does not supply these API credentials.

CostBudget reserves a conservative maximum before every generation request,
including malformed-output retries and failed attempts. LLM reservation uses
UTF-8 request bytes plus overhead as a conservative input token bound and the
configured output-token maximum. Returned token usage gets a separate estimate.
Failed/uncertain reservations are not refunded, so the controller cannot
accidentally spend a presumed refund. Rates must be supplied for unknown models.

If the next request does not fit, the whole generation returns BUDGET_EXCEEDED
with no further paid calls. This is a normal workflow result, not an exception
email. The ceiling applies to one invocation/render version, not all future
regenerations or the provider account. Configure provider-side account limits too
if an invoice-level cap is needed; local estimates cannot guarantee vendor bills
when configured rates are incorrect or an account has additional fees.

Every invocation has an attempts/<id>/cost_report.json, copied/updated beside
the story as cost_report.json once a story folder exists. It includes provider,
model, request/retry count, requested/generated seconds, generated assets,
reservations, available token usage, fallback reasons and total estimated cost.
actual_cost_usd is null for paid calls without an invoice; it is zero for a run
with no paid requests. It never invents an actual charge.

Runway task polls do not reserve another generation. A definite terminal failure
or rejected 429 can be retried within the budget. Ambiguous submission results
and poll timeouts do not resubmit; any known task ID is saved for later inspection.
Moderation failures are not retried with altered prompts. Downloads use a separate
unauthenticated session; signed output URLs and raw provider responses are not
saved. API failures trip a stage circuit breaker: development output is used for
the rest of that stage, and the cost report records why.

Fallback and mixed renders are always REVIEW_REQUIRED for publication purposes.
The existing playable-MP4 QC can pass a prototype, but that is not creative
acceptance or permission to publish it.

## Smallest next paid validation

After choosing accessible accounts/voice and setting keys, run one 2-second
scene plus a short narration sample under a deliberately small explicit budget.
Review character style, voice and timing first. Then authorize a complete story
with a ceiling covering the scene plan and desired retries. No purchase or
production API request was required for this coding milestone.

## Executed validation

- Local full suite: 66/66 passed, including all 44 pre-existing tests. No skips
  when FFMPEG_BINARY/FFPROBE_BINARY point to the installed binaries.
- Real video-asset assembly: generated a short local test clip, exercised the
  video branch, branding, hold-last-frame, captions/audio and full MP4 decode.
- Real 68-second 720x1280 development render: production selectors with absent
  credentials fell back for all three stages. QUALITY_CHECK_PASSED, all checks
  true, no paid requests, estimated and actual cost $0.
- Workflow YAML parsed; git diff whitespace check passed.
- Paid API integrations use mocks: no configured credentials were available.
  This proves contracts, fallback and spending controls, not paid output quality.

## Exact changed files

- .github/workflows/story-prototype.yml
- media_processor.py
- story_engine/costs.py
- story_engine/provider_http.py
- story_engine/llm_provider.py
- story_engine/runway_provider.py
- story_engine/elevenlabs_provider.py
- story_engine/provider_selection.py
- story_engine/providers.py
- story_engine/history.py
- story_engine/engine.py
- tests/test_production_providers.py
- PRODUCTION_PROVIDERS.md
- STORY_ENGINE.md
