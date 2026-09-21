"""Candidate selection, script writing and separate editorial critique; bounded."""
import json
import os
import re
import time
import uuid

from .costs import sanitize
from .dialogue import CATEGORIES, CONCEPT_METRICS, QUALITY_METRICS, StoryQualityFailure, duration_policy, evaluate_story, max_attempts, normalize_dialogue, select_concept
from .history import DuplicatePremise, write_json
from .llm_provider import ChatStoryProvider
from .provider_http import ProviderFailure, attempts, endpoint, rate, request_json
from .schema import validate_story


def candidate_response_schema():
    # Keep to the portable strict-schema subset; local validation enforces the
    # score range and the minimum of three candidates.
    score_properties = {metric: {"type": "number"} for metric in CONCEPT_METRICS}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["concept", "category", "trope", "scores"],
                    "properties": {
                        "concept": {"type": "string"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "trope": {"type": "string"},
                        "scores": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": list(CONCEPT_METRICS),
                            "properties": score_properties,
                        },
                    },
                },
            }
        },
    }


def _supports_candidate_schema(base, model):
    if base != "https://api.openai.com/v1":
        return False
    return model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6"))


def _parse_json_object(content):
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("structured response content missing")
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("structured response is not a JSON object")


def _validate_candidate_payload(payload):
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or len(candidates) < 3:
        raise ValueError("candidate response requires at least three candidates")
    for candidate in candidates:
        if not isinstance(candidate, dict) or not all(isinstance(candidate.get(k), str) and candidate[k].strip() for k in ("concept", "category", "trope")):
            raise ValueError("candidate fields must be non-empty strings")
        scores = candidate.get("scores")
        if not isinstance(scores, dict) or any(type(scores.get(k)) not in (int, float) or not 0 <= scores[k] <= 10 for k in CONCEPT_METRICS):
            raise ValueError("candidate scores must contain every metric from 0 to 10")
    return payload


def _dialogue_timing_summary(story):
    """Return safe, mechanical timing facts for rewrite feedback and telemetry."""
    lines = story.get("dialogue") if isinstance(story, dict) else None
    word_count = sum(len(line.get("text", "").split()) for line in lines if isinstance(line, dict)) if isinstance(lines, list) else 0
    duration = story.get("estimated_voice_duration_seconds") if isinstance(story, dict) else None
    return word_count, duration


def _require_production_timing(story):
    """Reject a script outside the existing production duration policy before critique/paid media."""
    word_count, duration = _dialogue_timing_summary(story)
    policy = duration_policy()
    if not isinstance(duration, (int, float)) or not policy["minimum"] <= duration <= policy["maximum"]:
        raise ValueError(
            f"DIALOGUE_TIMING_CONTRACT_FAILED: {word_count} spoken words normalize to {duration!r}s; "
            f"production requires {policy['minimum']:.0f}-{policy['maximum']:.0f}s. "
            "Rewrite with meaningful conversational turns covering escalation and payoff, never filler."
        )
    return word_count, duration


def _timing_repair_instruction(feedback):
    if "DIALOGUE_TIMING_CONTRACT_FAILED" not in feedback:
        return ""
    match = re.search(r"(\d+) spoken words normalize to ([0-9.]+)s", feedback)
    if not match:
        return (
            "This is a mandatory timing repair after a rejected draft. Write a complete replacement, not a summary or outline. "
            "Aim for approximately 190 spoken words across 20-23 meaningful turns. "
        )
    previous_words = int(match.group(1))
    delta = 190 - previous_words
    if delta > 0:
        adjustment = f"add about {delta} meaningful words"
    elif delta < 0:
        adjustment = f"trim about {-delta} redundant words"
    else:
        adjustment = "keep the dialogue word count nearly unchanged"
    return (
        f"MANDATORY TIMING REPAIR: the previous complete draft measured {previous_words} spoken words and {match.group(2)} seconds. "
        "Write a complete replacement, not a summary or outline, with exactly 10 scenes and exactly 20 dialogue objects (two per scene), targeting 190 spoken words (188-192 acceptable); "
        f"{adjustment}. If short, add one causal reaction or payoff beat; if long, combine redundant reactions. "
        "Do not repeat the same short structure, pad, slow speech or add dead air. "
    )


