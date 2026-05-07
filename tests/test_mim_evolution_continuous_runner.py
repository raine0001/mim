import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.training_routine_service import build_next_cycle_plan, load_routine_profile
from scripts.mim_evolution_continuous_runner import build_proof_summary, collect_evaluation_inputs


class ContinuousTrainingRunnerMetricsTest(unittest.TestCase):
    def test_collect_evaluation_inputs_aggregates_report_metrics(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            conversation_report = tmp_path / "conversation.json"
            training_summary = tmp_path / "summary.json"

            conversation_report.write_text(
                """
{
  "summary": {
    "overall": 0.81,
    "scenario_count": 2,
    "failure_count": 3,
    "top_failures": [
      {"tag": "context_drift", "count": 2},
      {"tag": "clarification_spam", "count": 1}
    ]
  },
  "results": [
    {
      "score": {
        "overall": 0.9,
        "relevance": 0.95,
        "task_completion": 0.8,
        "initiative": 0.7,
        "smoothness": 0.85,
        "brevity": 0.75,
        "non_repetition": 0.8,
        "safety": 1.0
      },
      "failures": ["context_drift"]
    },
    {
      "score": {
        "overall": 0.7,
        "relevance": 0.65,
        "task_completion": 0.9,
        "initiative": 0.6,
        "smoothness": 0.75,
        "brevity": 0.85,
        "non_repetition": 0.9,
        "safety": 0.95
      },
      "failures": ["clarification_spam", "context_drift"]
    }
  ]
}
                """.strip(),
                encoding="utf-8",
            )
            training_summary.write_text(
                """
{
  "conversation": {
    "overall": 0.81,
    "scenario_count": 2,
    "failure_count": 3
  },
  "actions": {
    "pass_ratio": 0.75
  }
}
                """.strip(),
                encoding="utf-8",
            )

            inputs = collect_evaluation_inputs(
                conversation_report_path=conversation_report,
                training_summary_path=training_summary,
            )

            metrics_json = inputs["metrics_json"]
            self.assertAlmostEqual(metrics_json["overall"], 0.8)
            self.assertAlmostEqual(metrics_json["relevance"], 0.8)
            self.assertAlmostEqual(metrics_json["task_completion"], 0.85)
            self.assertAlmostEqual(metrics_json["smoke"]["action_pass_ratio"], 0.75)
            self.assertEqual(inputs["failure_tags"], ["clarification_spam", "context_drift"])
            self.assertEqual(inputs["top_failures"][0]["tag"], "context_drift")
            self.assertIn("followup_continuity", inputs["discovered_skill_candidates"])
            self.assertIn("clarification_discipline", inputs["discovered_skill_candidates"])

            proof_summary = build_proof_summary(cycle=4, evaluation_inputs=inputs)
            self.assertIn("cycle 4", proof_summary)
            self.assertIn("overall=0.8000", proof_summary)

    def test_build_next_cycle_plan_reduces_batch_when_quality_is_weak(self) -> None:
        profile = load_routine_profile(
            {
                "MIM_TRAINING_TARGET_CONVERSATIONS": "320",
                "MIM_TRAINING_MIN_CONVERSATIONS": "240",
                "MIM_TRAINING_MAX_CONVERSATIONS": "720",
                "MIM_TRAINING_TARGET_WINDOW_SECONDS": "5400",
            }
        )
        plan = build_next_cycle_plan(
            profile=profile,
            previous_status={"elapsed_seconds": 5400, "run_exit_code": 0},
            previous_summary={
                "conversation": {"overall": 0.71, "scenario_count": 600, "failure_count": 420},
                "actions": {"pass_ratio": 0.88},
            },
        )
        self.assertEqual(plan["quality_signal"], "reduce")
        self.assertLess(plan["target_conversations"], 600)
        self.assertGreaterEqual(plan["target_conversations"], profile["min_conversations"])

    def test_build_next_cycle_plan_expands_batch_when_quality_is_strong(self) -> None:
        profile = load_routine_profile(
            {
                "MIM_TRAINING_TARGET_CONVERSATIONS": "320",
                "MIM_TRAINING_MIN_CONVERSATIONS": "240",
                "MIM_TRAINING_MAX_CONVERSATIONS": "720",
                "MIM_TRAINING_TARGET_WINDOW_SECONDS": "5400",
            }
        )
        plan = build_next_cycle_plan(
            profile=profile,
            previous_status={"elapsed_seconds": 3600, "run_exit_code": 0},
            previous_summary={
                "conversation": {"overall": 0.86, "scenario_count": 320, "failure_count": 10},
                "actions": {"pass_ratio": 1.0},
            },
        )
        self.assertEqual(plan["quality_signal"], "expand")
        self.assertGreater(plan["target_conversations"], profile["default_conversations"])
        self.assertLessEqual(plan["target_conversations"], profile["max_conversations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)