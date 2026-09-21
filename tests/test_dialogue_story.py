import base64
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from media_processor import write_ass
from quality_control import _read_captions
from story_engine.costs import BudgetExceeded, CostBudget
from story_engine.dialogue import CONCEPT_METRICS, QUALITY_METRICS, StoryQualityFailure, evaluate_story, normalize_dialogue, select_concept
from story_engine.dialogue_demo import DialogueDemoProvider, demo_candidates
from story_engine.dialogue_provider import DialogueStoryProvider
from story_engine.dialogue_voice import apply_timeline, assign_voices, synthesize_dialogue
from story_engine.elevenlabs_provider import ElevenLabsVoiceProvider
from story_engine.engine import generate_story_video
from story_engine.history import DuplicatePremise, StoryHistory
from story_engine.provider_http import ProviderFailure
from story_engine.providers import DemoStoryProvider, load_provider
from story_engine.schema import StoryValidationError, validate_story
from story_engine.voice import DevelopmentVoiceProvider


def demo():
    return DialogueDemoProvider().generate(excluded_concepts=set())


class DialogueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"STORY_MIN_DURATION_SECONDS": "65", "STORY_QUALITY_MAX_ATTEMPTS": "3", "STORY_FORMAT": "", "STORY_PROVIDER": "development", "VISUAL_PROVIDER": "development", "VOICE_PROVIDER": "development", "STORY_VOICE_MAP": "", "STORY_VOICE_POOL": "", "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "0", "ELEVENLABS_API_KEY": "mock-dialogue-secret", "ELEVENLABS_MODEL": "eleven_multilingual_v2", "ELEVENLABS_USD_PER_1K_CHARACTERS": ".10"})
        env.start()
        self.addCleanup(env.stop)

    def test_three_candidates_scored_and_strongest_selected(self):
        selected, scored = select_concept(demo_candidates(), set())
        self.assertEqual(len(scored), 3)
        self.assertEqual(selected["score"], 9)
        self.assertEqual(set(selected["scores"]), set(CONCEPT_METRICS))
        self.assertIn("weak_hook_interest_or_payoff", scored[2]["rejected_reasons"])

    def test_weak_and_non_distinct_batches_rejected(self):
        for candidates in ([demo_candidates()[2]] * 3, demo_candidates()[:2]):
            with self.assertRaises(StoryQualityFailure):
                select_concept(candidates, set())

    def test_category_variation_is_quality_driven_not_a_fixed_cast(self):
        ideas = demo_candidates()
        ideas[1]["scores"] = {k: 10 for k in CONCEPT_METRICS}
        self.assertEqual(select_concept(ideas, set())[0]["category"], "fantasy_scifi")
        self.assertEqual(select_concept(demo_candidates(), set())[0]["category"], "situational_comedy")

    def test_history_dedupe_and_recent_trope_penalty(self):
        history = StoryHistory(self.root / "history.json")
        s = demo()
        history.reserve(s, self.root / "old.mp4")
        with self.assertRaises(DuplicatePremise):
            history.reserve(s, self.root / "duplicate.mp4")
        winner, scored = select_concept(demo_candidates(), history.fingerprints(), history)
        self.assertEqual(winner["category"], "fantasy_scifi")
        self.assertIn("history_duplicate", scored[0]["rejected_reasons"])
        for index in range(2):
            other = copy.deepcopy(s)
            other.update(story_id=f"other-{index}", concept=f"Distinct premise {index} about a cloud saving a lost village")
            # Distinct enough to exercise trope history rather than premise overlap.
            other["concept"] = ("A cloud saves a dry village" if index == 0 else "An octopus opens a shoe shop")
            history.reserve(other, self.root / f"other-{index}.mp4")
        self.assertIn("recent_trope_repeated", select_concept(demo_candidates(), set(), history)[1][0]["rejected_reasons"])

    def test_dialogue_schema_and_conversational_duration(self):
        s = demo()
        validate_story(s)
        self.assertEqual(len(s["characters"]), 3)
        self.assertEqual(len(s["dialogue"]), 20)
        self.assertTrue(65 <= s["estimated_voice_duration_seconds"] <= 75)
        self.assertEqual(evaluate_story(s)["status"], "PASSED")

    def test_normalization_derives_hook_from_first_dialogue_line(self):
        s = demo()
        s["hook"] = "A different hook"
        normalized = normalize_dialogue(s)
        self.assertEqual(normalized["hook"], normalized["dialogue"][0]["text"])
        self.assertEqual(evaluate_story(normalized)["status"], "PASSED")

    def test_schema_rejects_unknown_speaker_listener_and_overlapping_line(self):
        for mutate in (lambda s: s["dialogue"][0].update(speaker_id="unknown"), lambda s: s["dialogue"][0].update(listeners=["unknown"]), lambda s: s["dialogue"][1].update(intended_start_time=0), lambda s: s["scenes"][0].update(active_speaker="ben")):
            s = demo()
            mutate(s)
            with self.assertRaises(StoryValidationError):
                validate_story(s)

    def test_duration_below_sixty_requires_explicit_override(self):
        s = demo()
        factor = 58 / s["estimated_voice_duration_seconds"]
        apply_timeline(s, [l["estimated_duration_seconds"] * factor for l in s["dialogue"]])
        self.assertIn("duration", evaluate_story(s)["failed_checks"])
        with patch.dict(os.environ, {"STORY_MIN_DURATION_SECONDS": "55"}):
            self.assertEqual(evaluate_story(s)["status"], "PASSED")

    def test_quality_rejects_repetition_exposition_missing_payoff_and_long_hook(self):
        for mutate, check in ((lambda s: s.update(hook=s["dialogue"][0]["text"] + " And another long explanation."), "schema"), (lambda s: s["story_beats"].update(conflict=""), "setup_goal_conflict"), (lambda s: s["scenes"][-1].update(payoff_moment=False), "reaction_payoffs"), (lambda s: s["dialogue"][2].update(text=s["dialogue"][0]["text"]), "no_repetition")):
            s = demo()
            mutate(s)
            if check != "schema":
                normalize_dialogue(s)
            self.assertIn(check, evaluate_story(s)["failed_checks"])

    def test_production_generates_candidates_then_script_then_critique(self):
        provider = DialogueStoryProvider(CostBudget(self.root / "cost.json", 0))
        provider._request = Mock(side_effect=[{"candidates": demo_candidates()}, demo(), {"scores": {k: 9 for k in QUALITY_METRICS}}])
        result = provider.generate(excluded_concepts=set())
        self.assertEqual([c.args[1] for c in provider._request.call_args_list], ["candidate_concepts", "dialogue_script", "editorial_critique"])
        self.assertEqual(result["story_quality"]["status"], "PASSED")

    def test_short_script_gets_measured_timing_feedback_before_critique(self):
        short = demo()
        for line in short["dialogue"]:
            line["text"] = "No way."
        provider = DialogueStoryProvider(CostBudget(self.root / "timing-feedback.json", 0))
        provider._request = Mock(side_effect=[
            {"candidates": demo_candidates()},
            short,
            demo(),
            {"scores": {k: 9 for k in QUALITY_METRICS}},
        ])
        result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["story_quality"]["status"], "PASSED")
        self.assertEqual([c.args[1] for c in provider._request.call_args_list], ["candidate_concepts", "dialogue_script", "dialogue_script", "editorial_critique"])
        rewrite_prompt = provider._request.call_args_list[2].args[0]
        self.assertIn("DIALOGUE_TIMING_CONTRACT_FAILED", rewrite_prompt)
        self.assertIn("spoken words", rewrite_prompt)
        self.assertIn("after normalization", rewrite_prompt)
        self.assertIn("targeting 190 spoken words", rewrite_prompt)
        self.assertIn("exactly 20 dialogue objects", rewrite_prompt)
        self.assertIn("add about 150 meaningful words", rewrite_prompt)
        self.assertEqual(provider.budget.events[0]["dialogue_word_count"], 40)
        self.assertLess(provider.budget.events[0]["estimated_dialogue_duration_seconds"], 65)

    def test_scene_timing_schema_failure_gets_targeted_rewrite_instruction(self):
        invalid_scene = demo()
        invalid_scene["dialogue"] = [line for index, line in enumerate(invalid_scene["dialogue"]) if index != 0]
        invalid_scene["dialogue"][0]["text"] = "I"
        invalid_scene["dialogue"][-1]["text"] += " for once"
        provider = DialogueStoryProvider(CostBudget(self.root / "scene-feedback.json", 0))
        provider._request = Mock(side_effect=[
            {"candidates": demo_candidates()},
            invalid_scene,
            demo(),
            {"scores": {k: 9 for k in QUALITY_METRICS}},
        ])
        result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["story_quality"]["status"], "PASSED")
        rewrite_prompt = provider._request.call_args_list[2].args[0]
        self.assertIn("MANDATORY SCENE-TIMING REPAIR", rewrite_prompt)
        self.assertIn("at least two spoken words", rewrite_prompt)

    def test_character_schema_failure_gets_targeted_rewrite_instruction(self):
        incomplete = demo()
        incomplete["characters"][0].pop("voice_profile_hint")
        provider = DialogueStoryProvider(CostBudget(self.root / "character-feedback.json", 0))
        provider._request = Mock(side_effect=[
            {"candidates": demo_candidates()},
            incomplete,
            demo(),
            {"scores": {k: 9 for k in QUALITY_METRICS}},
        ])
        result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["story_quality"]["status"], "PASSED")
        rewrite_prompt = provider._request.call_args_list[2].args[0]
        self.assertIn("MANDATORY CHARACTER-SCHEMA REPAIR", rewrite_prompt)
        self.assertIn("voice_profile_hint", rewrite_prompt)

    def test_visual_bible_failure_gets_targeted_rewrite_instruction(self):
        incomplete = demo()
        complete = demo()
        for character in incomplete["characters"]:
            character["visual_identity"] = {}
        for character, color in zip(complete["characters"], ("amber", "blue", "silver")):
            character["visual_identity"] = {
                "appearance": "robot",
                "proportions": "round",
                "clothing_accessories": "scarf",
                "colors": color,
                "facial_traits": "oval eyes",
            }
        provider = DialogueStoryProvider(CostBudget(self.root / "visual-bible-feedback.json", 0))
        provider._request = Mock(side_effect=[
            {"candidates": demo_candidates()},
            incomplete,
            complete,
            {"scores": {k: 9 for k in QUALITY_METRICS}},
        ])
        with patch.dict(os.environ, {"STORY_REFERENCE_PROVIDER": "runway", "STORY_VOICE_POOL": '["voiceA", "voiceB", "voiceC"]'}):
            result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["story_quality"]["status"], "PASSED")
        rewrite_prompt = provider._request.call_args_list[2].args[0]
        self.assertIn("MANDATORY VISUAL-BIBLE REPAIR", rewrite_prompt)
        self.assertIn("facial_traits", rewrite_prompt)

    def test_candidate_request_uses_strict_schema_for_supported_openai_model(self):
        payload = {"candidates": demo_candidates()}
        response = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": json.dumps(payload)}}], "usage": {"prompt_tokens": 20, "completion_tokens": 40}})
        session = Mock()
        session.request.return_value = response
        provider = DialogueStoryProvider(CostBudget(self.root / "strict-candidate.json", 1), session=session)
        with patch.dict(os.environ, {"STORY_LLM_API_KEY": "fake-llm-secret", "STORY_LLM_MODEL": "gpt-4.1-mini", "STORY_LLM_BASE_URL": "https://api.openai.com/v1"}):
            result = provider._request("Return candidates as JSON", "candidate_concepts")
        request = session.request.call_args.kwargs["json"]
        self.assertEqual(result, payload)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        schema = request["response_format"]["json_schema"]["schema"]
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertEqual(schema["required"], ["candidates"])
        self.assertEqual(schema["properties"]["candidates"]["items"]["required"], ["concept", "category", "trope", "scores"])
        self.assertEqual(provider.budget.requests[0]["status"], "SUCCEEDED")

    def test_candidate_parser_retries_invalid_shape_and_accepts_fenced_json(self):
        invalid = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "{\"candidates\": []}"}}]})
        valid = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": "```json\n" + json.dumps({"candidates": demo_candidates()}) + "\n```"}}]})
        session = Mock()
        session.request.side_effect = [invalid, valid]
        provider = DialogueStoryProvider(CostBudget(self.root / "candidate-retry.json", 1), session=session)
        with patch.dict(os.environ, {"STORY_LLM_API_KEY": "fake-llm-secret", "STORY_LLM_MODEL": "gpt-4.1-mini", "STORY_LLM_BASE_URL": "https://api.openai.com/v1"}):
            result = provider._request("Return candidates as JSON", "candidate_concepts")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(provider.budget.requests[0]["status"], "INVALID_STRUCTURED_OUTPUT")

    def test_candidate_schema_falls_back_to_json_object_when_proxy_rejects_schema(self):
        rejected = Mock(status_code=400, json=lambda: {"error": {"type": "invalid_request_error"}})
        valid = Mock(status_code=200, json=lambda: {"choices": [{"message": {"content": json.dumps({"candidates": demo_candidates()})}}]})
        session = Mock()
        session.request.side_effect = [rejected, valid]
        provider = DialogueStoryProvider(CostBudget(self.root / "candidate-compat.json", 1), session=session)
        with patch.dict(os.environ, {"STORY_LLM_API_KEY": "fake-llm-secret", "STORY_LLM_MODEL": "gpt-4.1-mini", "STORY_LLM_BASE_URL": "https://api.openai.com/v1"}):
            provider._request("Return candidates as JSON", "candidate_concepts")
        first = session.request.call_args_list[0].kwargs["json"]["response_format"]["type"]
        second = session.request.call_args_list[1].kwargs["json"]["response_format"]["type"]
        self.assertEqual((first, second), ("json_schema", "json_object"))

    def test_separate_quality_critique_triggers_bounded_rewrite(self):
        provider = DialogueStoryProvider(CostBudget(self.root / "cost.json", 0))
        weak = {k: 9 for k in QUALITY_METRICS}
        weak["payoff"] = 3
        provider._request = Mock(side_effect=[{"candidates": demo_candidates()}, demo(), {"scores": weak, "issues": ["Unearned ending"]}, demo(), {"scores": {k: 9 for k in QUALITY_METRICS}}])
        result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["generation"]["script_attempts"], 2)
        self.assertIn("Unearned ending", provider._request.call_args_list[3].args[0])
        self.assertEqual(result["story_quality"]["status"], "PASSED")

    def test_quality_attempt_limit_stops_instead_of_looping(self):
        provider = DialogueStoryProvider(CostBudget(self.root / "cost.json", 0))
        provider._request = Mock(side_effect=[{"candidates": demo_candidates()}] + [x for _ in range(3) for x in (demo(), {"scores": {k: 2 for k in QUALITY_METRICS}})])
        with self.assertRaisesRegex(ProviderFailure, "QUALITY_ATTEMPTS_EXHAUSTED"):
            provider.generate(excluded_concepts=set())
        self.assertEqual(provider.script_attempts, 3)

    def test_llm_budget_stops_before_concept_request(self):
        with patch.dict(os.environ, {"STORY_LLM_API_KEY": "fake", "STORY_LLM_MODEL": "gpt-4.1-mini", "STORY_LLM_BASE_URL": "https://api.openai.com/v1"}), patch("requests.sessions.Session.request") as request:
            with self.assertRaises(BudgetExceeded):
                DialogueStoryProvider(CostBudget(self.root / "cost.json", 0)).generate(excluded_concepts=set())
        request.assert_not_called()

    def test_per_character_mapping_pool_and_narrator(self):
        s = demo()
        with patch.dict(os.environ, {"STORY_VOICE_MAP": '{"mira":"voiceA","ben":"voiceB","lift":"voiceC"}'}):
            self.assertEqual(assign_voices(s, True)["mira"], "voiceA")
        with patch.dict(os.environ, {"STORY_VOICE_POOL": '["voiceC","voiceA","voiceB"]'}):
            self.assertEqual(len(set(assign_voices(s, True).values())), 3)
        with self.assertRaisesRegex(ProviderFailure, "VOICE_MAP_REQUIRED"):
            assign_voices(s, True)
        s["dialogue"].append({"speaker_id": "narrator"})
        with patch.dict(os.environ, {"STORY_VOICE_MAP": '{"mira":"voiceA","ben":"voiceB","lift":"voiceC"}', "ELEVENLABS_VOICE_ID": "narratorVoice"}):
            self.assertEqual(assign_voices(s, True)["narrator"], "narratorVoice")

    def test_development_fallback_without_credentials(self):
        budget = CostBudget(self.root / "cost.json", 0)
        with patch.dict(os.environ, {"STORY_PROVIDER": "production", "VOICE_PROVIDER": "production", "STORY_LLM_API_KEY": "", "ELEVENLABS_API_KEY": ""}), patch("requests.sessions.Session.request") as request:
            s = load_provider("script", DemoStoryProvider, budget=budget).generate(excluded_concepts=set())
            voice = load_provider("voice", DevelopmentVoiceProvider, budget=budget)
        self.assertIn("dialogue", s)
        self.assertIsInstance(voice, DevelopmentVoiceProvider)
        request.assert_not_called()

    def test_optional_narrator_does_not_consume_character_voice_pool(self):
        s = demo()
        s["dialogue"].insert(0, {"speaker_id": "narrator"})
        with patch.dict(os.environ, {"STORY_VOICE_POOL": '["voiceA","voiceB","voiceC"]', "ELEVENLABS_VOICE_ID": "narratorVoice"}):
            mapping = assign_voices(s, True)
        self.assertEqual(mapping["narrator"], "narratorVoice")
        self.assertEqual(len(set(mapping.values())), 4)

    def test_paid_line_voices_alignment_and_single_caption_layer(self):
        s = demo()
        budget = CostBudget(self.root / "cost.json", 1)
        provider = ElevenLabsVoiceProvider(budget, session=Mock())
        last = {}
        def request(*args, **kwargs):
            text = kwargs["json"]["text"]
            duration = len(text.split()) / 2.75
            last["duration"] = duration
            step = duration / len(text)
            return Mock(status_code=200, json=lambda: {"audio_base64": base64.b64encode(b"mock").decode(), "alignment": {"characters": list(text), "character_start_times_seconds": [i*step for i in range(len(text))], "character_end_times_seconds": [(i+1)*step for i in range(len(text))]}})
        def decode(raw, audio):
            with wave.open(str(audio), "wb") as output:
                output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                output.writeframes(b"\x01\x01" * int(last["duration"] * 44100))
        provider.session.request.side_effect = request
        provider._decode = decode
        def normalize(command, **kwargs):
            shutil.copyfile(command[command.index("-i")+1], command[-1])
        with patch.dict(os.environ, {"STORY_VOICE_POOL": '["voiceC","voiceA","voiceB"]'}), patch("story_engine.dialogue_voice.subprocess.run", side_effect=normalize):
            audio, captions, metadata = provider.synthesize(s, self.root / "audio")
        validate_story(s)
        self.assertEqual(evaluate_story(s)["status"], "PASSED")
        self.assertEqual(len(budget.requests), len(s["dialogue"]))
        self.assertEqual({c["speaker_id"] for c in captions}, {"lift", "mira", "ben"})
        self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(captions, captions[1:])))
        self.assertEqual(metadata["tempo_factor"], 1)
        self.assertTrue(audio.exists())
        subtitles = self.root / "captions.ass"
        write_ass(captions, subtitles, font_size=38)
        self.assertEqual(" ".join(" ".join(c["text"] for c in _read_captions(subtitles)).split()), s["full_script"])
        self.assertEqual(subtitles.read_text().count("Dialogue: 0,"), len(captions))
        self.assertNotIn("mock-dialogue-secret", (self.root / "audio/dialogue_timing.json").read_text())

    def _engine(self, story, voice, visual, provider=None):
        source = provider or type("Source", (), {"generate": lambda _, **kwargs: copy.deepcopy(story)})()
        with patch("story_engine.engine.render_story"), patch("story_engine.engine.inspect_story_final", return_value={"status": "QUALITY_CHECK_PASSED"}), patch("story_engine.engine.inspect_story_file", return_value={}):
            return generate_story_video(self.root / "out", self.root / "history.json", story_provider=source, voice_provider=voice, visual_provider=visual)

    def test_failed_quality_never_calls_visual_or_voice(self):
        s, voice, visual = demo(), Mock(), Mock()
        s["story_beats"]["conflict"] = ""
        result = self._engine(s, voice, visual)
        self.assertEqual(result["reason"], "STORY_QUALITY_REJECTED_BEFORE_VISUALS")
        visual.create.assert_not_called()
        voice.synthesize.assert_not_called()

    def test_measured_short_voice_never_calls_visual(self):
        s, voice, visual = demo(), Mock(), Mock()
        def short(story, directory):
            factor = 58 / story["estimated_voice_duration_seconds"]
            apply_timeline(story, [l["estimated_duration_seconds"] * factor for l in story["dialogue"]])
            return self.root / "fake.wav", [], {}
        voice.synthesize.side_effect = short
        self.assertEqual(self._engine(s, voice, visual)["status"], "REVIEW_REQUIRED")
        visual.create.assert_not_called()

    def test_engine_rewrites_then_measures_voice_before_visuals(self):
        s, voice, visual, events = demo(), Mock(), Mock(), []
        weak = copy.deepcopy(s)
        weak["story_beats"]["payoff"] = ""
        source = type("Source", (), {"generate": lambda _, **kwargs: weak, "rewrite": lambda _, story, feedback: copy.deepcopy(s)})()
        def synth(story, directory):
            events.append("voice")
            story["actual_voice_duration_seconds"] = story["estimated_voice_duration_seconds"]
            return self.root / "fake.wav", [], {}
        voice.synthesize.side_effect = synth
        visual.create.side_effect = lambda *args: (events.append("visual") or Mock(provider="test"))
        result = self._engine(s, voice, visual, source)
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(events[0], "voice")
        self.assertEqual(events.count("visual"), len(s["scenes"]))

    def test_legacy_script_cannot_bypass_production_visual_gate(self):
        visual, voice = Mock(), Mock()
        with patch.dict(os.environ, {"VISUAL_PROVIDER": "production"}):
            result = self._engine(DemoStoryProvider().generate(excluded_concepts=set()), voice, visual)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        visual.create.assert_not_called()

    def test_injected_paid_adapter_cannot_bypass_quality_gate(self):
        from story_engine.runway_provider import RunwayVisualProvider
        visual = RunwayVisualProvider(CostBudget(self.root / "cost.json", 5))
        with patch.object(visual, "create") as create:
            result = self._engine(DemoStoryProvider().generate(excluded_concepts=set()), Mock(), visual)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        create.assert_not_called()

    def test_failed_production_writer_is_not_reentered_after_fallback(self):
        from story_engine.provider_selection import FallbackProvider
        production = Mock()
        wrapper = FallbackProvider(production, DialogueDemoProvider(), CostBudget(self.root / "cost.json", 0), "script")
        wrapper.failed = True
        with self.assertRaisesRegex(ProviderFailure, "UNAVAILABLE_FOR_REWRITE"):
            wrapper.rewrite(demo(), "Measured duration needs revision")
        production.rewrite.assert_not_called()
