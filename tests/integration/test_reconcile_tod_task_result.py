import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reconcile_tod_task_result.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


class ReconcileTodTaskResultTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_reconcile_promotes_current_accepted_review_into_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-03-31T02:15:36Z",
                    "task_id": "objective-97-task-3422",
                    "objective_id": "objective-97",
                    "correlation_id": "obj97-task3422",
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_REVIEW_DECISION.latest.json",
                {
                    "generated_at": "2026-03-31T02:19:55Z",
                    "task_id": "objective-97-task-3422",
                    "objective_id": "objective-97",
                    "correlation_id": "obj97-task3422",
                    "decision": "accepted",
                    "decision_rationale": "Automated checkpoint accepted.",
                },
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {
                    "generated_at": "2026-03-30T03:20:42Z",
                    "source": "tod-mim-task-result-v1",
                    "execution_readiness": {"valid": True},
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads((shared_dir / "TOD_MIM_TASK_RESULT.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["request_id"], "objective-97-task-3422")
            self.assertEqual(payload["task_id"], "objective-97-task-3422")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["objective_id"], "97")
            self.assertEqual(payload["correlation_id"], "obj97-task3422")
            self.assertTrue(payload["review_gate"]["passed"])
            self.assertEqual(payload["review_gate"]["decision"], "accepted")

    def test_reconcile_keeps_review_gate_false_when_decision_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-03-31T02:15:36Z",
                    "task_id": "objective-97-task-3422",
                    "objective_id": "objective-97",
                    "correlation_id": "obj97-task3422",
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_REVIEW_DECISION.latest.json",
                {
                    "generated_at": "2026-03-31T02:19:55Z",
                    "task_id": "objective-97-task-3421",
                    "objective_id": "objective-97",
                    "correlation_id": "obj97-task3421",
                    "decision": "accepted",
                },
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {
                    "generated_at": "2026-03-30T03:20:42Z",
                    "source": "tod-mim-task-result-v1",
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads((shared_dir / "TOD_MIM_TASK_RESULT.latest.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["review_gate"]["passed"])
            self.assertEqual(payload["review_gate"]["reason"], "review_gate_unresolved")
            self.assertEqual(payload["request_id"], "objective-97-task-3422")

    def test_reconcile_rebinds_stale_result_identity_to_active_ack_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-05T16:16:49Z",
                    "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                    "objective_id": "objective-97",
                    "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                },
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_ACK.latest.json",
                {
                    "generated_at": "2026-04-05T16:21:57Z",
                    "request_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                    "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                    "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                    "bridge_runtime": {
                        "current_processing": {
                            "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                            "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                        }
                    },
                },
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {
                    "generated_at": "2026-04-03T04:07:22Z",
                    "source": "tod-mim-task-result-v1",
                    "request_id": "objective-97-task-mim-arm-safe-home-1775247707",
                    "task_id": "objective-97-task-mim-arm-safe-home-1775247707",
                    "status": "failed",
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads((shared_dir / "TOD_MIM_TASK_RESULT.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
            self.assertEqual(payload["task_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
            self.assertEqual(payload["task"], "objective-97-task-mim-arm-safe-home-20260404194149")
            self.assertEqual(payload["correlation_id"], "obj97-mim-arm-safe-home-20260404194149")
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["result_status"], "failed")
            self.assertTrue(payload["reconciliation"]["stale_result_rebound"])

    def test_reconcile_preserves_contract_valid_status_when_result_status_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-06T19:03:41Z",
                    "task_id": "objective-109-task-mim-arm-scan-pose-20260406190341",
                    "objective_id": "objective-109",
                    "correlation_id": "obj109-mim-arm-scan-pose-20260406190341",
                },
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {
                    "generated_at": "2026-04-06T18:51:04Z",
                    "source": "tod-mim-task-result-v1",
                    "request_id": "objective-109-task-mim-arm-scan-pose-20260406185022",
                    "task_id": "objective-109-task-mim-arm-scan-pose-20260406185022",
                    "status": "",
                    "result_status": "failed",
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads((shared_dir / "TOD_MIM_TASK_RESULT.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["request_id"], "objective-109-task-mim-arm-scan-pose-20260406190341")
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["result_status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)