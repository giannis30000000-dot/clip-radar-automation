import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

from media_processor import render_story
from media_tools import ffmpeg_binary, ffprobe_binary, media_summary
from story_engine.costs import BudgetExceeded, CostBudget
from story_engine.dialogue_demo import DialogueDemoProvider
from story_engine.engine import generate_story_video
from story_engine.production_review import plan_visuals, prepare_references, production_preflight
from story_engine.provider_http import ProviderFailure
from story_engine.providers import VisualAsset, load_provider
from story_engine.reference_images import NO_TEXT, RunwayReferenceImageProvider, build_visual_bible, image_input, reference_prompt, utf16_length
from story_engine.runway_provider import RunwayVisualProvider, scene_prompt
from story_engine.voice import DevelopmentVoiceProvider


def fixture():
    story = DialogueDemoProvider().generate(excluded_concepts=set())
    for c, color in zip(story["characters"], ("amber", "blue", "silver")):
        c["visual_identity"] = {"appearance": "robot", "proportions": "round", "clothing_accessories": "scarf", "colors": color, "facial_traits": "oval eyes"}
    story["art_direction"] = {"style": "soft clay comedy", "environment": "wooden elevator"}
    story["visual_bible"] = build_visual_bible(story)
    story["actual_voice_duration_seconds"] = story["estimated_voice_duration_seconds"]
    return story


def response(data):
    return Mock(status_code=200, json=lambda: data)


class ReferenceImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"RUNWAYML_API_SECRET": "fake-reference-secret", "RUNWAY_MAX_POLLS": "2", "STORY_REQUIRE_PRODUCTION": "", "STORY_REFERENCE_PROVIDER": "", "STORY_PROVIDER": "development", "VOICE_PROVIDER": "development", "VISUAL_PROVIDER": "development", "STORY_MIN_DURATION_SECONDS": "65", "RUNWAY_USD_PER_SECOND": ".08", "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "10"})
        env.start()
        self.addCleanup(env.stop)

    def budget(self, limit=10):
        return CostBudget(self.root / "cost.json", limit)

    def picture(self):
        path = self.root / "cast.png"
        Image.new("RGB", (720, 1280), "navy").save(path)
        return path

    def test_exact_bible_and_no_text_constraints_propagate_to_all_prompts(self):
        story = fixture()
        bible = story["visual_bible"]["prompt_text"]
        for scene in [None, *story["scenes"]]:
            prompt = reference_prompt(story, scene)
            self.assertIn(bible, prompt)
            self.assertIn(NO_TEXT, prompt)
            self.assertLessEqual(utf16_length(prompt), 1000)
            if scene:
                video = scene_prompt(story, scene)
                self.assertIn(bible, video)
                self.assertIn(NO_TEXT, video)
                self.assertIn("no mouth closeups", video)
                self.assertIn(scene["action_direction"], video)

    def test_incomplete_or_oversize_bible_rejected_not_silently_truncated(self):
        for identity in ({}, {k: "appearance " * 100 for k in fixture()["characters"][0]["visual_identity"]}):
            story = fixture()
            story["characters"][0]["visual_identity"] = identity
            with self.assertRaisesRegex(ValueError, "VISUAL_BIBLE"):
                build_visual_bible(story)

    def test_image_generation_download_validation_and_cost_without_secret_or_base64_logs(self):
        session, downloads = Mock(), Mock()
        session.request.side_effect = [response({"id": "image-task"}), response({"status": "SUCCEEDED", "output": ["https://media.example/ref.png?signed=private"]})]
        stream = Mock(status_code=200)
        stream.iter_content.return_value = [self.picture().read_bytes()]
        downloads.get.return_value.__enter__ = Mock(return_value=stream)
        downloads.get.return_value.__exit__ = Mock(return_value=False)
        budget = self.budget()
        provider = RunwayReferenceImageProvider(budget, session, downloads, sleep=lambda _: None)
        asset = provider.create(fixture(), fixture()["scenes"][0], self.root / "references", cast_reference=self.picture())
        self.assertTrue(asset.path.exists())
        self.assertEqual(asset.provider, "runway-reference")
        payload = session.request.call_args_list[0].kwargs["json"]
        self.assertTrue(payload["referenceImages"][0]["uri"].startswith("data:image/png;base64,"))
        self.assertEqual(budget.report()["total_estimated_cost_usd"], .05)
        self.assertEqual(budget.requests[0]["generated_assets"], 1)
        metadata = (self.root / "references/scene_01.provider.json").read_text()
        for forbidden in ("fake-reference-secret", "data:image", "signed=private"):
            self.assertNotIn(forbidden, metadata)
        self.assertNotIn("headers", downloads.get.call_args.kwargs)

    def test_image_budget_blocks_before_network_and_timeout_is_not_resubmitted(self):
        session = Mock()
        with self.assertRaises(BudgetExceeded):
            RunwayReferenceImageProvider(self.budget(0), session).create(fixture(), None, self.root)
        session.request.assert_not_called()
        session.request.side_effect = requests.Timeout("fake-reference-secret")
        budget = self.budget()
        with self.assertRaisesRegex(ProviderFailure, "NETWORK_RESULT_UNKNOWN"):
            RunwayReferenceImageProvider(budget, session).create(fixture(), None, self.root)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(len(budget.requests), 1)

    def test_pending_image_task_preserves_id_without_resubmission(self):
        session = Mock()
        session.request.side_effect = [response({"id": "pending-image"})] + [response({"status": "RUNNING"})] * 2
        with self.assertRaisesRegex(ProviderFailure, "TIMEOUT_RESULT_UNKNOWN"):
            RunwayReferenceImageProvider(self.budget(), session, sleep=lambda _: None).create(fixture(), None, self.root)
        report = json.loads((self.root / "cast.provider.json").read_text())
        self.assertEqual(report["task_id"], "pending-image")
        self.assertEqual(sum(c.args[0] == "POST" for c in session.request.call_args_list), 1)

    def test_h3_accepts_generated_local_first_frame_with_compact_audit_record(self):
        story = fixture()
        scene = story["scenes"][0]
        scene["reference_image_path"] = str(self.picture())
        session = Mock()
        session.request.side_effect = [response({"id": "h3-task"}), response({"status": "SUCCEEDED", "output": ["https://media.example/clip.mp4"]})]
        budget = self.budget()
        provider = RunwayVisualProvider(budget, session=session, model="h3_max", max_attempts=1, sleep=lambda _: None)
        with patch.object(provider, "_download"), patch("story_engine.runway_provider.media_summary", return_value={"has_video": True, "duration": 10, "width": 768, "height": 1366, "size": 1000}):
            asset = provider.create(story, scene, self.root / "scenes")
        payload = session.request.call_args_list[0].kwargs["json"]
        self.assertTrue(payload["promptImage"].startswith("data:image/png;base64,"))
        self.assertEqual(payload["resolution"], "768p")
        self.assertNotIn("ratio", payload)
        self.assertEqual(asset.provider, "runway")
        report = json.loads((self.root / "scenes/scene_01.provider.json").read_text())
        self.assertEqual(report["bible_sha256"], story["visual_bible"]["sha256"])
        self.assertNotIn("data:image", json.dumps(report))

    def test_bad_first_frame_stops_h3_before_reservation(self):
        bad = self.root / "bad.png"
        bad.write_bytes(b"not an image")
        story, session, budget = fixture(), Mock(), self.budget()
        story["scenes"][0]["reference_image_path"] = str(bad)
        with self.assertRaisesRegex(ProviderFailure, "INVALID_REFERENCE_IMAGE"):
            RunwayVisualProvider(budget, session=session, model="h3_max").create(story, story["scenes"][0], self.root)
        self.assertEqual(budget.requests, [])
        session.request.assert_not_called()

    def test_plan_preserves_all_story_beats_and_uses_images_under_tight_budget(self):
        story = fixture()
        original = copy.deepcopy(story)
        budget = self.budget(2.5)
        budget.reserve("voice", "mock", ".2")
        plan = plan_visuals(story, budget)
        self.assertEqual(plan["reference_images"], len(story["scenes"]) + 1)
        self.assertEqual(plan["scenes"][0]["kind"], "video")
        self.assertEqual(plan["scenes"][-1]["kind"], "video")
        self.assertIn("image", [s["kind"] for s in plan["scenes"]])
        self.assertLessEqual(plan["total_remaining_visual_estimated_usd"] + .2, 2.5)
        self.assertEqual(story, original)
        with self.assertRaises(BudgetExceeded):
            plan_visuals(story, self.budget(.5))

    def test_prepare_creates_one_shared_cast_and_each_h3_reference(self):
        story, provider = fixture(), Mock()
        provider.create.return_value = VisualAsset(self.picture(), "image", "runway-reference")
        assets = prepare_references(story, self.budget(), self.root, provider)
        self.assertEqual(provider.create.call_count, len(story["scenes"]) + 1)
        self.assertEqual(len(assets), len(story["scenes"]))
        for scene, call in zip(story["scenes"], provider.create.call_args_list[1:]):
            self.assertEqual(call.kwargs["cast_reference"], self.picture())
            self.assertTrue(Path(scene["reference_image_path"]).exists())

    def test_quality_and_measured_timing_gate_reference_calls(self):
        for failure in ("quality", "timing", "unmeasured"):
            story, provider = fixture(), Mock()
            if failure == "quality":
                story["story_beats"]["conflict"] = ""
            elif failure == "timing":
                story["actual_voice_duration_seconds"] = 59
            else:
                del story["actual_voice_duration_seconds"]
            with self.assertRaisesRegex(ProviderFailure, "BEFORE_REFERENCES"):
                prepare_references(story, self.budget(), self.root, provider)
            provider.create.assert_not_called()

    def test_engine_blocks_images_when_script_or_measured_voice_fails(self):
        for failure in ("script", "voice"):
            story, voice = fixture(), Mock()
            if failure == "script":
                story["story_beats"]["conflict"] = ""
            def synth(story, directory):
                story["actual_voice_duration_seconds"] = 59
                return self.root / "fake.wav", [], {}
            voice.synthesize.side_effect = synth
            source = type("Source", (), {"generate": lambda _, **kw: story})()
            with patch.dict(os.environ, {"STORY_REFERENCE_PROVIDER": "runway"}), patch("story_engine.engine.prepare_references") as prepare:
                result = generate_story_video(self.root / failure, self.root / (failure + ".json"), story_provider=source, voice_provider=voice)
            self.assertEqual(result["status"], "REVIEW_REQUIRED")
            prepare.assert_not_called()

    def test_strict_mode_missing_credentials_stops_before_any_paid_call(self):
        with patch.dict(os.environ, {"STORY_REQUIRE_PRODUCTION": "true", "STORY_LLM_API_KEY": "", "ELEVENLABS_API_KEY": "", "STORY_VOICE_POOL": ""}), patch("requests.sessions.Session.request") as request:
            result = generate_story_video(self.root / "out", self.root / "history.json")
        self.assertEqual(result["status"], "CONFIGURATION_REQUIRED")
        self.assertFalse(result["live_request_sent"])
        request.assert_not_called()
        self.assertFalse((self.root / "history.json").exists())

    def test_strict_engine_completes_mocked_production_in_gate_reference_motion_order(self):
        story, source, voice, visual, references = fixture(), Mock(), Mock(), Mock(), Mock()
        source.generate.return_value = story
        events = []
        def synth(story, directory):
            events.append("voice")
            return self.root / "fake.wav", [], {"provider": "elevenlabs-dialogue", "development_only": False}
        voice.synthesize.side_effect = synth
        references.create.side_effect = lambda *args, **kwargs: (events.append("reference") or VisualAsset(self.picture(), "image", "runway-reference"))
        visual.create.side_effect = lambda *args: (events.append("motion") or VisualAsset(self.root / "fake.mp4", "video", "runway"))
        settings = {"STORY_REQUIRE_PRODUCTION": "true", "STORY_REFERENCE_PROVIDER": "runway", "RUNWAY_MODEL": "h3_max", "STORY_PROVIDER": "production", "VOICE_PROVIDER": "production", "VISUAL_PROVIDER": "production", "STORY_LLM_API_KEY": "fake", "ELEVENLABS_API_KEY": "fake", "STORY_VOICE_POOL": '["v1","v2","v3"]', "STORY_VOICE_MAP": ""}
        with patch.dict(os.environ, settings), patch("story_engine.production_review.RunwayReferenceImageProvider", return_value=references), patch("story_engine.engine.render_story"), patch("story_engine.engine.inspect_story_final", return_value={"status": "QUALITY_CHECK_PASSED"}), patch("story_engine.engine.inspect_story_file", return_value={}):
            result = generate_story_video(self.root / "out", self.root / "history.json", story_provider=source, voice_provider=voice, visual_provider=visual)
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(events, ["voice"] + ["reference"] * 11 + ["motion"] * 10)
        self.assertFalse(result["publishing_enabled"])
        self.assertFalse(result["live_request_sent"])

    def test_strict_mode_disallows_missing_voice_fallback_and_cap_above_ten(self):
        with patch.dict(os.environ, {"STORY_REQUIRE_PRODUCTION": "true", "VOICE_PROVIDER": "production", "ELEVENLABS_API_KEY": "", "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "10.01"}):
            with self.assertRaisesRegex(ProviderFailure, "CONFIGURATION_REQUIRED"):
                load_provider("voice", DevelopmentVoiceProvider, budget=self.budget())
            self.assertIn("An explicit positive per-video cost cap no greater than $10 is required", production_preflight()["errors"])

    def test_real_assembly_discards_generated_audio_and_uses_master_only(self):
        import numpy as np
        try:
            ffmpeg, _ = ffmpeg_binary(), ffprobe_binary()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        clip, voice, final = self.root / "clip.mp4", self.root / "voice.wav", self.root / "final.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=180x320:r=30:d=1", "-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True, capture_output=True)
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(voice)], check=True, capture_output=True)
        story = fixture()
        story["scenes"] = story["scenes"][:1]
        story["scenes"][0].update(start_time=0, estimated_duration=1)
        story["actual_voice_duration_seconds"] = 1
        manifest = render_story(story, [VisualAsset(clip, "video", "runway")], voice, [{"start": 0, "end": .9, "text": "Master voice only"}], final)
        decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(final), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], check=True, capture_output=True).stdout
        signal = np.frombuffer(decoded, dtype=np.float32)[:16000]
        spectrum = abs(np.fft.rfft(signal))
        frequencies = np.fft.rfftfreq(len(signal), 1 / 16000)
        energy = lambda hz: max(spectrum[abs(frequencies - hz) < 5])
        self.assertGreater(energy(220), energy(880) * 50)
        self.assertFalse(manifest["lip_sync"])
        self.assertTrue(media_summary(final)["has_audio"])
