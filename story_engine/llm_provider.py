"""Vendor-isolated OpenAI-compatible JSON chat story generation."""
from __future__ import annotations

import json
import os
import time
import uuid
import requests

from .costs import sanitize
from .history import DuplicatePremise
from .provider_http import ProviderFailure, attempts, endpoint, rate, request_json
from .schema import concept_fingerprint, estimate_duration, validate_story
from .providers import DemoStoryProvider
from .voice import caption_phrases


class ChatStoryProvider:
    def __init__(self, budget, history=None, session=None):
        self.budget, self.history = budget, history
        self.session = session or requests.Session()

    def generate(self, *, excluded_concepts: set[str], template=None):
        base = endpoint(os.getenv("STORY_LLM_BASE_URL", "https://api.openai.com/v1"))
        model = os.getenv("STORY_LLM_MODEL", "gpt-4.1-mini")
        known = base == "https://api.openai.com/v1" and model in {"gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"}
        input_rate = rate("STORY_LLM_INPUT_USD_PER_MILLION", ".40" if known else None)
        output_rate = rate("STORY_LLM_OUTPUT_USD_PER_MILLION", "1.60" if known else None)
        max_tokens = max(2000, min(12000, int(os.getenv("STORY_LLM_MAX_OUTPUT_TOKENS", "6000"))))
        example = DemoStoryProvider().generate(excluded_concepts=set())
        # Send the shape, not a finished example to accidentally imitate.
        shape = {k: v for k, v in example.items() if k not in {"concept_fingerprint", "template_id", "generation", "art_direction"}}
        for key in ("title", "concept", "hook", "full_script"):
            shape[key] = "<original " + key + ">"
        shape["characters"] = [{"character_id": "stable-id", "name": "Original name", "description": "Consistent visual appearance, wardrobe, personality"}]
        shape["scenes"] = [{**{k: "<" + k + ">" for k in example["scenes"][0] if isinstance(example["scenes"][0][k], str)}, "scene_number": 1, "start_time": 0, "estimated_duration": 5, "characters_present": ["stable-id"]}]
        previous = [v["concept"] for v in self.history.read()["stories"].values()] if self.history else []
        prompt = (
            "Write one ORIGINAL absurd/comedy fictional story in English as a JSON object. "
            "No existing franchises, real people, quoted jokes or copyrighted characters. "
            "175-195 spoken words, 60-75 seconds, 10-16 scenes each under 9 seconds. "
            "A compelling premise in the first 4-6 words; clear setup, escalation, surprising earned payoff. "
            "Standalone by default. No padding or forced cliffhanger. Full_script must be exactly scene narrations joined by spaces. "
            "Hook must be the beginning of scene one's narration. Use stable original character IDs and precise recurring descriptions. "
            "Supply a consistent art_direction.style and environment; image/video prompts describe visible action, camera and continuity. "
            "Do not put captions, logos or narration text into scene imagery. Scene timing is contiguous at 165 words/minute. "
            "Return the following shape, expanding scenes; set all platform publishing_enabled fields false:\n"
            + json.dumps(shape) + "\nAvoid these prior premises (including paraphrases):\n" + json.dumps(previous[-200:])
        )
        for retry in range(attempts()):
            messages = [{"role": "system", "content": "You write tightly paced original short comedy. Return valid JSON only."}, {"role": "user", "content": prompt + ("\nPrevious output was invalid or duplicate; generate a new valid story." if retry else "")}]
            # UTF-8 bytes plus framing overhead is a conservative token bound
            # for the default tokenizer; other vendors require verified rates.
            input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 256
            cost = (input_bound * input_rate + max_tokens * output_rate) / 1_000_000
            record = self.budget.reserve("chat-completions", model, cost, retry=retry, input_token_bound=input_bound, max_output_tokens=max_tokens)
            try:
                response = request_json(self.session, "POST", base + "/chat/completions", headers={"Authorization": "Bearer " + os.environ["STORY_LLM_API_KEY"]}, json={"model": model, "messages": messages, "response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens})
                usage = response.get("usage") or {}
                self.budget.update(record, status="RESPONSE_RECEIVED", usage={k: usage[k] for k in ("prompt_tokens", "completion_tokens") if k in usage})
                if all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                    self.budget.update(record, usage_estimated_usd=float((usage["prompt_tokens"] * input_rate + usage["completion_tokens"] * output_rate) / 1_000_000))
                story = sanitize(json.loads(response["choices"][0]["message"]["content"]))
                # IDs and timing are mechanical, not entrusted to the model.
                story["story_id"] = "story-" + uuid.uuid4().hex[:16]
                cursor = 0.0
                for number, scene in enumerate(story["scenes"], 1):
                    duration = estimate_duration(scene["narration"])
                    scene.update(scene_number=number, start_time=round(cursor, 3), estimated_duration=duration)
                    cursor += duration
                story["estimated_voice_duration_seconds"] = round(cursor, 3)
                story["concept_fingerprint"] = concept_fingerprint(story["concept"])
                for value in story["platform_metadata"].values():
                    value["publishing_enabled"] = False
                validate_story(story)
                if not all(isinstance(c[k], str) for c in story["characters"] for k in ("character_id", "name", "description")):
                    raise ValueError("character fields must be strings")
                art = story.get("art_direction") or {}
                if not isinstance(art, dict) or not all(isinstance(v, str) for v in art.values()):
                    raise ValueError("art direction fields must be strings")
                # Keep provider-neutral style/context; demo-specific colors and
                # prop enums from an LLM must not break the free fallback.
                story["art_direction"] = {k: art[k] for k in ("style", "environment") if k in art}
                caption_phrases(story["full_script"])
                if not 60 <= cursor <= 75 or len(story["hook"].split()) > 8 or not 10 <= len(story["scenes"]) <= 16:
                    raise ValueError("duration or opening hook outside editorial target")
                if story["concept_fingerprint"] in excluded_concepts:
                    raise ValueError("duplicate")
                if self.history:
                    self.history.ensure_new(story)
                story["generation"] = {"provider": "chat-completions", "model": model, "review_required": True}
                self.budget.update(record, status="SUCCEEDED", generated_assets=1)
                return story
            except ProviderFailure as exc:
                self.budget.update(record, status=str(exc))
                if not exc.retryable:
                    raise
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, DuplicatePremise):
                self.budget.update(record, status="INVALID_OR_DUPLICATE_STORY")
            if retry + 1 < attempts():
                time.sleep(min(2 ** retry, 4))
        raise ProviderFailure("STORY_ATTEMPTS_EXHAUSTED")
