import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS_SCRIPT = ROOT / "scripts" / "run_tod_mim_conversation_simulation.py"
HARNESS_WRAPPER = ROOT / "tod" / "Invoke-TODMimConversationSimulation.ps1"


class TodMimConversationSimulationTest(unittest.TestCase):
    def test_python_harness_runs_all_scenarios(self) -> None:
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
            self.assertEqual(payload["scenario_count"], 3, payload)
            self.assertEqual(
                {item["scenario_id"] for item in payload["results"]},
                {
                    "diagnostic_roundtrip",
                    "next_step_consensus_roundtrip",
                    "supersede_reissue_same_session",
                },
            )

    def test_next_step_consensus_scenario_returns_required_fields(self) -> None:
        from scripts.run_tod_mim_conversation_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="next_step_consensus_roundtrip",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["scenario_id"], "next_step_consensus_roundtrip")
            self.assertEqual(result["checks"]["processed_count"], 1)
            self.assertEqual(result["checks"]["reply_to_turn"], 7)
            self.assertEqual(result["checks"]["finding_positions_count"], 2)
            self.assertTrue(result["checks"]["required_fields_present"], result)

    def test_supersede_reissue_answers_latest_turn_only(self) -> None:
        from scripts.run_tod_mim_conversation_simulation import run_scenarios

        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_scenarios(
                scenario="supersede_reissue_same_session",
                synthetic_root=str(Path(tmp_dir) / "synthetic-root"),
            )
            self.assertTrue(payload["passed"], payload)
            result = payload["results"][0]
            self.assertEqual(result["checks"]["processed_count"], 1)
            self.assertEqual(result["checks"]["response_count"], 1)
            self.assertEqual(result["checks"]["reply_to_turn"], 5)

    def test_powershell_wrapper_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(HARNESS_WRAPPER),
                    "-Scenario",
                    "diagnostic_roundtrip",
                    "-SyntheticRoot",
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
            self.assertEqual(payload["scenario_count"], 1, payload)
            self.assertEqual(payload["results"][0]["scenario_id"], "diagnostic_roundtrip")


if __name__ == "__main__":
    unittest.main(verbosity=2)