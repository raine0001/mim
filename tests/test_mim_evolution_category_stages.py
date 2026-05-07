import unittest
from pathlib import Path

from scripts.run_mim_evolution_category_stages import build_stage_run_plan


class MimEvolutionCategoryStagesTests(unittest.TestCase):
    def test_build_stage_run_plan_creates_train_and_holdout_runs(self) -> None:
        repo_root = Path("/tmp/mim")
        plan = {
            "scenario_library": "conversation_scenarios/mim_evolution_training_set.json",
            "profile_library": "conversation_profiles_evolution.json",
            "categories": [
                {"category_id": "leadership", "description": "Leadership prompts"},
                {"category_id": "initiative", "description": "Initiative prompts"},
            ],
            "stages": [
                {
                    "stage_id": "pilot",
                    "train_target": 1000,
                    "holdout_target": 200,
                    "max_overall_drop": 0.02,
                    "max_failure_increase": 12,
                }
            ],
        }

        stage_plan = build_stage_run_plan(
            repo_root=repo_root,
            plan=plan,
            stage_id="pilot",
            base_url="http://127.0.0.1:18001",
            python_bin="/tmp/mim/.venv/bin/python",
            request_timeout_seconds=90,
        )

        self.assertEqual(stage_plan["stage_id"], "pilot")
        self.assertEqual(len(stage_plan["runs"]), 4)
        leadership_train = stage_plan["runs"][0]
        self.assertEqual(leadership_train["category_id"], "leadership")
        self.assertEqual(leadership_train["split"], "train")
        self.assertEqual(leadership_train["target_conversations"], 1000)
        self.assertIn("--include-categories", leadership_train["command"])
        self.assertIn("leadership", leadership_train["command"])
        self.assertIn("--include-splits", leadership_train["command"])
        self.assertIn("train", leadership_train["command"])
        self.assertIn("--request-timeout-seconds", leadership_train["command"])
        self.assertIn("90", leadership_train["command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)