def _scene_repair_instruction(feedback):
    if "scene gap, overlap or unreasonable length" not in feedback:
        return ""
    return (
        "MANDATORY SCENE-TIMING REPAIR: keep scenes numbered contiguously from 1, with 1-3 dialogue lines in every scene. "
        "Every dialogue line must contain at least two spoken words, and every normalized scene must remain between 0.5 and 12 seconds. "
        "Do not create a one-word reaction scene or leave a scene without dialogue. "
    )


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
        strict_schema = stage == "candidate_concepts" and _supports_candidate_schema(base, model)
        for retry in range(attempts()):
            record = self.budget.reserve("chat-completions", model, (bound * input_rate + maximum * output_rate) / 1_000_000, retry=retry, stage=stage)
            try:
                response_format = {"type": "json_object"}
                if strict_schema:
                    response_format = {"type": "json_schema", "json_schema": {"name": "clip_radar_candidate_concepts", "strict": True, "schema": candidate_response_schema()}}
                response = request_json(self.session, "POST", base + "/chat/completions", headers={"Authorization": "Bearer " + os.environ["STORY_LLM_API_KEY"]}, json={"model": model, "messages": messages, "response_format": response_format, "max_completion_tokens": maximum})
                message = response["choices"][0]["message"]
                if message.get("refusal"):
                    raise ValueError("model refusal")
                result = sanitize(_parse_json_object(message.get("content")))
                if stage == "candidate_concepts":
                    _validate_candidate_payload(result)
                usage = response.get("usage") or {}
                self.budget.update(record, status="SUCCEEDED", usage={k: usage[k] for k in ("prompt_tokens", "completion_tokens") if isinstance(usage.get(k), int)})
                return result
            except ProviderFailure as exc:
                fields = {"status": str(exc)}
                if exc.detail:
                    fields["provider_error_type"] = exc.detail
                self.budget.update(record, **fields)
                if strict_schema and str(exc) == "HTTP_400":
                    # A compatible proxy may reject JSON Schema; retry once in
                    # legacy JSON mode while retaining local validation.
                    strict_schema = False
                    if retry + 1 < attempts():
                        continue
                if not exc.retryable:
                    raise
                if retry + 1 < attempts():
                    # Give transient 429/5xx responses a bounded pause; the
                    # reservation already protects the per-video ceiling.
                    delay = max(0.0, min(30.0, float(os.getenv("STORY_PROVIDER_RETRY_DELAY_SECONDS", "5"))))
                    time.sleep(delay * (retry + 1))
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, json.JSONDecodeError):
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
            timing_repair = _timing_repair_instruction(feedback)
            scene_repair = _scene_repair_instruction(feedback)
            prompt = (
                f"Write the selected concept as a {policy['minimum']}-75 second DIALOGUE-FIRST skit. Default aim 65-75s. "
                "Hard timing contract: return 188-192 spoken dialogue words across exactly 20 dialogue objects; count only dialogue[].text, not action or visual fields. "
                "At 165 words per minute plus brief turn pauses, this must mechanically normalize to 65-75 seconds. "
                "Before returning JSON, self-check the word count and add meaningful reaction/escalation turns if it is short; tighten redundant turns if it is long. Never pad, repeat a gag, slow speech or add dead air. "
                "2-4 speaking characters; optional narrator ID narrator with under 20% of words. New cast/world/style allowed every video. "
                "First dialogue line is ONLY a 4-5-word immediate hook; set hook character-for-character equal to dialogue[0].text, including punctuation and capitalization. Then goal, conflict, causal escalation, at least three reaction/punchline beats and final earned payoff. "
                "Use exactly 10 contiguous scenes with exactly 2 dialogue lines per scene: line 1 is 4-5 words and lines 2-20 are conversational 9-11-word turns. No exposition dumps or generic narration; every line has at least two spoken words. "
                "Return JSON with title, hook (exact opening text), ending_type=standalone unless a sequel truly improves it, sequel_possible:boolean, "
                "characters:[{character_id,name,personality,speaking_style,visual_description,description,voice_profile_hint}], "
                "dialogue:[{speaker_id,text,emotion,scene_number,action,listeners:[character_id]}] in playback order, "
                "scenes:[{scene_number,characters_present,visual_description,visual_prompt,action_direction,reaction_direction,camera_direction,sound_effect_hint,background_music_mood,transition_hint,payoff_moment:boolean}], "
                "art_direction:{style,environment}, story_beats:{setup,goal,conflict,escalation:[at least two causal beats],payoff}, "
                "platform_metadata:{instagram:{caption},tiktok:{caption},youtube:{caption}}. No voice IDs, no existing characters. "
                + visual_instructions +
                timing_repair + scene_repair + f"Selected concept: {json.dumps(selected)}. Rewrite feedback: {feedback}"
            )
            normalized_story = None
            try:
                story = self._request(prompt, "dialogue_script")
                story.update(story_id="dialogue-" + uuid.uuid4().hex[:16], concept=selected["concept"], content_category=selected["category"], trope=selected["trope"])
                story["generation"] = {"provider": "dialogue-chat", "candidate_selection": self.selection, "script_attempts": self.script_attempts, "review_required": True}
                normalized_story = normalize_dialogue(story)
                word_count, duration = _require_production_timing(normalized_story)
                normalized_story["generation"].update(dialogue_word_count=word_count, estimated_dialogue_duration_seconds=duration)
                validate_story(normalized_story)
                if os.getenv("STORY_REFERENCE_PROVIDER") == "runway":
                    from .reference_images import build_visual_bible
                    normalized_story["visual_bible"] = build_visual_bible(normalized_story)
                critique = self._request("Independently evaluate this script, not its author's confidence. Score each requested criterion 0-10 with minimum acceptable 7. Be strict about causal escalation, conversational dialogue, first-two-second hook, final payoff and 65-75s engagement without filler. Return {scores:{metric:number},issues:[short actionable issues]}. Criteria: " + json.dumps(QUALITY_METRICS) + ". Script: " + json.dumps(normalized_story), "editorial_critique")
                normalized_story["generation"]["editorial_review"] = critique["scores"]
                quality = evaluate_story(normalized_story)
                write_json(self.budget.paths[0].with_name(f"story_quality_attempt_{self.script_attempts}.json"), {"quality": quality, "critique": critique})
                if quality["status"] == "PASSED":
                    if self.history and not reserved:
                        self.history.ensure_new(normalized_story)
                    normalized_story["story_quality"] = quality
                    return normalized_story
                feedback = json.dumps({"failed_checks": quality["failed_checks"], "issues": critique.get("issues", [])})
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, DuplicatePremise) as exc:
                timing = ""
                if normalized_story is not None:
                    words, duration = _dialogue_timing_summary(normalized_story)
                    timing = f" Measured dialogue before rejection: {words} words, {duration!r}s after normalization."
                feedback = "Invalid schema, dialogue timing, duplicate premise or missing required fields; rewrite coherently. " + str(sanitize(str(exc)))[:250] + timing
            event_fields = {"attempt": self.script_attempts, "feedback": feedback}
            if normalized_story is not None:
                words, duration = _dialogue_timing_summary(normalized_story)
                event_fields.update(dialogue_word_count=words, estimated_dialogue_duration_seconds=duration)
            self.budget.event("story", "STORY_REWRITE_REQUIRED", **event_fields)
        raise ProviderFailure("STORY_QUALITY_ATTEMPTS_EXHAUSTED")

    def rewrite(self, story, feedback):
        measured = {"quality_feedback": feedback, "previous_script": story["full_script"], "measured_duration_seconds": story.get("actual_voice_duration_seconds"), "instruction": "Revise actual dialogue length with useful escalation or tighter turns; never pad, slow speech or add dead air."}
        rewritten = self._write_story(self.selection["selected"], json.dumps(measured), reserved=True)
        rewritten["story_id"] = story["story_id"]
        return rewritten
