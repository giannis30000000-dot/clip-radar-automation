# Dialogue-driven story milestone

The production story selector now uses DialogueStoryProvider. The existing
ChatStoryProvider, narrated demos, visual/TTS adapters and comparison command
remain available. No publishing, scheduling, dashboard, Twitch code, dependency
or workflow was changed. Defaults still cannot spend without an explicit budget.

## What runs before visual spending

1. A production LLM request generates at least three distinct concepts. Each has
   hook, curiosity, interest, dialogue, visual, escalation, payoff, originality
   and retention scores. Candidates need average >=7, every metric >=6, hook >=8
   and payoff >=7. Persistent premise/near-duplicate history applies before
   selection; a trope used twice in the latest three stories is rejected.
2. Select the highest-scoring eligible concept, not a forced category quota.
   Humans, food, animals, objects, fantasy and science-fiction casts are welcome.
   Character IDs only identify speakers inside one story; no mascot is required.
3. Generate the dialogue script. A separate LLM critique scores ten editorial
   criteria, each requiring >=7. Schema/structural checks also run locally.
4. Rewrite rejected scripts with actionable feedback. STORY_QUALITY_MAX_ATTEMPTS
   defaults to 3 and is capped at 3, including subsequent measured-timing rewrites
   by that production provider. Candidate batches are also bounded to 3; malformed
   JSON retries retain the existing 1-3 API attempt bound. All calls/retries share
   the existing CostBudget; no new budget bypass exists.
5. Only a passing script reaches voice synthesis. Measure actual per-line audio,
   hook duration and total duration; recheck quality BEFORE creating any visuals.
   If speech is outside the target, rewrite dialogue with its measured timing as
   feedback, not time stretching or padding. If attempts are exhausted, return
   REVIEW_REQUIRED / STORY_QUALITY_REJECTED_BEFORE_VISUALS, with no visual calls.
6. Existing ffmpeg assembly and final QC run after those gates pass.

Saved evidence: candidate_selection.json and story_quality_attempt_*.json beside
the invocation cost ledger, plus per-story story_quality.json, dialogue_timing.json,
story.json, captions, render manifest and final quality.json.

The checks cover a <=2-second hook; setup/goal/conflict; escalation; at least three
payoff/reaction beats including an ending; >=80% dialogue words; bounded lines;
speaker turns; exact repetition; scene-prompt variety; exposition warning phrases;
schema validity and duration. The separate production critique evaluates semantic
coherence, naturalness and payoff. These are editorial safeguards, NOT proof of
originality, copyright clearance, audience retention or genuinely funny writing.
The authored offline demo uses structural checks, not a pretend paid critique.

## Schema and voices

Schema version 2 adds:

- Per-character personality, speaking_style, visual_description and
  voice_profile_hint, alongside existing name/description/character_id.
- dialogue lines: speaker_id, text, emotion, order, intended_start_time,
  estimated_duration_seconds, scene_number, action and listeners.
- Scene active_speaker, visual_prompt, action_direction, reaction_direction,
  background_music_mood and payoff_moment; existing camera/SFX/transition fields
  remain. narration/full_script are compatibility projections of spoken dialogue,
  not a separate narrator track.
- voice_assignments and actual measured line/scene timings after synthesis.

Production voice IDs come ONLY from account configuration, not LLM suggestions:

    STORY_VOICE_MAP={"mira":"voice_A","ben":"voice_B","lift":"voice_C"}

Or use STORY_VOICE_POOL as a JSON list of distinct IDs for new casts; unmapped
speakers receive the next unused pool ID in first-speaking order. Optional
narrator uses ID narrator, with an explicit map entry or ELEVENLABS_VOICE_ID.
All actual speakers must have distinct IDs; missing/invalid assignments use the
existing development fallback, never silently one production voice for everyone.

ElevenLabs synthesizes each line separately using the selected character's voice.
Character alignment is validated against that exact line before captions are
offset onto the master timeline. API emotion control is not claimed: emotion and
voice-profile hints are stored as direction; the selected v2 voice controls delivery.
Budget reservations include every line/retry, including unusable paid audio.

Development uses espeak voice variants or installed Windows English voices.
Windows may reuse a voice if too few are installed. Speaker roles/IDs remain
distinct; these are testing voices, not production-quality character performances.
The new dialogue path performs loudness normalization but NO atempo/time stretch.
Short 120ms turn gaps separate speech; duration is reached through actual dialogue.

