import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from story_engine.costs import BudgetExceeded, CostBudget, sanitize
from story_engine.engine import generate_story_video
from story_engine.history import StoryHistory
from story_engine.llm_provider import ChatStoryProvider
from story_engine.runway_provider import RunwayVisualProvider, scene_prompt
from story_engine.elevenlabs_provider import ElevenLabsVoiceProvider, aligned_captions
from story_engine.providers import DemoStoryProvider, VisualAsset, load_provider
from story_engine.provider_selection import FallbackProvider
from story_engine.provider_http import ProviderFailure
from story_engine.visuals import DevelopmentVisualProvider
from story_engine.voice import DevelopmentVoiceProvider
from story_engine.schema import validate_story


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


def story():
    return DemoStoryProvider().generate(excluded_concepts=set())


def llm_response(data):
    return Response({"choices": [{"message": {"content": data if isinstance(data, str) else json.dumps(data)}}], "usage": {"prompt_tokens": 1100, "completion_tokens": 2500}})


def alignment(text, duration=68):
    scale = (duration - .1) / len(text)
    return {"characters": list(text), "character_start_times_seconds": [i*scale for i in range(len(text))], "character_end_times_seconds": [(i+1)*scale for i in range(len(text))]}


class ProductionProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {
            "STORY_PROVIDER": "", "VISUAL_PROVIDER": "", "VOICE_PROVIDER": "",
            "STORY_SCRIPT_PROVIDER": "demo", "STORY_VISUAL_PROVIDER": "demo", "STORY_VOICE_PROVIDER": "demo",
            "STORY_LLM_API_KEY": "fake-llm-secret", "RUNWAYML_API_SECRET": "fake-runway-secret",
            "ELEVENLABS_API_KEY": "fake-voice-secret", "ELEVENLABS_VOICE_ID": "test_voice",
            "STORY_PROVIDER_MAX_ATTEMPTS": "2", "RUNWAY_MAX_POLLS": "3",
            "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "10", "STORY_LLM_MODEL": "gpt-4.1-mini",
            "STORY_LLM_BASE_URL": "https://api.openai.com/v1",
        })
        self.env.start()
        self.sleep = patch("time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()
        self.temporary.cleanup()

    def budget(self, limit=10):
        return CostBudget(self.root / "cost.json", limit=limit)

    def test_default_never_selects_paid_even_with_keys(self):
        self.assertIsInstance(load_provider("script", DemoStoryProvider), DemoStoryProvider)
        self.assertIsInstance(load_provider("visual", DevelopmentVisualProvider), DevelopmentVisualProvider)
        self.assertIsInstance(load_provider("voice", DevelopmentVoiceProvider), DevelopmentVoiceProvider)

    def test_production_selection_and_shared_budget(self):
        budget = self.budget()
        with patch.dict(os.environ, {"STORY_PROVIDER": "production", "VISUAL_PROVIDER": "production", "VOICE_PROVIDER": "production"}):
            for kind, default, expected in [("script", DemoStoryProvider, ChatStoryProvider), ("visual", DevelopmentVisualProvider, RunwayVisualProvider), ("voice", DevelopmentVoiceProvider, ElevenLabsVoiceProvider)]:
                provider = load_provider(kind, default, budget=budget)
                self.assertIsInstance(provider, FallbackProvider)
                self.assertIsInstance(provider.production, expected)
                self.assertIs(provider.production.budget, budget)

    def test_absent_keys_fall_back_without_network(self):
        budget = self.budget()
        with patch.dict(os.environ, {"STORY_PROVIDER": "production", "VISUAL_PROVIDER": "production", "VOICE_PROVIDER": "production", "STORY_LLM_API_KEY": "", "RUNWAYML_API_SECRET": "", "ELEVENLABS_API_KEY": ""}), patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
            for kind, default in [("script", DemoStoryProvider), ("visual", DevelopmentVisualProvider), ("voice", DevelopmentVoiceProvider)]:
                self.assertIsInstance(load_provider(kind, default, budget=budget), default)
        self.assertEqual(budget.report()["total_estimated_cost_usd"], 0)
        self.assertEqual(len(budget.events), 3)

    def test_llm_retries_malformed_json_and_records_usage(self):
        session = Mock()
        session.request.side_effect = [llm_response("{broken"), llm_response(story())]
        budget = self.budget()
        result = ChatStoryProvider(budget, session=session).generate(excluded_concepts=set())
        validate_story(result)
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(budget.requests[0]["status"], "INVALID_OR_DUPLICATE_STORY")
        self.assertEqual(budget.requests[1]["retry"], 1)
        self.assertGreater(budget.requests[1]["usage_estimated_usd"], 0)
        self.assertTrue(result["story_id"].startswith("story-"))
        self.assertNotIn("fake-llm-secret", (self.root / "cost.json").read_text())

    def test_llm_retries_schema_invalid_and_near_duplicate_premises(self):
        history = StoryHistory(self.root / "history.json")
        history.reserve(story(), self.root / "old.mp4")
        duplicate = story()
        duplicate["concept"] += " Suddenly."
        fresh = DemoStoryProvider().generate(excluded_concepts=set(), template="moon-landlord")
        session = Mock()
        session.request.side_effect = [llm_response(duplicate), llm_response(fresh)]
        result = ChatStoryProvider(self.budget(), history, session).generate(excluded_concepts=history.fingerprints())
        self.assertEqual(result["concept"], fresh["concept"])
        self.assertEqual(session.request.call_count, 2)

    def test_llm_invalid_schema_falls_back_after_bounded_attempts(self):
        session = Mock()
        session.request.return_value = llm_response({"title": "incomplete"})
        budget = self.budget()
        provider = FallbackProvider(ChatStoryProvider(budget, session=session), DemoStoryProvider(), budget, "script")
        result = provider.generate(excluded_concepts=set())
        self.assertEqual(result["generation"]["provider"], "authored-demo-templates")
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(budget.events[-1]["reason"], "DEVELOPMENT_FALLBACK")

    def test_unknown_model_needs_explicit_prices_before_network(self):
        session = Mock()
        with patch.dict(os.environ, {"STORY_LLM_MODEL": "custom-model", "STORY_LLM_INPUT_USD_PER_MILLION": "", "STORY_LLM_OUTPUT_USD_PER_MILLION": ""}):
            with self.assertRaisesRegex(ProviderFailure, "PRICE_CONFIGURATION_REQUIRED"):
                ChatStoryProvider(self.budget(), session=session).generate(excluded_concepts=set())
        session.request.assert_not_called()

    def test_non_object_and_wrongly_typed_responses_fall_back(self):
        malformed = story()
        malformed["platform_metadata"] = []
        for response in (Response([]), llm_response(malformed)):
            with self.subTest(response=response.payload):
                session = Mock()
                session.request.return_value = response
                budget = self.budget()
                provider = FallbackProvider(ChatStoryProvider(budget, session=session), DemoStoryProvider(), budget, "script")
                result = provider.generate(excluded_concepts=set())
                self.assertEqual(result["generation"]["provider"], "authored-demo-templates")
                self.assertEqual(session.request.call_count, 2)
                self.assertEqual(len(budget.requests), 2)

    def test_budget_reserves_exact_decimal_and_counts_failed_retries(self):
        budget = self.budget(".30")
        a = budget.reserve("visual", "model", ".10", requested_seconds=2)
        budget.update(a, status="FAILED")
        budget.reserve("visual", "model", ".20", retry=1, requested_seconds=4)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("visual", "model", ".01", retry=2)
        report = budget.report()
        self.assertEqual(report["total_estimated_cost_usd"], .3)
        self.assertEqual(report["providers"][0]["requests"], 2)
        self.assertEqual(report["providers"][0]["retries"], 1)
        self.assertEqual(report["providers"][0]["requested_seconds"], 6)
        self.assertIsNone(report["actual_cost_usd"])

    def test_budget_stop_never_becomes_fallback_or_paid_request(self):
        session = Mock()
        budget = self.budget(0)
        provider = FallbackProvider(ChatStoryProvider(budget, session=session), Mock(), budget, "script")
        with self.assertRaises(BudgetExceeded):
            provider.generate(excluded_concepts=set())
        session.request.assert_not_called()
        provider.development.generate.assert_not_called()
        self.assertEqual(budget.report()["status"], "BUDGET_EXCEEDED")

    def test_engine_reports_budget_stop_cleanly(self):
        with patch.dict(os.environ, {"STORY_PROVIDER": "production", "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "0"}), patch("requests.sessions.Session.request") as request:
            result = generate_story_video(self.root / "out", self.root / "history.json")
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertFalse(result["publishing_enabled"])
        request.assert_not_called()
        self.assertTrue(Path(result["cost_report"]).exists())

    def test_runway_retries_terminal_failure_then_downloads_once(self):
        session = Mock()
        session.request.side_effect = [
            Response({"id": "first"}), Response({"status": "FAILED"}),
            Response({"id": "second"}), Response({"status": "RUNNING"}),
            Response({"status": "SUCCEEDED", "output": ["https://media.example/scene.mp4?secret=signed"]}),
        ]
        budget = self.budget()
        provider = RunwayVisualProvider(budget, session=session, sleep=lambda _: None)
        with patch.object(provider, "_download"), patch("story_engine.runway_provider.media_summary", return_value={"has_video": True, "duration": 6, "width": 720, "height": 1280, "size": 4000}):
            output = provider.create(story(), story()["scenes"][0], self.root / "scenes")
        self.assertEqual(output.kind, "video")
        self.assertEqual(len(budget.requests), 2)
        self.assertEqual(budget.requests[1]["generated_assets"], 1)
        posts = [call for call in session.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].kwargs["json"]["ratio"], "720:1280")
        self.assertLessEqual(posts[0].kwargs["json"]["duration"], 10)
        metadata = (self.root / "scenes/scene_01.provider.json").read_text()
        self.assertNotIn("fake-runway-secret", metadata)
        self.assertNotIn("signed", metadata)

    def test_runway_retry_cannot_exceed_budget(self):
        session = Mock()
        session.request.side_effect = [Response({"id": "first"}), Response({"status": "FAILED"})]
        budget = self.budget(".72")
        provider = RunwayVisualProvider(budget, session=session, sleep=lambda _: None)
        with self.assertRaises(BudgetExceeded):
            provider.create(story(), story()["scenes"][0], self.root / "scenes")
        self.assertEqual(len(budget.requests), 1)
        self.assertEqual(session.request.call_count, 2)

    def test_runway_uncertain_submission_never_reposts(self):
        session = Mock()
        session.request.side_effect = requests.Timeout("includes fake-runway-secret")
        budget = self.budget()
        provider = RunwayVisualProvider(budget, session=session, sleep=lambda _: None)
        with self.assertRaisesRegex(ProviderFailure, "NETWORK_RESULT_UNKNOWN"):
            provider.create(story(), story()["scenes"][0], self.root / "scenes")
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(len(budget.requests), 1)
        self.assertNotIn("fake-runway-secret", (self.root / "cost.json").read_text())

    def test_runway_polling_timeout_does_not_generate_another_clip(self):
        session = Mock()
        session.request.side_effect = [Response({"id": "pending"})] + [Response({"status": "RUNNING"})] * 3
        provider = RunwayVisualProvider(self.budget(), session=session, sleep=lambda _: None)
        with self.assertRaisesRegex(ProviderFailure, "TASK_TIMEOUT"):
            provider.create(story(), story()["scenes"][0], self.root / "scenes")
        self.assertEqual(len([c for c in session.request.call_args_list if c.args[0] == "POST"]), 1)

    def test_visual_fallback_trips_circuit_for_remaining_scenes(self):
        paid, free, budget = Mock(), Mock(), self.budget()
        paid.create.side_effect = ProviderFailure("HTTP_401")
        fallback = FallbackProvider(paid, free, budget, "visual")
        fallback.create(story(), story()["scenes"][0], self.root)
        fallback.create(story(), story()["scenes"][1], self.root)
        self.assertEqual(paid.create.call_count, 1)
        self.assertEqual(free.create.call_count, 2)

    def test_continuity_prompt_carries_cast_style_and_previous_scene(self):
        s = story()
        s["art_direction"]["style"] = "clay animation"
        prompt = scene_prompt(s, s["scenes"][1])
        self.assertIn("Toast:", prompt)
        self.assertIn("clay animation", prompt)
        self.assertIn("Previously:", prompt)
        self.assertLessEqual(len(prompt.encode("utf-16-le")), 2000)

    def test_voice_uses_provider_alignment_and_records_cost(self):
        s = story()
        session = Mock()
        session.request.return_value = Response({"audio_base64": base64.b64encode(b"mock-audio").decode(), "alignment": alignment(s["full_script"])})
        budget = self.budget()
        provider = ElevenLabsVoiceProvider(budget, session=session)
        with patch.object(provider, "_decode"), patch("story_engine.elevenlabs_provider.wav_duration", return_value=68):
            audio, captions, metadata = provider.synthesize(s, self.root / "audio")
        validate_story(s)
        self.assertEqual(metadata["timing_basis"], "provider_character_alignment")
        self.assertEqual(" ".join(c["text"] for c in captions), s["full_script"])
        self.assertEqual(s["actual_voice_duration_seconds"], 68)
        self.assertEqual(budget.requests[0]["characters"], len(s["full_script"]))
        self.assertFalse(metadata["development_only"])
        self.assertNotIn("fake-voice-secret", (self.root / "audio/voice.provider.json").read_text())

    def test_bad_voice_alignment_retries_then_falls_back(self):
        session = Mock()
        session.request.return_value = Response({"audio_base64": base64.b64encode(b"mock-audio").decode(), "alignment": alignment("wrong text")})
        budget, free = self.budget(), Mock()
        provider = ElevenLabsVoiceProvider(budget, session=session)
        wrapper = FallbackProvider(provider, free, budget, "voice")
        with patch.object(provider, "_decode"), patch("story_engine.elevenlabs_provider.wav_duration", return_value=68):
            wrapper.synthesize(story(), self.root / "audio")
        self.assertEqual(session.request.call_count, 2)
        free.synthesize.assert_called_once()
        self.assertEqual(budget.events[-1]["reason"], "DEVELOPMENT_FALLBACK")

    def test_alignment_rejects_nan_and_out_of_order_timing(self):
        s = story()
        bad = alignment(s["full_script"])
        bad["character_start_times_seconds"][5] = float("nan")
        with self.assertRaises(ValueError):
            aligned_captions(s, bad, 68)

    def test_secret_redaction_in_all_report_fields(self):
        value = sanitize({"api_key": "hidden", "nested": [{"error": "fake-llm-secret fake-runway-secret fake-voice-secret Bearer unknown-token https://host/file?sig=secret"}]})
        serialized = json.dumps(value)
        for secret in ("hidden", "fake-llm-secret", "fake-runway-secret", "fake-voice-secret", "unknown-token", "sig=secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("<redacted>", serialized)

    def test_real_video_asset_assembly_branding_and_timing_metadata(self):
        import subprocess
        from media_processor import render_story
        from media_tools import ffmpeg_binary, ffprobe_binary, media_summary
        try:
            ffmpeg_binary()
            ffprobe_binary()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        clip, audio = self.root / "scene.mp4", self.root / "voice.wav"
        subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:r=30", "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True)
        subprocess.run([ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100", "-t", "2", str(audio)], check=True, capture_output=True)
        s = story()
        s["scenes"] = s["scenes"][:1]
        s["scenes"][0].update(start_time=0, estimated_duration=2)
        s["actual_voice_duration_seconds"] = 2
        s["generation"]["voice"] = {"timing_basis": "provider_character_alignment"}
        output = self.root / "final.mp4"
        manifest = render_story(s, [VisualAsset(clip, "video", "runway-mock")], audio, [{"start": 0, "end": 1.9, "text": "Original story"}], output)
        media = media_summary(output)
        self.assertEqual((media["width"], media["height"]), (720, 1280))
        self.assertAlmostEqual(media["duration"], 2, places=1)
        self.assertTrue(media["has_audio"])
        self.assertEqual(manifest["subtitles"]["timing_basis"], "provider_character_alignment")
        self.assertEqual(manifest["branding"], "subtle_scene_overlay")
        self.assertFalse(manifest["publishing_enabled"])
        subprocess.run([ffmpeg_binary(), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"], check=True, capture_output=True)
