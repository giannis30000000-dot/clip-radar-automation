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
from cloudinary_media_host import CloudinaryConfig, CloudinaryMediaHost
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

    def test_cloudinary_upload_verifies_and_reuses_final_delivery(self):
        class FakeResponse:
            ok = True
            status_code = 200
            url = "https://res.cloudinary.com/demo/video/upload/v1/clipradar/buffer/clip123.mp4"

            def __init__(self, payload=None, headers=None, status_code=200):
                self.payload = payload or {}
                self.headers = headers or {"Content-Type": "video/mp4", "Content-Length": "4096"}
                self.status_code = status_code
                self.ok = 200 <= status_code < 400

            def json(self):
                return self.payload

            def iter_content(self, chunk_size=4096):
                yield b"\x00\x00\x00\x18ftypisom" + b"0" * 100

            def close(self):
                return None

        class FakeSession:
            def __init__(self):
                self.posts = []
                self.heads = 0
                self.gets = 0

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                if url.endswith("/video/upload"):
                    return FakeResponse(
                        {
                            "secure_url": "https://res.cloudinary.com/demo/video/upload/v1/clipradar/buffer/clip123.mp4",
                            "public_id": "clipradar/buffer/clip123",
                            "bytes": 9,
                            "duration": 3.0,
                            "width": 720,
                            "height": 1280,
                            "format": "mp4",
                            "resource_type": "video",
                        }
                    )
                return FakeResponse({"result": "ok"})

            def head(self, url, **kwargs):
                self.heads += 1
                return FakeResponse()

            def get(self, url, **kwargs):
                self.gets += 1
                response = FakeResponse()
                response.status_code = 206
                response.ok = True
                return response

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final = root / "clip.mp4"
            final.write_bytes(b"final mp4")
            ledger = PublicationLedger(root / "publications.json")
            session = FakeSession()
            host = CloudinaryMediaHost(
                config=CloudinaryConfig(
                    cloud_name="demo",
                    api_key="key",
                    api_secret="secret",
                    folder="clipradar/buffer",
                    retention_hours=48,
                    abandoned_retention_hours=24,
                    max_upload_bytes=1000,
                    max_active_objects=8,
                    max_cleanup_per_run=20,
                ),
                session=session,
                clock=lambda: datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
            )
            first = host.upload_final(final, "clip123", ledger)
            second = host.upload_final(final, "clip123", ledger)
            saved = json.loads((root / "publications.json").read_text(encoding="utf-8"))

        self.assertEqual(first["public_id"], "clipradar/buffer/clip123")
        self.assertEqual(first["status"], "READY_FOR_BUFFER")
        self.assertEqual(first["verification"]["status"], "PUBLIC_HTTPS_MP4_VERIFIED")
        self.assertEqual(second["public_url"], first["public_url"])
        self.assertEqual(len([item for item in session.posts if item[0].endswith("/video/upload")]), 1)
        upload_call = next(item for item in session.posts if item[0].endswith("/video/upload"))
        self.assertEqual(upload_call[1]["auth"], ("key", "secret"))
        self.assertNotIn("secret", json.dumps(saved))

    def test_cloudinary_cleanup_skips_queued_assets_and_deletes_expired_safe_asset(self):
        class FakeResponse:
            ok = True
            status_code = 200
            headers = {}

            def json(self):
                return {"result": "ok"}

        class FakeSession:
            def __init__(self):
                self.urls = []

            def post(self, url, **kwargs):
                self.urls.append(url)
                return FakeResponse()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = PublicationLedger(root / "publications.json")
            old = "2026-09-10T12:00:00+00:00"
            ledger.upsert_clip("expired", "PUBLISHED")
            ledger.update_media_delivery(
                "expired",
                {"provider": "cloudinary", "status": "BUFFER_PUBLISHED", "public_id": "clipradar/buffer/expired", "cleanup_after": old},
            )
            ledger.upsert_clip("queued", "QUEUED")
            ledger.update_network("queued", "instagram", "QUEUED")
            ledger.update_media_delivery(
                "queued",
                {"provider": "cloudinary", "status": "BUFFER_QUEUED", "public_id": "clipradar/buffer/queued", "cleanup_after": old},
            )
            session = FakeSession()
            host = CloudinaryMediaHost(
                config=CloudinaryConfig(
                    cloud_name="demo",
                    api_key="key",
                    api_secret="secret",
                    folder="clipradar/buffer",
                    retention_hours=48,
                    abandoned_retention_hours=24,
                    max_upload_bytes=1000,
                    max_active_objects=8,
                    max_cleanup_per_run=20,
                ),
                session=session,
                clock=lambda: datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),
            )
            result = host.cleanup_expired(ledger)

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["results"][0]["clip_id"], "expired")
            self.assertEqual(ledger.get("expired")["media_delivery"]["status"], "DELETED")
            self.assertEqual(ledger.get("queued")["media_delivery"]["status"], "BUFFER_QUEUED")
            self.assertEqual(len(session.urls), 1)


if __name__ == "__main__":
    unittest.main()
