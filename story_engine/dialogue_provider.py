"""Candidate selection, script writing and separate editorial critique; bounded."""
import json
import os
import uuid

from .costs import sanitize
from .dialogue import CATEGORIES, CONCEPT_METRICS, QUALITY_METRICS, StoryQualityFailure, duration_policy, evaluate_story, max_attempts, normalize_dialogue, select_concept
from .history import DuplicatePremise, write_json
from .llm_provider import ChatStoryProvider
from .provider_http import ProviderFailure, attempts, endpoint, rate, request_json
from .schema import validate_story


class DialogueStoryProvider(ChatStoryProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.script_attempts = 0

    def _request(self, prompt, stage):
        base = endpoint(os.getenv("STORY_LLM_BASE_URL", "https://api.openai.com/v1"))
        model = os.getenv("STORY_LLM_MODEL", "gpt-4.1-mini")
        known = base == "https://api.openai.com/v1" and model in {"gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"}
        input_rate = rate("STORY_LLM_INPUT_USD_PER_MILLION", ".40" if known else None)
        output_rate = rate("STORY_LLM_OUTPUT_USD_PER_MILLION", "1.60" if known else None)
        maximum = max(2000, min(12000, int(os.getenv("STORY_LLM_MAX_OUTPUT_TOKENS", "6000"))))
        messages = [{"role": "system", "content": "You are an original short-form dialogue comedy writer/editor. Return a valid JSON object, never markdown. No franchises or copied jokes."}, {"role": "user", "content": prompt}]
        bound = len(json.dumps(messages, ensure_ascii=False).encode()) + 256
        for retry in range(attempts()):
            record = self.budget.reserve("chat-completions", model, (bound * input_rate + maximum * output_rate) / 1_000_000, retry=retry, stage=stage)
            try:
                response = request_json(self.session, "POST", base + "/chat/completions", headers={"Authorization": "Bearer " + os.environ["STORY_LLM_API_KEY"]}, json={"model": model, "messages": messages, "response_format": {"type": "json_object"}, "max_completion_tokens": maximum})
                result = sanitize(json.loads(response["choices"][0]["message"]["content"]))
                if not isinstance(result, dict):
                    raise ValueError("object required")
                usage = response.get("usage") or {}
                self.budget.update(record, status="SUCCEEDED", usage={k: usage[k] for k in ("prompt_tokens", "completion_tokens") if isinstance(usage.get(k), int)})
                return result
            except ProviderFailure as exc:
                self.budget.update(record, status=str(exc))
                if not exc.retryable:
                    raise
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                self.budget.update(record, status="INVALID_STRUCTURED_OUTPUT")
        raise ProviderFailure("DIALOGUE_JSON_ATTEMPTS_EXHAUSTED")

    def generate(self, *, excluded_concepts, template=None):
        duration_policy()
        history = list(self.history.read()["stories"].values()) if self.history else []
        recent = [{k: s.get(k) for k in ("concept", "content_category", "trope")} for s in history[-50:]]
        feedback = ""
        for attempt in range(max_attempts()):
            ideas = self._request(
                "Generate at least THREE distinct candidate concepts BEFORE writing a script. No scripts yet. "
                "Every video can use an entirely new world, cast and art style: humans, animals, food, objects, aliens, fantasy creatures. No permanent mascot. "
                "Choose quality over forced category rotation. Avoid recent premises AND repeated tropes. "
                "Return {candidates:[{concept,category,trope,scores:{metric:0..10}}]}. Score every metric honestly; reject randomness without causality/payoff. "
                f"Metrics: {CONCEPT_METRICS}. Categories: {CATEGORIES}. Prior history: {json.dumps(recent)}. Feedback: {feedback}", "candidate_concepts")
            try:
                selected, scored = select_concept(ideas["candidates"], excluded_concepts, self.history)
                self.selection = {"selected": selected, "candidates": scored}
                write_json(self.budget.paths[0].with_name("candidate_selection.json"), self.selection)
                return self._write_story(selected)
            except (StoryQualityFailure, KeyError, TypeError) as exc:
                feedback = type(exc).__name__ + ": weak, duplicate or invalid candidate batch"
                self.budget.event("story", "CANDIDATES_REJECTED", attempt=attempt + 1)
        raise ProviderFailure("NO_STRONG_DIALOGUE_CONCEPT")

    def _write_story(self, selected, feedback="", reserved=False):
        while self.script_attempts < max_attempts():
            self.script_attempts += 1
            policy = duration_policy()
            visual_instructions = ""
            if os.getenv("STORY_REFERENCE_PROVIDER") == "runway":
                pool = json.loads(os.getenv("STORY_VOICE_POOL") or "[]")
                visual_instructions = (
                    f"Use 2-{min(4, len(pool)) if len(pool) >= 2 else 3} speaking characters, no narrator. "
                    "Give each character visual_identity:{appearance,proportions,clothing_accessories,colors,facial_traits}. "
                    "Each value 1-3 words; concrete distinctive designs, matching visual_description. "
                    "Keep art_direction style and environment to 3-6 words each. "
                    "All character IDs plus identity values and both art fields must total under 430 characters, for a compact shared visual bible. "
                    "Every visual_prompt starts with the important physical action, camera directions 2-5 words. "
                    "No visible text, signage, logos or UI. Favor medium/wide expressive acting and listener reactions; no mouth closeups or precise lip-sync. "
                )
            prompt = (
                f"Write the selected concept as a {policy['minimum']}-75 second DIALOGUE-FIRST skit. Default aim 65-75s, about 175-190 words at 165wpm plus brief turn pauses. "
                "2-4 speaking characters; optional narrator ID narrator with under 20% of words. New cast/world/style allowed every video. "
                "First line is ONLY a 4-5-word immediate hook. Then goal, conflict, causal escalation, at least three reaction/punchline beats and final earned payoff. "
                "Natural short character-specific turns, no exposition dumps, filler, repeated gags or artificial stretching. 8-12 scenes, each under 10 seconds, 1-3 lines each. "
                "Return JSON with title, hook (exact opening text), ending_type=standalone unless a sequel truly improves it, sequel_possible:boolean, "
                "characters:[{character_id,name,personality,speaking_style,visual_description,description,voice_profile_hint}], "
                "dialogue:[{speaker_id,text,emotion,scene_number,action,listeners:[character_id]}] in playback order, "
                "scenes:[{scene_number,characters_present,visual_description,visual_prompt,action_direction,reaction_direction,camera_direction,sound_effect_hint,background_music_mood,transition_hint,payoff_moment:boolean}], "
                "art_direction:{style,environment}, story_beats:{setup,goal,conflict,escalation:[at least two causal beats],payoff}, "
                "platform_metadata:{instagram:{caption},tiktok:{caption},youtube:{caption}}. No voice IDs, no existing characters. "
                + visual_instructions +
                f"Selected concept: {json.dumps(selected)}. Rewrite feedback: {feedback}"
            )
            try:
                story = self._request(prompt, "dialogue_script")
                story.update(story_id="dialogue-" + uuid.uuid4().hex[:16], concept=selected["concept"], content_category=selected["category"], trope=selected["trope"])
                story["generation"] = {"provider": "dialogue-chat", "candidate_selection": self.selection, "script_attempts": self.script_attempts, "review_required": True}
                story = normalize_dialogue(story)
                validate_story(story)
                if os.getenv("STORY_REFERENCE_PROVIDER") == "runway":
                    from .reference_images import build_visual_bible
                    story["visual_bible"] = build_visual_bible(story)
                critique = self._request("Independently evaluate this script, not its author's confidence. Score each requested criterion 0-10 with minimum acceptable 7. Be strict about causal escalation, conversational dialogue, first-two-second hook, final payoff and 65-75s engagement without filler. Return {scores:{metric:number},issues:[short actionable issues]}. Criteria: " + json.dumps(QUALITY_METRICS) + ". Script: " + json.dumps(story), "editorial_critique")
                story["generation"]["editorial_review"] = critique["scores"]
                quality = evaluate_story(story)
                write_json(self.budget.paths[0].with_name(f"story_quality_attempt_{self.script_attempts}.json"), {"quality": quality, "critique": critique})
                if quality["status"] == "PASSED":
                    if self.history and not reserved:
                        self.history.ensure_new(story)
                    story["story_quality"] = quality
                    return story
                feedback = json.dumps({"failed_checks": quality["failed_checks"], "issues": critique.get("issues", [])})
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, DuplicatePremise) as exc:
                feedback = "Invalid schema, dialogue timing, duplicate premise or missing required fields; rewrite coherently. " + str(sanitize(str(exc)))[:250]
            self.budget.event("story", "STORY_REWRITE_REQUIRED", attempt=self.script_attempts, feedback=feedback)
        raise ProviderFailure("STORY_QUALITY_ATTEMPTS_EXHAUSTED")

    def rewrite(self, story, feedback):
        measured = {"quality_feedback": feedback, "previous_script": story["full_script"], "measured_duration_seconds": story.get("actual_voice_duration_seconds"), "instruction": "Revise actual dialogue length with useful escalation or tighter turns; never pad, slow speech or add dead air."}
        rewritten = self._write_story(self.selection["selected"], json.dumps(measured), reserved=True)
        rewritten["story_id"] = story["story_id"]
        return rewritten