Captions reuse the existing single ASS/SRT system, safe vertical position and
mobile text size. Each entry carries speaker_id and line_order; subtle speaker
color is optional presentation within that same layer. QC verifies speaker/line
timing in addition to complete text, no overlaps, audio, video and scene coverage.
Music/SFX hints and character emotion are directions, not newly generated tracks.

## Configuration and limits

- STORY_PROVIDER=production selects the candidate/dialogue/critique implementation.
- STORY_FORMAT=dialogue enables the authored demo when using development providers;
  alternatively use --template dialogue-demo. Legacy demos still work unchanged.
- STORY_MIN_DURATION_SECONDS defaults to 65; maximum is 75. Deliberately setting
  a value below 60 (minimum 45) is the explicit short-video override. Saved/model
  metadata cannot lower the current configured limit.
- STORY_QUALITY_MAX_ATTEMPTS defaults to 3, hard maximum 3.
- STORY_VOICE_MAP or STORY_VOICE_POOL supplies distinct ElevenLabs IDs.
- Existing STORY_LLM_API_KEY, ELEVENLABS_API_KEY, RUNWAYML_API_SECRET, model/rate
  variables and CLIP_RADAR_MAX_COST_USD_PER_VIDEO remain in effect. No keys added.

The manual GitHub workflow was intentionally left unchanged. For a later cloud
dialogue run, pass the new voice-map/pool and optional duration/format settings
into its existing generation step; repository variables alone are not inherited
unless mapped to the job environment.

## Executed free demo

Command (with providers set to development and per-video budget 0):

    python -m story_engine --template dialogue-demo --output-dir output/dialogue-demo --state-file output/dialogue-demo/demo_history.json

Use a fresh output/history directory for a new isolated test, or the existing
explicit versioned regeneration option; never delete persistent history to reroll.

Selected: **The Elevator Interview**. An elevator interviews two workers before
letting them upstairs, then hires Mira to do its lifting and resigns. The separate
authored demo candidates score 9/10 (elevator), 8/10 (dragon bakery), and 3/10
(bananas saying random colors and vanishing). The banana concept is rejected for
weak hook/interest, no causality and no earned payoff. Those are demo fixture
scores, not evidence that a paid LLM was called.

- Final duration: **66.033 seconds**, 720x1280 H.264/AAC.
- Three speaking characters: Mira, Ben and Lift; 20 dialogue lines, 10 scenes.
- First hook delivered in **1.34 seconds**; tempo_factor=1.0.
- All **19 final QC checks passed**. Rendered contact sheet inspected: readable
  single caption track, clear cast labels and explicitly labeled placeholder art.
- Local final: output/dialogue-demo/dialogue-elevator-interview/final.mp4.
- No paid requests; cost report records **$0**. No cloud generation dispatched.
- Automated suite: **98/98 passed**, preserving all prior 76 tests; new tests cover
  selection, weak rejection, critique rewrites, limits, mapping/narrator, alignment,
  captions, dedupe, category variety, fallback and pre-visual spending barriers.

## Smallest next paid step

Review the demo's writing/pacing first. Then configure the existing LLM/TTS/Runway
keys, a distinct voice map/pool and an explicitly approved small budget. Test one
dialogue exchange plus one H3 Max scene before authorizing a complete video.
H3 Max remains selectable with RUNWAY_MODEL=h3_max; it still needs a permitted
first-frame reference_image_url on each scene in this adapter. Prepare matching
reference frames before a full render; reference-image generation and lip-sync
are not implemented or claimed in this milestone. The full production pipeline
still cannot spend visual credits until script AND measured voice timing pass.

Official OpenAI documentation informed the JSON contract: JSON mode alone does
not validate the schema, hence the explicit local validation/rewrite stages.
[Structured output guidance](https://developers.openai.com/api/docs/guides/structured-outputs).
Voice alignment uses the existing [ElevenLabs timestamp API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps).

## Exact changed files

- story_engine/dialogue.py
- story_engine/dialogue_demo.py
- story_engine/dialogue_provider.py
- story_engine/dialogue_voice.py
- story_engine/schema.py
- story_engine/providers.py
- story_engine/provider_selection.py
- story_engine/engine.py
- story_engine/history.py
- story_engine/voice.py
- story_engine/elevenlabs_provider.py
- story_engine/sapi_voice.ps1
- story_engine/visuals.py
- media_processor.py
- quality_control.py
- tests/test_dialogue_story.py
- DIALOGUE_STORIES.md
