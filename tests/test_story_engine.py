import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_processor import render_story, write_ass, _wrap_caption
from media_tools import ffmpeg_binary, ffprobe_binary, media_summary
from quality_control import _read_captions, inspect_story_final
from story_engine import generate_story_video
from story_engine.history import DuplicatePremise, HistoryBusy, StoryHistory
from story_engine.providers import DemoStoryProvider, NoNewPremise, load_provider
from story_engine.schema import StoryValidationError, concept_fingerprint, estimate_duration, validate_story
from story_engine.templates import TEMPLATES
from story_engine.visuals import DevelopmentVisualProvider
from story_engine.voice import caption_phrases


class StoryTests(unittest.TestCase):
    def story(self):
        return DemoStoryProvider().generate(excluded_concepts=set())

    def test_all_templates_have_valid_complete_scripts_and_timing(self):
        for template in TEMPLATES:
            story = DemoStoryProvider().generate(excluded_concepts=set(), template=template["key"])
            self.assertEqual(validate_story(story), story)
            self.assertAlmostEqual(sum(s["estimated_duration"] for s in story["scenes"]), story["estimated_voice_duration_seconds"], places=2)
            self.assertTrue(60 <= estimate_duration(story["full_script"]) <= 75)

    def test_schema_rejects_missing_metadata_bad_timing_and_traversal(self):
        for mutate in [
            lambda s: s.pop("platform_metadata"),
            lambda s: s.update(story_id="../escape"),
            lambda s: s["scenes"][2].update(start_time=0),
            lambda s: s["scenes"][2].update(estimated_duration=float("nan")),
            lambda s: s["scenes"][2].update(narration=""),
            lambda s: s["scenes"][2].update(characters_present=["unknown"]),
            lambda s: s.update(full_script="not the scene text"),
        ]:
            story = self.story()
            mutate(story)
            with self.assertRaises(StoryValidationError):
                validate_story(story)

    def test_normalized_and_near_duplicate_premises_are_rejected(self):
        story = self.story()
        self.assertEqual(concept_fingerprint(story["concept"]), concept_fingerprint(story["concept"].upper() + "!!!"))
        with tempfile.TemporaryDirectory() as directory:
            history = StoryHistory(Path(directory) / "history.json")
            with history.locked():
                history.reserve(story, Path(directory) / "first.mp4")
                duplicate = copy.deepcopy(story)
                duplicate["story_id"] = "different-id"
                duplicate["concept"] += " Suddenly."
                with self.assertRaises(DuplicatePremise):
                    history.reserve(duplicate, Path(directory) / "second.mp4")
            restored = StoryHistory(history.path)
            self.assertIn(concept_fingerprint(story["concept"]), restored.fingerprints())

    def test_catalogue_exhaustion_is_expected_and_not_a_render_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = StoryHistory(root / "history.json")
            for template in TEMPLATES:
                history.reserve(DemoStoryProvider().generate(excluded_concepts=set(), template=template["key"]), root / template["key"])
            with patch("story_engine.engine.render_story") as render:
                result = generate_story_video(root / "output", history.path)
            self.assertEqual(result["status"], "NO_NEW_PREMISE")
            render.assert_not_called()

    def test_lock_prevents_overlapping_generation_and_preserves_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = StoryHistory(root / "history.json")
            with history.locked():
                result = generate_story_video(root / "output", history.path)
            self.assertEqual(result["status"], "BUSY")
            self.assertFalse((root / "output/run_summary.json").exists())

    def test_corrupt_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text('{"version": 2, "stories": []}')
            with self.assertRaises(ValueError):
                StoryHistory(path).read()

    def test_captions_preserve_every_word_without_truncation(self):
        for template in TEMPLATES:
            for narration, _, _ in template["beats"]:
                phrases = caption_phrases(narration)
                self.assertEqual(" ".join(phrases), narration)
                for phrase in phrases:
                    wrapped = _wrap_caption(phrase)
                    self.assertNotIn("…", wrapped)
                    self.assertEqual(" ".join(wrapped.split()), phrase)
                    self.assertLessEqual(max(map(len, wrapped.splitlines())), 24)

    def test_caption_round_trip_and_no_ass_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            entries = [{"text": "My toaster hired", "start": 0, "end": 1.5}, {"text": "a lawyer.", "start": 1.55, "end": 2.4}]
            write_ass(entries, path, font_size=38)
            self.assertEqual(_read_captions(path), entries)
            self.assertIn("ClipRadar,Arial,38,", path.read_text())

    def test_provider_factory_and_default_mode(self):
        import orchestrator
        with patch.dict(os.environ, {"STORY_SCRIPT_PROVIDER": "story_engine.providers:DemoStoryProvider", "CLIP_RADAR_MODE": "ORIGINAL_STORY_MODE"}):
            self.assertIsInstance(load_provider("script", lambda: None), DemoStoryProvider)
            with patch("orchestrator.generate_story_video", return_value={"status": "READY_FOR_REVIEW"}) as generate, patch("orchestrator.run_live") as twitch:
                orchestrator.run_configured()
                generate.assert_called_once()
                twitch.assert_not_called()

    def test_failed_voice_persists_history_without_publication_even_if_env_enabled(self):
        class BrokenVoice:
            def synthesize(self, *args):
                raise RuntimeError("intentional TTS failure")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"PUBLISHING_ENABLED": "true", "PUBLISHING_DRY_RUN": "true"}), patch("buffer_publisher.BufferPublisher.publish") as buffer, patch("metricool_publisher.MetricoolPublisher.publish") as metricool, patch("cloudinary_media_host.CloudinaryMediaHost.upload_final") as upload:
                with self.assertRaisesRegex(RuntimeError, "intentional"):
                    generate_story_video(root / "output", root / "history.json", voice_provider=BrokenVoice())
                buffer.assert_not_called()
                metricool.assert_not_called()
                upload.assert_not_called()
            saved = StoryHistory(root / "history.json").read()["stories"]["toaster-lawyer"]
            self.assertEqual(saved["status"], "FAILED")
            self.assertEqual(saved["publication_state"], "REVIEW_REQUIRED")

    def test_visuals_are_deterministic_and_scene_specific(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            story = self.story()
            provider = DevelopmentVisualProvider()
            first = provider.create(story, story["scenes"][0], root / "first")
            second = provider.create(story, story["scenes"][0], root / "second")
            other = provider.create(story, story["scenes"][1], root / "first")
            self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
            self.assertNotEqual(first.path.read_bytes(), other.path.read_bytes())
            with Image.open(first.path) as image:
                self.assertEqual(image.size, (720, 1280))
                self.assertGreater(max(ImageStat.Stat(image).stddev), 20)

    def test_successful_review_cannot_publish_and_regeneration_is_versioned(self):
        class FakeVoice:
            def synthesize(self, story, directory):
                story["actual_voice_duration_seconds"] = story["estimated_voice_duration_seconds"]
                return Path(directory) / "narration.wav", [], {"provider": "test"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"PUBLISHING_ENABLED": "true", "PUBLISHING_DRY_RUN": "true"}), patch("story_engine.engine.render_story"), patch("story_engine.engine.inspect_story_final", return_value={"status": "QUALITY_CHECK_PASSED"}), patch("story_engine.engine.inspect_story_file", return_value={}), patch("requests.sessions.Session.request", side_effect=AssertionError("No network call allowed")):
                first = generate_story_video(root / "output", root / "history.json", voice_provider=FakeVoice())
                second = generate_story_video(root / "output", root / "history.json", voice_provider=FakeVoice(), regenerate_story_id=first["story_id"])
            self.assertEqual(first["status"], "READY_FOR_REVIEW")
            self.assertFalse(first["publishing_enabled"])
            self.assertFalse(first["live_request_sent"])
            self.assertFalse(first["review"]["publish_available"])
            self.assertNotEqual(first["final"], second["final"])
            self.assertTrue(Path(first["metadata"]).exists())
            self.assertTrue(Path(second["metadata"]).exists())
            record = StoryHistory(root / "history.json").read()["stories"][first["story_id"]]
            self.assertEqual(record["generation_attempts"], 2)

    def test_qc_rejects_missing_story_and_corrupt_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, metadata = root / "bad.mp4", root / "story.json"
            video.write_bytes(b"not a video")
            self.assertEqual(inspect_story_final(video, metadata)["status"], "REVIEW_REQUIRED")
            metadata.write_text(json.dumps(self.story()))
            self.assertEqual(inspect_story_final(video, metadata)["status"], "REVIEW_REQUIRED")

    def test_real_short_render_captions_audio_and_decode(self):
        import subprocess
        import wave
        import math
        import struct
        try:
            ffmpeg_binary()
            ffprobe_binary()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "voice.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                handle.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(i * 2 * math.pi * 220 / 22050))) for i in range(44100)))
            story = self.story()
            story["scenes"] = story["scenes"][:2]
            for i, scene in enumerate(story["scenes"]):
                scene.update(start_time=i, estimated_duration=1)
            story["actual_voice_duration_seconds"] = 2
            assets = [DevelopmentVisualProvider().create(story, s, root / "scenes") for s in story["scenes"]]
            output = root / "final.mp4"
            manifest = render_story(story, assets, audio, [{"start": 0, "end": .9, "text": "Hello toaster"}, {"start": 1, "end": 1.9, "text": "Objection!"}], output)
            media = media_summary(output)
            self.assertEqual((media["width"], media["height"]), (720, 1280))
            self.assertTrue(media["has_audio"])
            self.assertAlmostEqual(media["duration"], 2, places=1)
            self.assertEqual(len(_read_captions(output.with_suffix(".ass"))), 2)
            subprocess.run([ffmpeg_binary(), "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"], check=True, capture_output=True)
            self.assertFalse(manifest["publishing_enabled"])


