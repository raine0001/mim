import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.local_broker_artifact_worker import consume_broker_request_artifact


ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT = ROOT / "scripts" / "run_handoff_intake_once.py"


class LocalBrokerArtifactWorkerTest(unittest.TestCase):
    def test_worker_consumes_request_and_rewrites_existing_result_with_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-codex-worker-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement a bounded broker worker",
                "summary": "Prepare a broker request artifact and then consume it locally.",
                "requested_outcome": "Write a placeholder broker result without any live broker client.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Consume the broker request artifact once."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "codex-worker.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            worker_result = consume_broker_request_artifact(
                request_artifact_path=Path(broker_preparation["broker_request_artifact"])
            )

            self.assertEqual(worker_result["status"], "completed")
            result_payload = json.loads(Path(worker_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(result_payload["task_id"], "handoff-task-handoff-codex-worker-001")
            self.assertEqual(
                result_payload["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(result_payload["response"]["status"], "placeholder_written")
            self.assertIn("model_response_placeholder", result_payload["response"])
            self.assertIn("tool_call_placeholder", result_payload["response"])
            self.assertIsNone(result_payload["response"]["tool_call_intent"])
            self.assertEqual(
                result_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-codex-worker-001",
            )

    def test_worker_writes_single_bounded_tool_intent_placeholder_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-codex-worker-intent-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Prepare a bounded tool intent placeholder",
                "summary": "Produce a single placeholder tool intent without executing it.",
                "requested_outcome": "Write a bounded placeholder tool call intent only.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Write one bounded tool intent placeholder."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "codex-worker-intent.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            worker_result = consume_broker_request_artifact(
                request_artifact_path=Path(broker_preparation["broker_request_artifact"]),
                tool_name="run_bounded_action",
                arguments={"action_name": "tod_status_check"},
            )

            self.assertEqual(worker_result["status"], "completed")
            result_payload = json.loads(Path(worker_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(
                result_payload["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(
                result_payload["response"]["tool_call_placeholder"]["tool_name"],
                "run_bounded_action",
            )
            self.assertEqual(
                result_payload["response"]["tool_call_placeholder"]["arguments"],
                {"action_name": "tod_status_check"},
            )
            self.assertEqual(
                result_payload["response"]["tool_call_intent"]["execution_state"],
                "not_executed",
            )
            self.assertEqual(
                result_payload["response"]["tool_call_intent"]["arguments"],
                {"action_name": "tod_status_check"},
            )
            self.assertEqual(
                result_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-codex-worker-intent-001",
            )


if __name__ == "__main__":
    unittest.main()