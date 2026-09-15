import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe import DedupeStore
from acquisition import AcquisitionError, AcquisitionResult, acquire_candidate
import orchestrator
from rights_gate import pick_eligible
from scanner import total_score
from media_processor import write_srt


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


if __name__ == "__main__":
    unittest.main()