class CloudinaryReviewTests(unittest.TestCase):
    def test_empty_cleanup_does_not_require_credentials(self):
        from publication_state import PublicationLedger
        from cloudinary_media_host import CloudinaryMediaHost
        with tempfile.TemporaryDirectory() as directory:
            host = CloudinaryMediaHost()
            with patch.object(host, "_require_credentials") as credentials:
                result = host.cleanup_expired(PublicationLedger(Path(directory) / "publication.json"))
            credentials.assert_not_called()
            self.assertEqual(result["processed"], 0)

    def test_cleanup_preserves_top_level_queued_and_foreign_folder(self):
        from publication_state import PublicationLedger
        from cloudinary_media_host import CloudinaryMediaHost
        with tempfile.TemporaryDirectory() as directory:
            ledger = PublicationLedger(Path(directory) / "publication.json")
            for key, status, public_id in [("queued", "QUEUED", "clipradar/buffer/queued"), ("foreign", "FAILED", "another-project/asset")]:
                ledger.upsert_clip(key, status)
                ledger.update_media_delivery(key, {"provider": "cloudinary", "public_id": public_id, "status": "READY_FOR_BUFFER", "cleanup_after": "2020-01-01T00:00:00+00:00"})
            host = CloudinaryMediaHost()
            with patch.object(host, "_destroy") as destroy, patch.object(host, "_require_credentials"):
                host.cleanup_expired(ledger)
            destroy.assert_not_called()

    def test_buffer_dry_run_never_instantiates_cloudinary(self):
        import orchestrator
        from types import SimpleNamespace
        fake = SimpleNamespace(config=SimpleNamespace(safe_summary=lambda: {}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"PUBLISHER_BACKEND": "buffer", "PUBLISHING_ENABLED": "false", "PUBLISHING_DRY_RUN": "true", "CLIP_RADAR_PUBLICATION_STATE_FILE": str(root / "pub.json")}), patch("orchestrator.create_publisher", return_value=fake), patch("orchestrator.scan_candidates", return_value=[]), patch("orchestrator.CloudinaryMediaHost") as cloud:
                result = orchestrator.run_live(root / "output", root / "dedupe.json")
            cloud.assert_not_called()
            self.assertEqual(result["cloudinary_deliveries"], [])
