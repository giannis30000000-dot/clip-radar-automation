import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from compare_visual_providers import compare, MODELS, STORY, SCENE
from story_engine.costs import CostBudget
from story_engine.provider_http import ProviderFailure
from story_engine.runway_provider import RunwayVisualProvider


class Response:
    status_code = 200

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class VisualComparisonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "RUNWAYML_API_SECRET": "mock-comparison-secret",
            "CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD": "2.50",
            "CLIP_RADAR_PROVIDER_TEST_MAX_ATTEMPTS": "1",
            "CLIP_RADAR_PROVIDER_TEST_IMAGE_URL": "https://example.com/owned-scene.png",
            "CLIP_RADAR_MAX_COST_USD_PER_VIDEO": "999",
            "RUNWAY_USD_PER_SECOND": "999", "RUNWAY_MODEL": "ignored-production-setting",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for target, kwargs in [
            ("compare_visual_providers.ffprobe_binary", {"return_value": "ffprobe"}),
            ("story_engine.runway_provider.RunwayVisualProvider._download", {}),
            ("story_engine.runway_provider.media_summary", {"return_value": {"has_video": True, "duration": 10, "width": 720, "height": 1280, "size": 100}}),
            ("compare_visual_providers.media_summary", {"return_value": {"has_video": True, "duration": 10, "width": 720, "height": 1280}}),
        ]:
            context = patch(target, **kwargs)
            context.start()
            self.addCleanup(context.stop)
        # The provider's default argument retains the original sleep function.
        real_init = RunwayVisualProvider.__init__

        def initialize(provider, *args, **kwargs):
            real_init(provider, *args, sleep=lambda _: None, **kwargs)

        context = patch.object(RunwayVisualProvider, "__init__", initialize)
        context.start()
        self.addCleanup(context.stop)
        self.http = patch("requests.sessions.Session.request")
        self.request = self.http.start()
        self.addCleanup(self.http.stop)
        self.request.side_effect = [item for model in MODELS for item in (
            Response({"id": model.replace(".", "-")}),
            Response({"status": "SUCCEEDED", "output": ["https://output.example/video.mp4?sig=private"]}),
        )]

    def test_identical_inputs_separate_outputs_and_complete_accounting(self):
        result = compare(self.root)
        self.assertEqual(result["status"], "COMPLETED")
        posts = [c.kwargs["json"] for c in self.request.call_args_list if c.args[0] == "POST"]
        for key in ("promptText", "promptImage", "duration"):
            self.assertEqual(len({p[key] for p in posts}), 1)
        self.assertEqual(posts[0]["duration"], 10)
        self.assertEqual(posts[2]["resolution"], "768p")
        self.assertEqual(posts[2]["promptExpansionMode"], "disabled")
        self.assertNotIn("ratio", posts[2])
        self.assertEqual(result["costs"]["total_estimated_cost_usd"], 2.5)
        self.assertEqual(len({r["output_path"] for r in result["results"]}), 3)
        for row, cost in zip(result["results"], (1.2, .5, .8)):
            self.assertEqual(row["provider"], "runway")
            self.assertEqual(row["estimated_cost_usd"], cost)
            self.assertEqual(row["generated_duration_seconds"], 10)
            self.assertEqual(row["request_count"], 1)
            self.assertEqual(row["retries"], 0)
            self.assertIsNone(row["actual_cost_usd"])
            self.assertGreaterEqual(row["generation_time_seconds"], 0)
            self.assertTrue((Path(row["output_path"]).parent / "result.json").exists())
        self.assertFalse(result["publishing_enabled"])
        self.assertFalse(result["scheduling_enabled"])
        self.assertEqual(os.environ["RUNWAY_MODEL"], "ignored-production-setting")
        for path in Path(result["report_path"]).parent.rglob("*.json"):
            text = path.read_text()
            self.assertNotIn("mock-comparison-secret", text)
            self.assertNotIn("sig=private", text)

    def test_no_test_budget_never_inherits_story_budget(self):
        for limit in ("", "0"):
            with self.subTest(limit=limit), patch.dict(os.environ, {"CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD": limit}):
                result = compare(self.root)
                self.assertEqual(result["status"], "PAID_TEST_DISABLED")
                self.assertEqual(result["costs"]["actual_cost_usd"], 0)
        self.request.assert_not_called()

    def test_missing_credentials_and_image_are_reported_without_calls(self):
        for name, status in (("RUNWAYML_API_SECRET", "MISSING_CREDENTIALS"), ("CLIP_RADAR_PROVIDER_TEST_IMAGE_URL", "SHARED_REFERENCE_IMAGE_REQUIRED")):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                self.assertEqual(compare(self.root)["status"], status)
        self.request.assert_not_called()

    def test_insufficient_whole_comparison_budget_stops_before_first_call(self):
        with patch.dict(os.environ, {"CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD": "2.49"}):
            result = compare(self.root)
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertEqual(result["costs"]["total_estimated_cost_usd"], 0)
        self.request.assert_not_called()

    def test_retry_consumes_shared_budget_and_stops_remaining_models(self):
        self.request.side_effect = [Response({"id": "first"}), Response({"status": "FAILED"}), Response({"id": "retry"}), Response({"status": "SUCCEEDED", "output": ["https://output.example/video.mp4"]})]
        with patch.dict(os.environ, {"CLIP_RADAR_PROVIDER_TEST_MAX_ATTEMPTS": "2"}):
            result = compare(self.root)
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertEqual(result["costs"]["total_estimated_cost_usd"], 2.4)
        self.assertEqual(result["results"][0]["request_count"], 2)
        self.assertEqual(result["results"][0]["retries"], 1)
        self.assertEqual(result["results"][1]["request_count"], 0)
        self.assertEqual(self.request.call_count, 4)

    def test_failure_is_not_replaced_by_development_visual(self):
        self.request.side_effect = [Response({"id": "failed"}), Response({"status": "FAILED"})]
        result = compare(self.root, models=["gen4.5"])
        self.assertEqual(result["status"], "COMPLETED_WITH_FAILURES")
        self.assertEqual(result["results"][0]["status"], "FAILED")
        self.assertIsNone(result["results"][0]["output_path"])
        self.assertEqual(result["results"][0]["estimated_cost_usd"], 1.2)

    def test_unique_directories_preserve_previous_comparison(self):
        with patch.dict(os.environ, {"CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD": "0"}):
            first, second = compare(self.root), compare(self.root)
        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertTrue(Path(first["report_path"]).exists())

    def test_turbo_requires_image_before_paid_request(self):
        provider = RunwayVisualProvider(CostBudget(self.root / "cost.json", 5), model="gen4_turbo", usd_per_second=".05")
        with self.assertRaisesRegex(ProviderFailure, "REFERENCE_IMAGE_REQUIRED"):
            provider.create(STORY, SCENE, self.root / "turbo")
        self.request.assert_not_called()

    def test_invalid_selection_or_budget_never_calls_provider(self):
        for models in ([], ["unknown"], ["gen4.5", "gen4.5"]):
            with self.assertRaises(ValueError):
                compare(self.root, models)
        for limit in ("-1", "NaN", "Infinity"):
            with patch.dict(os.environ, {"CLIP_RADAR_PROVIDER_TEST_MAX_COST_USD": limit}), self.assertRaises(ValueError):
                compare(self.root)
        self.request.assert_not_called()

    def test_missing_ffprobe_does_not_spend(self):
        with patch("compare_visual_providers.ffprobe_binary", side_effect=RuntimeError("missing")):
            result = compare(self.root)
        self.assertEqual(result["status"], "FFPROBE_REQUIRED")
        self.request.assert_not_called()
