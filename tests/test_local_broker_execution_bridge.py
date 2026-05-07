import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.local_broker_artifact_worker import consume_broker_request_artifact
from core.local_broker_execution_bridge import execute_interpreted_broker_tool_intent
from core.local_broker_result_interpreter import interpret_broker_result_artifact


ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT = ROOT / "scripts" / "run_handoff_intake_once.py"


class LocalBrokerExecutionBridgeTest(unittest.TestCase):
    def test_submit_handoff_payload_blocks_when_local_broker_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            shared_root.mkdir(parents=True, exist_ok=True)

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import asyncio, json; "
                        "from pathlib import Path; "
                        "from core.handoff_intake_service import submit_handoff_payload; "
                        f"handoff_root = Path({str(handoff_root)!r}); "
                        f"shared_root = Path({str(shared_root)!r}); "
                        "payload = {"
                        "'source': 'conversation-gateway', "
                        "'topic': 'Implement conversation handoff bridge', "
                        "'summary': 'Create one bounded implementation task from a live conversation request.', "
                        "'requested_outcome': 'Implement the next bounded handoff step for conversation routing.', "
                        "'constraints': ['Bounded implementation only'], "
                        "'next_bounded_steps': ['Prepare broker request artifact.'], "
                        "'status': 'pending'"
                        "}; "
                        "result = asyncio.run(submit_handoff_payload(payload, handoff_root=handoff_root, shared_root=shared_root)); "
                        "print(json.dumps(result))"
                    ),
                ],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "OPENAI_API_KEY": "",
                    "MIM_OPENAI_API_KEY": "",
                },
                check=True,
                capture_output=True,
                text=True,
            )

            intake_result = json.loads(result.stdout)
            self.assertEqual(
                intake_result["mode"],
                "codex_assisted_bounded_implementation",
            )
            self.assertIn(intake_result["status"], {"blocked", "completed"})
            self.assertTrue(str(intake_result["latest_result_summary"]).strip())
            self.assertTrue(Path(intake_result["status_path"]).exists())

    def test_codex_assisted_handoff_completes_full_broker_dry_run_for_get_current_objective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            shared_root.mkdir(parents=True, exist_ok=True)
            (shared_root / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "objective-220"}, indent=2) + "\n",
                encoding="utf-8",
            )
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-broker-dry-run-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement codex-assisted broker dry run",
                "summary": "Prepare one codex-assisted handoff that travels through the full local broker path.",
                "requested_outcome": "Implement one full local broker dry run for a read-only tool.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Prepare broker request artifact.", "Write one placeholder tool intent.", "Interpret and execute one tool intent."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "broker-dry-run.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
            self.assertEqual(intake_result["mode"], "codex_assisted_bounded_implementation")
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            worker_result = consume_broker_request_artifact(
                request_artifact_path=Path(broker_preparation["broker_request_artifact"]),
                tool_name="get_current_objective",
                arguments={},
            )
            interpretation = interpret_broker_result_artifact(
                result_artifact_path=Path(worker_result["result_artifact"])
            )
            self.assertEqual(
                interpretation["classification"],
                "single_bounded_tool_intent_placeholder",
            )
            self.assertEqual(interpretation["handoff_id"], artifact["handoff_id"])
            self.assertEqual(
                interpretation["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )

            bridge_result = execute_interpreted_broker_tool_intent(
                result_artifact_path=Path(worker_result["result_artifact"]),
                shared_root=shared_root,
            )

            self.assertEqual(bridge_result["status"], "completed")
            self.assertEqual(bridge_result["executed_tool_name"], "get_current_objective")
            result_payload = json.loads(Path(bridge_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(result_payload["task_id"], "handoff-task-handoff-broker-dry-run-001")
            self.assertEqual(
                result_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-broker-dry-run-001",
            )
            self.assertEqual(
                result_payload["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(result_payload["response"]["tool_call_intent"]["execution_state"], "executed")
            self.assertEqual(result_payload["response"]["executed_result"]["objective_id"], "objective-220")

    def test_executes_single_get_current_objective_intent_and_rewrites_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            shared_root.mkdir(parents=True, exist_ok=True)
            (shared_root / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "objective-210"}, indent=2) + "\n",
                encoding="utf-8",
            )
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-execution-bridge-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement local broker execution bridge",
                "summary": "Prepare one interpreted broker tool intent for current objective execution.",
                "requested_outcome": "Implement exactly one local broker tool execution bridge.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Execute one interpreted broker tool intent."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "execution-bridge.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
                tool_name="get_current_objective",
                arguments={},
            )
            bridge_result = execute_interpreted_broker_tool_intent(
                result_artifact_path=Path(worker_result["result_artifact"]),
                shared_root=shared_root,
            )

            self.assertEqual(bridge_result["status"], "completed")
            self.assertEqual(bridge_result["executed_tool_name"], "get_current_objective")
            result_payload = json.loads(Path(bridge_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(result_payload["task_id"], "handoff-task-handoff-execution-bridge-001")
            self.assertEqual(result_payload["linked_request_artifact"], broker_preparation["broker_request_artifact"])
            self.assertEqual(result_payload["task_linkage"]["task_id"], "handoff-task-handoff-execution-bridge-001")
            self.assertEqual(result_payload["response"]["status"], "tool_executed")
            self.assertEqual(result_payload["response"]["tool_call_intent"]["execution_state"], "executed")
            self.assertEqual(result_payload["response"]["tool_call_intent"]["executed_tool_name"], "get_current_objective")
            self.assertEqual(result_payload["response"]["executed_result"]["objective_id"], "objective-210")

    def test_executes_single_get_tod_status_intent_and_rewrites_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            shared_root.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-execution-bridge-status-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement local broker status execution bridge",
                "summary": "Prepare one interpreted broker tool intent for TOD status execution.",
                "requested_outcome": "Implement exactly one local broker TOD status execution bridge.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Execute one interpreted broker TOD status tool intent."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "execution-bridge-status.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
                tool_name="get_tod_status",
                arguments={},
            )
            bridge_result = execute_interpreted_broker_tool_intent(
                result_artifact_path=Path(worker_result["result_artifact"]),
                shared_root=shared_root,
            )

            self.assertEqual(bridge_result["status"], "completed")
            self.assertEqual(bridge_result["executed_tool_name"], "get_tod_status")
            result_payload = json.loads(Path(bridge_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(result_payload["task_id"], "handoff-task-handoff-execution-bridge-status-001")
            self.assertEqual(result_payload["linked_request_artifact"], broker_preparation["broker_request_artifact"])
            self.assertEqual(result_payload["task_linkage"]["task_id"], "handoff-task-handoff-execution-bridge-status-001")
            self.assertEqual(result_payload["response"]["status"], "tool_executed")
            self.assertEqual(result_payload["response"]["tool_call_intent"]["execution_state"], "executed")
            self.assertEqual(result_payload["response"]["tool_call_intent"]["executed_tool_name"], "get_tod_status")
            self.assertEqual(result_payload["response"]["executed_result"]["action_name"], "tod_status_check")
            self.assertEqual(result_payload["response"]["executed_result"]["result_status"], "succeeded")
            tod_result_payload = json.loads(
                Path(result_payload["response"]["executed_result"]["result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(tod_result_payload["dispatch_kind"], "bounded_status_request")


if __name__ == "__main__":
    unittest.main()