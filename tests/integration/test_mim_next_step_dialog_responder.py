import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESPONDER_SCRIPT = ROOT / "scripts" / "watch_mim_next_step_dialog_responder.sh"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MimNextStepDialogResponderTest(unittest.TestCase):
    def test_appends_handoff_response_for_tod_ui_open_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            dialog = shared / "dialog"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            dialog.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            session_id = "tod-ui-copilot-20260504t001341z-278c403c"
            session_path = dialog / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"
            (dialog / "MIM_TOD_DIALOG.sessions.latest.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": session_id,
                                "status": "open",
                                "timed_out": False,
                                "message_count": 1,
                                "updated_at": iso_now(),
                                "session_path": str(session_path),
                                "open_reply": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "handoff_request",
                                    "summary": "TOD UI requests Copilot handoff for objective-2900-task-7117.",
                                    "timestamp": iso_now(),
                                },
                                "last_message": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "handoff_request",
                                    "summary": "TOD UI requests Copilot handoff for objective-2900-task-7117.",
                                    "task_id": "objective-2900-task-7117",
                                    "correlation_id": "",
                                    "timestamp": iso_now(),
                                },
                                "awaiting_reply_to": "MIM",
                                "reply_to": "",
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            request = {
                "message_type": "handoff_request",
                "intent": "tod_ui_copilot_handoff",
                "session_id": session_id,
                "turn_id": 1,
                "generated_at": iso_now(),
                "from": "TOD",
                "to": "MIM",
                "task_id": "objective-2900-task-7117",
                "payload": {
                    "task_id": "objective-2900-task-7117",
                    "request_id": "objective-2900-task-7117",
                    "id_kind": "bridge_request_id",
                    "execution_lane": "tod_bridge_request",
                    "objective_id": "2912",
                    "run_id": "tod-ui-copilot-20260504T001341Z-278c403c",
                    "findings": [
                        {
                            "finding_id": "objective-2912-finding-001",
                            "summary": "Continue objective 2912 from the newer authoritative request.",
                            "owner_workspace": "TOD",
                            "action_type": "inquire",
                            "risk": "low",
                            "cross_system": True,
                        }
                    ],
                    "response_contract": {
                        "required_fields": ["summary", "finding_positions"]
                    },
                },
            }
            session_path.write_text(json.dumps(request) + "\n", encoding="utf-8")

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                '{"task":{"active_task_id":"objective-2912-task-008"},"state":"completed","gate":{"pass":true,"promotion_ready":true},"blocking_reason_codes":[]}',
                encoding="utf-8",
            )
            (shared / "MIM_SYSTEM_ALERTS.latest.json").write_text(
                '{"active":false,"highest_severity":"none"}',
                encoding="utf-8",
            )
            (shared / "TOD_CATCHUP_GATE.latest.json").write_text(
                '{"gate_pass":true,"promotion_ready":true}',
                encoding="utf-8",
            )
            (shared / "mim_arm_control_readiness.latest.json").write_text(
                '{"operator_approval_required":true,"tod_execution_allowed":true}',
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "DIALOG_ROOT": str(dialog),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rows = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            response = rows[-1]
            self.assertEqual(response["message_type"], "handoff_response")
            self.assertEqual(response["intent"], "next_step_consensus")
            self.assertEqual(response["session_id"], session_id)

    def test_appends_handoff_response_for_live_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            dialog = shared / "dialog"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            dialog.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            session_id = "next-step-objective-97-authority-cleanup-tod-codex-run-objective-97-authority-cleanup-20260401t163514z"
            session_path = dialog / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"
            (dialog / "MIM_TOD_DIALOG.sessions.latest.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": session_id,
                                "status": "awaiting_reply",
                                "open_reply": {"to": "MIM"},
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            request = {
                "message_type": "handoff_request",
                "intent": "next_step_consensus",
                "session_id": session_id,
                "turn": 7,
                "generated_at": iso_now(),
                "payload": {
                    "task_id": "objective-97-authority-cleanup",
                    "request_id": "objective-97-authority-cleanup-request-001",
                    "id_kind": "bridge_request_id",
                    "execution_lane": "tod_bridge_request",
                    "objective_id": "97",
                    "run_id": "tod-codex-run-objective-97-authority-cleanup-20260401T163514Z",
                    "findings": [
                        {
                            "finding_id": "objective-97-authority-cleanup-finding-001",
                            "summary": "Run canonical-only validation pass",
                            "owner_workspace": "TOD",
                            "action_type": "inquire",
                            "risk": "low",
                            "cross_system": True,
                        },
                        {
                            "finding_id": "objective-97-authority-cleanup-finding-002",
                            "summary": "Retire remaining live aliases after validation",
                            "owner_workspace": "TOD",
                            "action_type": "inquire",
                            "risk": "low",
                            "cross_system": True,
                        },
                    ],
                    "response_contract": {
                        "required_fields": ["summary", "finding_positions"]
                    },
                },
            }
            reminder = {
                "type": "reminder",
                "session_id": session_id,
                "generated_at": iso_now(),
                "summary": "Reminder: waiting on MIM response.",
            }
            session_path.write_text(
                json.dumps(request) + "\n" + json.dumps(reminder) + "\n",
                encoding="utf-8",
            )

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                '{"task":{"active_task_id":"objective-97-task"},"state":"completed","gate":{"pass":true,"promotion_ready":true},"blocking_reason_codes":[]}',
                encoding="utf-8",
            )
            (shared / "MIM_SYSTEM_ALERTS.latest.json").write_text(
                '{"active":false,"highest_severity":"none"}',
                encoding="utf-8",
            )
            (shared / "TOD_CATCHUP_GATE.latest.json").write_text(
                '{"gate_pass":true,"promotion_ready":true}',
                encoding="utf-8",
            )
            (shared / "mim_arm_control_readiness.latest.json").write_text(
                '{"operator_approval_required":true,"tod_execution_allowed":true}',
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "DIALOG_ROOT": str(dialog),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rows = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            response = rows[-1]
            self.assertEqual(response["message_type"], "handoff_response")
            self.assertEqual(response["intent"], "next_step_consensus")
            self.assertEqual(response["session_id"], session_id)
            self.assertEqual(response["reply_to_turn"], 7)
            self.assertEqual(response["task_id"], "objective-97-authority-cleanup")
            self.assertEqual(response["request_id"], "objective-97-authority-cleanup-request-001")
            self.assertEqual(response["execution_id"], "objective-97-authority-cleanup-request-001")
            self.assertEqual(response["id_kind"], "bridge_request_id")
            self.assertEqual(response["execution_lane"], "tod_bridge_request")
            self.assertTrue(str(response.get("summary", "")).strip(), response)
            self.assertEqual(len(response.get("finding_positions", [])), 2, response)
            self.assertEqual(response["payload"]["summary"], response["summary"])
            self.assertEqual(
                response["payload"]["finding_positions"][0]["finding_id"],
                "objective-97-authority-cleanup-finding-001",
            )
            self.assertIn("decision", response["payload"]["finding_positions"][0])
            self.assertIn("reason", response["payload"]["finding_positions"][0])
            self.assertIn("confidence", response["payload"]["finding_positions"][0])
            self.assertIn("local_blockers", response["payload"]["finding_positions"][0])

            adjudication = json.loads((shared / "mim_next_step_adjudication.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(adjudication.get("items", [])), 2, adjudication)
            next_steps = json.loads((shared / "mim_codex_next_steps.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(next_steps["task_id"], "objective-97-authority-cleanup")
            self.assertEqual(next_steps["request_id"], "objective-97-authority-cleanup-request-001")
            self.assertEqual(next_steps["execution_id"], "objective-97-authority-cleanup-request-001")
            self.assertEqual(next_steps["id_kind"], "bridge_request_id")
            self.assertEqual(next_steps["execution_lane"], "tod_bridge_request")

            status = json.loads((logs / "mim_next_step_dialog_responder.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "response_appended")
            self.assertEqual(int(status["processed_count"]), 1)

    def test_appends_status_response_and_closes_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            dialog = shared / "dialog"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            dialog.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            session_id = "tod-operator-chat-explain-bridge-status-test"
            session_path = dialog / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"
            index_path = dialog / "MIM_TOD_DIALOG.sessions.latest.json"
            index_path.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": session_id,
                                "status": "awaiting_reply",
                                "timed_out": False,
                                "message_count": 1,
                                "updated_at": iso_now(),
                                "session_path": str(session_path),
                                "open_reply": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "status_request",
                                    "summary": "TOD operator chat asks MIM: What is the current bridge mismatch?",
                                    "timestamp": iso_now(),
                                },
                                "last_message": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "status_request",
                                    "summary": "TOD operator chat asks MIM: What is the current bridge mismatch?",
                                    "task_id": "objective-115-task-mim-arm-capture-frame-20260407033825",
                                    "correlation_id": "",
                                    "timestamp": iso_now(),
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "turn_id": 1,
                        "timestamp": iso_now(),
                        "from": "TOD",
                        "to": "MIM",
                        "message_type": "status_request",
                        "intent": "explain_bridge_status",
                        "task_id": "objective-115-task-mim-arm-capture-frame-20260407033825",
                        "summary": "TOD operator chat asks MIM: What is the current bridge mismatch?",
                        "payload": {
                            "objective_id": "objective-115",
                            "request_kind": "operator_chat",
                            "query": "What is the current bridge mismatch?",
                            "intent": "explain_bridge_status",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            (shared / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "objective_alignment": {"status": "in_sync", "mim_objective_active": "141"},
                        "failure_signals": ["listener_task_request_missing"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "task": {"active_task_id": "coordination-objective-141-publication_surface_divergence"},
                        "state": "idle_blocked",
                        "state_reason": "task_stream_drift",
                        "blocking_reason_codes": ["task_stream_drift", "task_ack_request_mismatch"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "selected_action": {
                            "code": "stabilize_task_stream",
                            "detail": "Stop rotating publishers from overwriting the active task packet and reissue one authoritative task_id.",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "141"}) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "DIALOG_ROOT": str(dialog),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rows = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            response = rows[-1]
            self.assertEqual(response["message_type"], "status_response")
            self.assertEqual(response["intent"], "explain_bridge_status")
            self.assertEqual(response["reply_to_turn"], 1)
            self.assertIn("stale task-stream artifacts", response["summary"])
            self.assertEqual(response["payload"]["objective_id"], "141")
            self.assertIn("task_stream_drift", response["payload"]["flags"])

            updated_index = json.loads(index_path.read_text(encoding="utf-8"))
            session_entry = updated_index["sessions"][0]
            self.assertEqual(session_entry["status"], "replied")
            self.assertEqual(session_entry["open_reply"], {})
            self.assertEqual(session_entry["last_message"]["message_type"], "status_response")

            latest_snapshot = json.loads(
                (dialog / f"MIM_TOD_DIALOG.session-{session_id}.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(latest_snapshot["status"], "replied")
            self.assertEqual(latest_snapshot["open_reply"], {})
            self.assertEqual(
                latest_snapshot["last_message"]["message_type"], "status_response"
            )

            status = json.loads((logs / "mim_next_step_dialog_responder.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "response_appended")
            self.assertEqual(int(status["processed_count"]), 1)

    def test_command_panel_status_response_honors_requested_objective_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            dialog = shared / "dialog"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            dialog.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            session_id = "mim-command-152-objective-anchor-test"
            session_path = dialog / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"
            index_path = dialog / "MIM_TOD_DIALOG.sessions.latest.json"
            index_path.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": session_id,
                                "status": "awaiting_reply",
                                "timed_out": False,
                                "message_count": 1,
                                "updated_at": iso_now(),
                                "session_path": str(session_path),
                                "open_reply": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "status_request",
                                    "summary": "Human asked MIM for summarize_status.",
                                    "timestamp": iso_now(),
                                },
                                "last_message": {
                                    "turn_id": 1,
                                    "from": "TOD",
                                    "to": "MIM",
                                    "message_type": "status_request",
                                    "summary": "Human asked MIM for summarize_status.",
                                    "task_id": "objective-152-task-mim-arm-safe-home-20260408160030",
                                    "correlation_id": "",
                                    "timestamp": iso_now(),
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "turn_id": 1,
                        "timestamp": iso_now(),
                        "from": "TOD",
                        "to": "MIM",
                        "message_type": "status_request",
                        "intent": "summarize_status",
                        "task_id": "objective-152-task-mim-arm-safe-home-20260408160030",
                        "summary": "Human asked MIM for summarize_status.",
                        "payload": {
                            "operator_text": "Summarize the current operating state for the human.",
                            "requested_response_mode": "bounded_action_guidance",
                            "request_id": "mim-command-summarize-status-20260410T000000Z-test152",
                            "objective_id": "152",
                            "request_kind": "mim_command",
                            "source_surface": "tod_browser_console",
                            "intent": "summarize_status",
                            "window_minutes": 10,
                            "primary_source": "MIM",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            (shared / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "objective_alignment": {"status": "in_sync", "mim_objective_active": "170"},
                        "failure_signals": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "task": {
                            "active_task_id": "objective-152-task-mim-arm-safe-home-20260408160030"
                        },
                        "state": "completed",
                        "state_reason": "task_result_current",
                        "blocking_reason_codes": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "selected_action": {
                            "code": "monitor_only",
                            "detail": "No blocking action selected; continue monitoring.",
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "170"}) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "DIALOG_ROOT": str(dialog),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rows = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            response = rows[-1]
            self.assertEqual(response["message_type"], "status_response")
            self.assertEqual(response["intent"], "summarize_status")
            self.assertEqual(response["reply_to_turn"], 1)
            self.assertEqual(response["request_id"], "mim-command-summarize-status-20260410T000000Z-test152")
            self.assertEqual(response["execution_id"], "mim-command-summarize-status-20260410T000000Z-test152")
            self.assertEqual(response["objective_id"], "152")
            self.assertEqual(response["payload"]["objective_id"], "152")
            self.assertIn("objective 152", response["summary"])

            updated_index = json.loads(index_path.read_text(encoding="utf-8"))
            session_entry = updated_index["sessions"][0]
            self.assertEqual(session_entry["status"], "replied")
            self.assertEqual(session_entry["last_message"]["message_type"], "status_response")

    def test_does_not_duplicate_existing_handoff_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            dialog = shared / "dialog"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            dialog.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            session_id = "next-step-objective-97-authority-cleanup-tod-codex-run-objective-97-authority-cleanup-20260401t163514z"
            session_path = dialog / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"
            (dialog / "MIM_TOD_DIALOG.sessions.latest.json").write_text(
                json.dumps(
                    {
                        "sessions": [
                            {
                                "session_id": session_id,
                                "status": "awaiting_reply",
                                "open_reply": {"to": "MIM"},
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            session_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "message_type": "handoff_request",
                                "intent": "next_step_consensus",
                                "session_id": session_id,
                                "generated_at": iso_now(),
                                "payload": {
                                    "findings": [
                                        {
                                            "finding_id": "objective-97-authority-cleanup-finding-001",
                                            "summary": "Run canonical-only validation pass",
                                        }
                                    ]
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "message_type": "handoff_response",
                                "session_id": session_id,
                                "generated_at": iso_now(),
                                "summary": "Existing response.",
                                "finding_positions": [{"finding_id": "objective-97-authority-cleanup-finding-001", "decision": "approve", "reason": "Existing response.", "confidence": 0.9, "local_blockers": []}],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "DIALOG_ROOT": str(dialog),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rows = [line for line in session_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 2, rows)

            status = json.loads((logs / "mim_next_step_dialog_responder.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "idle")
            self.assertEqual(int(status["processed_count"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)