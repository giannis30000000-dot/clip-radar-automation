import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from dedupe import DedupeStore, same_source_moment, source_moment
from acquisition import AcquisitionError, AcquisitionResult, acquire_candidate
from buffer_publisher import BufferConfig, BufferPublisher
from buffer_reconcile import reconcile
from cloudinary_media_host import CloudinaryConfig, CloudinaryMediaHost
from content_safety import check_third_party_content
import orchestrator
from rights_gate import pick_eligible
from scanner import total_score
from media_processor import write_ass, write_srt
from metricool_publisher import MetricoolConfig, MetricoolPublisher, PublicationBlocked, ValidatedClip
from publication_state import PublicationLedger
from publish_plan import build_hook, build_metadata
from fingerprint import fingerprint_similarity
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

    def test_dedupe_rejects_same_vod_moment_with_a_different_clip_id(self):
        first = {
            "id": "first-clip-123",
            "streamer": "xQc",
            "title": "Jean Paul delivery reaction",
            "video_id": "vod-77",
            "vod_offset": 412.0,
            "created_at": "2026-09-15T18:00:00Z",
            "duration": 30,
        }
        second = {**first, "id": "different-clip-456", "vod_offset": 435.0}
        self.assertTrue(same_source_moment(source_moment(first), source_moment(second)))
        with tempfile.TemporaryDirectory() as directory:
            store = DedupeStore(Path(directory) / "processed.json")
            store.record("first-clip-123", "prepared", source_moment=source_moment(first))
            duplicate = store.find_duplicate(second)
        self.assertEqual(duplicate["reason"], "same_source_moment")

    def test_dedupe_imports_history_alias_without_relying_on_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DedupeStore(Path(directory) / "processed.json")
            result = store.import_publication_history([
                {
                    "id": "buffer-old-1",
                    "channel_name": "clipradar01",
                    "service": "instagram",
                    "status": "sent",
                    "text": "Jean Paul just got an IRL delivery and xQc could not believe it #xQc #JeanPaul #GTARP",
                }
            ])
            duplicate = store.find_duplicate({
                "id": "new-clip-999",
                "streamer": "xQc",
                "title": "Jean Paul gets IRL GO Postal delivery",
            })
        self.assertEqual(result["imported"], 1)
        self.assertEqual(duplicate["reason"], "publication_history_alias")

    def test_perceptual_fingerprint_matches_same_media_without_same_id(self):
        fingerprint = {
            "sha256": "same-content",
            "frame_hashes": ["0f" * 32, "f0" * 32],
            "audio_signature": [10, 20, 30, 40],
            "duration": 30,
        }
        comparison = fingerprint_similarity(fingerprint, dict(fingerprint))
        self.assertTrue(comparison["duplicate"])
        self.assertEqual(comparison["frame_similarity"], 1.0)

    def test_perceptual_fingerprint_does_not_treat_generic_hud_as_duplicate(self):
        left = {
            "sha256": "left", "duration": 30,
            "frame_hashes": ["0f" * 32, "f0" * 32],
            "audio_signature": [10, 20, 30, 40],
            "frame_color_histograms": [[255, 0, 0, 0], [255, 0, 0, 0]],
        }
        right = {
            "sha256": "right", "duration": 30,
            "frame_hashes": ["0f" * 32, "f0" * 32],
            "audio_signature": [10, 20, 30, 40],
            "frame_color_histograms": [[0, 0, 0, 255], [0, 0, 0, 255]],
        }
        self.assertFalse(fingerprint_similarity(left, right)["duplicate"])

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

    def test_subtitles_use_one_explicit_bottom_safe_zone_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.srt"
            write_srt([{"start": 0.0, "end": 1.0, "text": "one short caption"}], path)
            content = path.read_text(encoding="utf-8")
        self.assertIn(r"{\an2\pos(360,1060)}", content)

    def test_ass_subtitles_declare_vertical_play_resolution_and_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            write_ass([{"start": 0.0, "end": 1.0, "text": "one short caption"}], path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("PlayResX: 720", content)
        self.assertIn("PlayResY: 1280", content)
        self.assertIn(r"{\an2\pos(360,1060)}", content)

    def test_hook_is_short_and_is_not_a_second_subtitle_system(self):
        hook = build_hook("xQc", "Jean Paul gets IRL GO Postal delivery")
        self.assertLessEqual(len(hook), 48)
        self.assertNotIn("\n", hook)

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

    def test_buffer_reconciliation_marks_only_missing_network_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "publications.json"
            ledger = PublicationLedger(state_path)
            ledger.upsert_clip("clip123", "FAILED", broadcaster="xQc")
            result = reconcile(
                [
                    {
                        "clip_id": "clip123",
                        "network": "tiktok",
                        "buffer_post_id": "buffer-123",
                        "status": "PUBLISHED",
                        "channel_id": "tt-1",
                        "due_at": "2026-09-15T20:15:00.000Z",
                    }
                ],
                state_path=state_path,
                output_path=root / "reconciliation.json",
            )
            record = PublicationLedger(state_path).get("clip123")
        self.assertEqual(result["entries"], 1)
        self.assertEqual(record["networks"]["tiktok"]["status"], "PUBLISHED")
        self.assertEqual(record["networks"]["tiktok"]["buffer_post_id"], "buffer-123")
        self.assertEqual(record["networks"]["instagram"]["status"], "FAILED")

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

    def test_buffer_live_publish_records_post_ids_without_duplicate_status_argument(self):
        class FakeResponse:
            ok = True
            status_code = 200
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse({"data": {"account": {"organizations": [{"id": "org-1", "name": "Clip Radar"}]}}}),
                    FakeResponse({"data": {"channels": [
                        {"id": "ig-1", "name": "clipradar01", "service": "instagram"},
                        {"id": "tt-1", "name": "clipradar001", "service": "tiktok"},
                    ]}}),
                    FakeResponse({"data": {"createPost": {"post": {
                        "id": "buffer-tt-1", "channelId": "tt-1", "dueAt": "2026-09-15T20:15:00.000Z", "status": "scheduled"
                    }}}}),
                    FakeResponse({"data": {"createPost": {"post": {
                        "id": "buffer-ig-1", "channelId": "ig-1", "dueAt": "2026-09-15T20:15:00.000Z", "status": "scheduled"
                    }}}}),
                ]

            def post(self, url, **kwargs):
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BufferConfig(
                enabled=True,
                dry_run=False,
                networks=("tiktok", "instagram"),
                timezone_name="Europe/Athens",
                api_url="https://api.buffer.com",
                api_key="test-only-key",
                channel_config_path=root / "buffer_channels.json",
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
                    "url": "https://www.twitch.tv/xqc/clip/clip123",
                },
                final_path=root / "final.mp4",
                qc_status="ready_for_publish_queue",
            )
            ledger = PublicationLedger(root / "publications.json")
            with patch("buffer_publisher.validate_final", return_value=(True, "ready_for_publish_queue")):
                plan = publisher.build_plan(
                    clip,
                    build_metadata("xQc", "A real eligible moment"),
                    slot=datetime(2026, 9, 15, 22, 15, tzinfo=ZoneInfo("Europe/Athens")),
                    media_url="https://res.cloudinary.com/demo/video/upload/clip123.mp4",
                )
                result = publisher.publish(clip, {}, ledger, plan=plan)
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(result["networks"]["tiktok"]["buffer_post_id"], "buffer-tt-1")
        self.assertEqual(result["networks"]["instagram"]["buffer_post_id"], "buffer-ig-1")
        self.assertEqual(ledger.get("clip123")["networks"]["tiktok"]["status"], "QUEUED")
        self.assertEqual(ledger.get("clip123")["networks"]["instagram"]["status"], "QUEUED")

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
