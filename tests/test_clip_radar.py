import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dedupe import DedupeStore
from acquisition import AcquisitionError, AcquisitionResult, acquire_candidate
from buffer_publisher import BufferConfig, BufferPublisher
from content_safety import check_third_party_content
import orchestrator
from rights_gate import pick_eligible
from scanner import total_score
from media_processor import write_srt
from metricool_publisher import MetricoolConfig, MetricoolPublisher, PublicationBlocked, ValidatedClip
from publication_state import PublicationLedger
from publish_plan import build_metadata
from schedule_slots import next_production_slot


class ClipRadarTests(unittest.TestCase):
    def test_rights_gate_skips_unverified_and_selects_xqc(self):
        selected, skipped = pick_eligible(
            [
                {"clip_id": "unverified", "streamer": "kaicenat", "score": 99},
                {"clip_id": "verified", "streamer": "xQc", "score": 80},
            ],
            limit=1,
        )
        self.assertEqual([item["clip_id"] for item in selected], ["verified"])
        self.assertEqual(skipped[0]["reason"], "sharing_not_verified")

    def test_dedupe_is_keyed_by_clip_id_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "processed_clips.json"
            store = DedupeStore(path)
            self.assertFalse(store.contains("abc123"))
            store.record("abc123", "prepared", final="final.mp4")
            self.assertTrue(DedupeStore(path).contains("abc123"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["clips"]["abc123"]["status"], "prepared")

    def test_score_rewards_traction_without_replacing_freshness(self):
        fresh = {
            "created_at": "2026-09-15T12:00:00Z",
            "view_count": 100,
            "duration": 30,
            "title": "reaction",
        }
        older = {
            "created_at": "2026-09-15T10:00:00Z",
            "view_count": 10_000,
            "duration": 30,
            "title": "reaction",
        }
        from datetime import datetime, timezone

        now = datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc)
        self.assertGreater(total_score(older, now), total_score(fresh, now))

    def test_acquisition_requires_explicit_rights_gate_approval(self):
        candidate={"id":"abc123","url":"https://www.twitch.tv/xqc/clip/abc123"}
        with self.assertRaisesRegex(AcquisitionError, "rights gate"):
            acquire_candidate(candidate, Path(tempfile.gettempdir()))

    def test_orchestrator_tries_next_eligible_candidate_after_failure(self):
        candidates=[]
        for clip_id, score in (("first123", 99.0), ("second123", 80.0)):
            candidates.append({
                "id": clip_id,
                "streamer": "xQc",
                "title": clip_id,
                "url": f"https://www.twitch.tv/xqc/clip/{clip_id}",
                "score": score,
                "view_count": 1000,
                "duration": 20,
                "age_h": 1,
                "vph": 1000,
            })
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"second.mp4"
            source.write_bytes(b"source placeholder")
            acquired=AcquisitionResult("second123", source, "test-provider", 1920, 1080, 20, 1000, "h264", "aac")
            with patch.object(orchestrator, "scan_candidates", return_value=candidates), \
                 patch.object(orchestrator, "print_candidates"), \
                 patch.object(orchestrator, "acquire_candidate", side_effect=[AcquisitionError("first failed"), acquired]), \
                 patch.object(orchestrator, "render_vertical"), \
                 patch.object(orchestrator, "check_transformation", return_value={"status": "TRANSFORMATION_CHECK_PASSED", "reason": "test"}), \
                 patch.object(orchestrator, "validate_final", return_value=(True, "ready_for_publish_queue")):
                result=orchestrator.run_live(root/"output", root/"state.json", max_outputs=1, max_candidates=20)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["outputs"][0]["clip_id"], "second123")
        self.assertEqual(result["attempts"][0]["reason"], "first failed")

    def test_subtitle_lines_are_bounded_for_vertical_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.srt"
            write_srt(
                [{"start": 0.0, "end": 2.0, "text": "under arrest. Do you have the right"}],
                path,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
        caption_lines = lines[3:5]
        self.assertTrue(caption_lines)
        self.assertTrue(all(len(line) <= 26 for line in caption_lines))

    def test_metadata_is_platform_specific_and_contextual(self):
        metadata = build_metadata(
            "xQc",
            "Jean Paul gets IRL GO Postal delivery",
            game_name="Just Chatting",
            source_url="https://www.twitch.tv/xqc/clip/clip123",
            transcript_entries=[{"start": 0.0, "end": 1.0, "text": "That is unbelievable"}],
        )
        self.assertNotEqual(metadata["tiktok"]["caption"], metadata["instagram"]["caption"])
        self.assertIn("https://www.twitch.tv/xqc/clip/clip123", metadata["source_url"])
        self.assertIn("unbelievable", metadata["transcript_excerpt"])
        self.assertLessEqual(len(metadata["hashtags"]), 5)
        self.assertEqual(metadata["instagram"]["type"], "REEL")
        self.assertEqual(metadata["youtube"]["category"], "GAMING")

    def test_production_slot_is_future_europe_athens_time(self):
        now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
        slot = next_production_slot(now=now, timezone_name="Europe/Athens")
        self.assertEqual(slot.strftime("%Y-%m-%d %H:%M"), "2026-09-15 12:00")
        self.assertGreater(slot, now.astimezone(slot.tzinfo))

    def test_metricool_dry_run_plan_is_gated_and_has_all_metadata(self):
        config = MetricoolConfig(
            enabled=False,
            dry_run=True,
            networks=("tiktok", "instagram"),
            timezone_name="Europe/Athens",
            base_url="https://app.metricool.com/api",
            user_token=None,
            user_id=None,
            blog_id=None,
        )
        clip = ValidatedClip(
            candidate={
                "id": "clip123",
                "streamer": "xQc",
                "title": "A real eligible moment",
                "game_name": "Just Chatting",
                "url": "https://www.twitch.tv/xqc/clip/clip123",
                "score": 60.0,
                "view_count": 5000,
                "duration": 30.0,
            },
            final_path=Path("final.mp4"),
            qc_status="ready_for_publish_queue",
        )
        metadata = build_metadata("xQc", clip.candidate["title"], game_name="Just Chatting")
        with patch("metricool_publisher.validate_final", return_value=(True, "ready_for_publish_queue")):
            plan = MetricoolPublisher(config=config).build_plan(
                clip,
                metadata,
                slot=datetime(2026, 9, 15, 12, 0, tzinfo=ZoneInfo("Europe/Athens")),
            )
        self.assertEqual(plan["status"], "DRY_RUN_READY")
        self.assertEqual(set(plan["networks"]), {"tiktok", "instagram"})
        self.assertEqual(plan["disabled_networks"], ["youtube"])
        self.assertIn("youtube", plan["all_generated_metadata"])
        self.assertEqual(plan["publication_slot"]["timezone"], "Europe/Athens")
        self.assertFalse(plan["live_request_sent"])
        self.assertFalse(plan["configuration"]["credentials_configured"])

    def test_metricool_reserves_distinct_production_slots(self):
        config = MetricoolConfig(
            enabled=False,
            dry_run=True,
            networks=("tiktok", "instagram"),
            timezone_name="Europe/Athens",
            base_url="https://app.metricool.com/api",
            user_token=None,
            user_id=None,
            blog_id=None,
        )
        publisher = MetricoolPublisher(config=config)
        metadata = build_metadata("xQc", "A real eligible moment")
        candidates = [
            {"id": "clip123", "streamer": "xQc", "title": "A real eligible moment", "url": "https://twitch.tv/xqc/clip/clip123"},
            {"id": "clip456", "streamer": "xQc", "title": "Another real eligible moment", "url": "https://twitch.tv/xqc/clip/clip456"},
        ]
        with patch("metricool_publisher.validate_final", return_value=(True, "ready_for_publish_queue")):
            first = publisher.build_plan(ValidatedClip(candidates[0], Path("one.mp4"), "ready_for_publish_queue"), metadata)
            second = publisher.build_plan(ValidatedClip(candidates[1], Path("two.mp4"), "ready_for_publish_queue"), metadata)
        self.assertNotEqual(first["publication_slot"]["date_time"], second["publication_slot"]["date_time"])

    def test_metricool_refuses_youtube_until_explicitly_enabled(self):
        with patch.dict(os.environ, {"METRICOOL_NETWORKS": "tiktok,instagram,youtube", "ENABLE_YOUTUBE_PUBLISHING": "false"}, clear=False):
            with self.assertRaises(PublicationBlocked):
                MetricoolConfig.from_env()

    def test_metricool_refuses_unverified_broadcaster(self):
        clip = ValidatedClip(
            candidate={"id": "clip123", "streamer": "kaicenat"},
            final_path=Path("final.mp4"),
            qc_status="ready_for_publish_queue",
        )
        with patch("metricool_publisher.validate_final", return_value=(True, "ready_for_publish_queue")):
            with self.assertRaises(PublicationBlocked):
                MetricoolPublisher(
                    config=MetricoolConfig(
                        enabled=False,
                        dry_run=True,
                        networks=("tiktok", "instagram"),
                        timezone_name="Europe/Athens",
                        base_url="https://app.metricool.com/api",
                        user_token=None,
                        user_id=None,
                        blog_id=None,
                    )
                ).validate_input(clip)

    def test_publication_ledger_persists_network_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publications.json"
            ledger = PublicationLedger(path)
            ledger.upsert_clip("clip123", "QC_PASSED", broadcaster="xQc")
            ledger.update_network("clip123", "tiktok", "QUEUED", metricool_id="post123")
            restored = PublicationLedger(path)
            self.assertTrue(restored.is_active("clip123"))
            self.assertEqual(restored.get("clip123")["networks"]["tiktok"]["metricool_id"], "post123")

    def test_publication_ledger_retries_planning_failure_after_qc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publications.json"
            ledger = PublicationLedger(path)
            ledger.upsert_clip("clip123", "QC_PASSED")
            ledger.upsert_clip("clip123", "FAILED", error="publisher planning failed")
            self.assertTrue(ledger.needs_publication_retry("clip123"))
            ledger.update_network("clip123", "instagram", "FAILED", error="post failed")
            self.assertFalse(ledger.needs_publication_retry("clip123"))

    def test_third_party_content_check_fails_closed_for_obvious_broadcast_media(self):
        result = check_third_party_content(
            {"title": "Streamer reacts to a tournament broadcast", "game_name": "Just Chatting"},
            [],
        )
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_buffer_dry_run_discovers_and_selects_real_channel_candidates(self):
        class FakeResponse:
            ok = True
            status_code = 200
            headers = {"X-RateLimit-Remaining": "2999"}

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeResponse({"data": {"account": {"organizations": [{"id": "org-1", "name": "Clip Radar"}]}}}),
                    FakeResponse({"data": {"channels": [
                        {"id": "ig-1", "name": "Clip Radar Instagram", "service": "instagram"},
                        {"id": "tt-1", "name": "Clip Radar TikTok", "service": "tiktok"},
                        {"id": "yt-1", "name": "Clip Radar YouTube", "service": "youtube"},
                    ]}}),
                ]

            def post(self, url, **kwargs):
                self.calls.append(kwargs["json"]["query"])
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            config = BufferConfig(
                enabled=False,
                dry_run=True,
                networks=("tiktok", "instagram"),
                timezone_name="Europe/Athens",
                api_url="https://api.buffer.com",
                api_key="test-only-key",
                channel_config_path=Path(directory) / "buffer_channels.json",
                brand_name="Clip Radar",
                instagram_channel_id=None,
                tiktok_channel_id=None,
                youtube_channel_id=None,
                media_url=None,
            )
            publisher = BufferPublisher(config=config, session=FakeSession())
            clip = ValidatedClip(
                candidate={
                    "id": "clip123",
                    "streamer": "xQc",
                    "title": "A real eligible moment",
                    "game_name": "Just Chatting",
                    "url": "https://www.twitch.tv/xqc/clip/clip123",
                },
                final_path=Path(directory) / "final.mp4",
                qc_status="ready_for_publish_queue",
            )
            with patch("buffer_publisher.validate_final", return_value=(True, "ready_for_publish_queue")):
                plan = publisher.build_plan(clip, build_metadata("xQc", "A real eligible moment"))
        self.assertEqual(plan["backend"], "buffer")
        self.assertEqual(plan["channels"]["instagram"]["id"], "ig-1")
        self.assertEqual(plan["channels"]["tiktok"]["id"], "tt-1")
        self.assertEqual(plan["channel_discovery"]["api_calls"], 2)
        self.assertEqual(set(plan["networks"]), {"instagram", "tiktok"})
        self.assertFalse(plan["live_request_sent"])
        self.assertEqual(plan["networks"]["instagram"]["input"]["metadata"]["instagram"]["type"], "reel")
        self.assertEqual(plan["networks"]["tiktok"]["input"]["channelId"], "tt-1")


if __name__ == "__main__":
    unittest.main()
