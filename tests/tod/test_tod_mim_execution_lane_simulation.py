import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS_SCRIPT = ROOT / "scripts" / "run_tod_mim_execution_lane_simulation.py"


class TodMimExecutionLaneSimulationTest(unittest.TestCase):
    def test_python_harness_runs_all_execution_scenarios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            completed = subprocess.run(
                [
                    str(ROOT / ".venv" / "bin" / "python"),
                    str(HARNESS_SCRIPT),
                    "--scenario",
                    "all",
                    "--synthetic-root",
                    str(Path(tmp_dir) / "synthetic-root"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["passed"], payload)
            self.assertEqual(payload["scenario_count"], 8, payload)

    def test_duplicate_request_scenario_keeps_ack_and_result_singleton(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="duplicate_request_idempotent",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["ack_count"], 1)
            self.assertEqual(result["checks"]["result_count"], 1)
            self.assertEqual(result["checks"]["second_disposition"], "duplicate")

    def test_stale_and_wrong_target_requests_are_rejected(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="stale_or_wrong_target_rejected",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["stale_reason"], "stale_request")
            self.assertEqual(result["checks"]["wrong_target_reason"], "wrong_target")

    def test_timeout_and_failure_are_surfaceable(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="timeout_failure_surfaced",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["timeout_result_status"], "timed_out")
            self.assertEqual(result["checks"]["failure_result_status"], "failed")

    def test_expanded_command_vocabulary_is_supported_in_synthetic_lane(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="expanded_command_vocabulary_supported",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertIn("move_home", result["checks"]["allowed_commands"])
            self.assertIn("move_relative", result["checks"]["allowed_commands"])
            self.assertIn("move_relative_then_set_gripper", result["checks"]["allowed_commands"])
            self.assertIn("pick_and_place", result["checks"]["allowed_commands"])
            self.assertIn("pick_at", result["checks"]["allowed_commands"])
            self.assertIn("place_at", result["checks"]["allowed_commands"])
            self.assertIn("set_gripper", result["checks"]["allowed_commands"])
            self.assertEqual(result["checks"]["current_execution_state"]["processed_request_count"], 9)

    def test_invalid_command_args_are_rejected_before_execution(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="invalid_command_args_rejected",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["gripper_reason"], "invalid_command_args:position_out_of_range")
            self.assertEqual(result["checks"]["speed_reason"], "invalid_command_args:level_unsupported")

    def test_move_relative_lineage_is_explicitly_supported(self) -> None:
        from scripts.run_tod_mim_execution_lane_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="move_relative_lineage_supported",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["duplicate_disposition"], "duplicate")
            self.assertEqual(result["checks"]["superseded_disposition"], "ignored_superseded")


if __name__ == "__main__":
    unittest.main(verbosity=2)