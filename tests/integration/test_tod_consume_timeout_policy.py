import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REISSUE_SCRIPT = ROOT / "scripts" / "reissue_active_tod_task.sh"
POLICY_SCRIPT = ROOT / "scripts" / "watch_tod_consume_timeout_policy.sh"


def iso_now(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class TodConsumeTimeoutPolicyTest(unittest.TestCase):
    def test_reissue_active_task_skips_completed_promotion_ready_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            shared.mkdir(parents=True, exist_ok=True)

            request_path = shared / "MIM_TOD_TASK_REQUEST.latest.json"
            request_path.write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-300),
                        "emitted_at": iso_now(-300),
                        "sequence": 1,
                        "request_id": "objective-152-task-smoke-20260418214904",
                        "task_id": "objective-152-task-smoke-20260418214904",
                        "objective_id": "152",
                        "correlation_id": "obj152-smoke",
                        "title": "Completed request",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            original_request = request_path.read_text(encoding="utf-8")
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "state": "completed",
                        "task": {
                            "active_task_id": "objective-152-task-smoke-20260418214904",
                            "authoritative_task_id": "objective-152-task-smoke-20260418214904",
                            "objective_id": "152",
                        },
                        "gate": {"pass": True, "promotion_ready": True},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(REISSUE_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "REMOTE_PUBLISH": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("objective-152-task-smoke-20260418214904", completed.stdout)
            self.assertIn("already completed and gate-passing", completed.stderr)
            self.assertEqual(request_path.read_text(encoding="utf-8"), original_request)
            self.assertFalse((shared / "MIM_TO_TOD_TRIGGER.latest.json").exists())

    def test_reissue_active_task_refreshes_request_and_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            shared.mkdir(parents=True, exist_ok=True)

            request_path = shared / "MIM_TOD_TASK_REQUEST.latest.json"
            trigger_path = shared / "MIM_TO_TOD_TRIGGER.latest.json"
            request_path.write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-300),
                        "emitted_at": iso_now(-300),
                        "sequence": 1,
                        "request_id": "objective-97-task-policy-reissue",
                        "objective_id": "97",
                        "correlation_id": "obj97-policy-reissue",
                        "title": "Policy reissue test",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(REISSUE_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "REMOTE_PUBLISH": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            request = json.loads(request_path.read_text(encoding="utf-8"))
            trigger = json.loads(trigger_path.read_text(encoding="utf-8"))
            self.assertEqual(request["request_id"], "objective-97-task-policy-reissue")
            self.assertEqual(request["task_id"], "objective-97-task-policy-reissue")
            self.assertNotEqual(request["generated_at"], iso_now(-300))
            self.assertEqual(trigger["task_id"], "objective-97-task-policy-reissue")
            self.assertEqual(trigger["trigger"], "task_request_posted")
            self.assertEqual(trigger["artifact"], "MIM_TOD_TASK_REQUEST.latest.json")

    def test_reissue_active_task_refuses_objective_mismatch_against_canonical_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            shared.mkdir(parents=True, exist_ok=True)

            request_path = shared / "MIM_TOD_TASK_REQUEST.latest.json"
            request_path.write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-300),
                        "emitted_at": iso_now(-300),
                        "sequence": 1,
                        "request_id": "objective-75-task-3422",
                        "objective_id": "objective-75",
                        "correlation_id": "obj75-task3422",
                        "title": "Stale objective 75 request",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "97"}, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(REISSUE_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "REMOTE_PUBLISH": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("objective mismatch", completed.stderr)
            self.assertFalse((shared / "MIM_TO_TOD_TRIGGER.latest.json").exists())

            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["request_id"], "objective-75-task-3422")
            self.assertNotIn("task_id", request)
            self.assertEqual(request["objective_id"], "objective-75")

    def test_timeout_policy_auto_reissues_once_for_timeout_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            task_id = "objective-97-task-policy-timeout"
            request_path = shared / "MIM_TOD_TASK_REQUEST.latest.json"
            request_path.write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-600),
                        "emitted_at": iso_now(-600),
                        "sequence": 2,
                        "request_id": task_id,
                        "objective_id": "97",
                        "correlation_id": "obj97-policy-timeout",
                        "title": "Policy timeout test",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "task": {"active_task_id": task_id},
                        "blocking_reason_codes": ["consume_watch_timeout"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TOD_CONSUME_EVIDENCE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "task_id": task_id,
                        "watch": {
                            "started_at": iso_now(-900),
                            "window_seconds": 900,
                            "elapsed_seconds": 900,
                            "phase": "timeout",
                            "timed_out": True,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(POLICY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "REMOTE_PUBLISH": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            escalation = json.loads(
                (shared / "MIM_TOD_AUTO_ESCALATION.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            request = json.loads(request_path.read_text(encoding="utf-8"))
            trigger = json.loads(
                (shared / "MIM_TO_TOD_TRIGGER.latest.json").read_text(encoding="utf-8")
            )
            state = json.loads(
                (logs / "mim_tod_consume_timeout_policy.state.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(escalation["task_id"], task_id)
            self.assertTrue(bool(escalation["action"]["success"]))
            self.assertEqual(request["request_id"], task_id)
            self.assertEqual(request["task_id"], task_id)
            self.assertEqual(trigger["task_id"], task_id)
            self.assertEqual(state["handled_task_id"], task_id)
            self.assertEqual(state["last_action"], "auto_reissue_and_republish_task")

    def test_timeout_policy_submits_direct_execution_handoff_after_sustained_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            handoff_root = root / "handoff"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            task_id = "objective-97-task-policy-timeout"
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "task": {"active_task_id": task_id, "objective_id": "97"},
                        "blocking_reason_codes": ["consume_watch_timeout"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TOD_CONSUME_EVIDENCE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "task_id": task_id,
                        "watch": {
                            "started_at": iso_now(-180),
                            "window_seconds": 180,
                            "elapsed_seconds": 180,
                            "phase": "timeout",
                            "timed_out": True,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (logs / "mim_tod_consume_timeout_policy.state.json").write_text(
                json.dumps(
                    {
                        "handled_task_id": task_id,
                        "handled_watch_started_at": iso_now(-180),
                        "last_action": "auto_reissue_and_republish_task",
                        "last_result": "success",
                        "last_updated_at": iso_now(-120),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(POLICY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared),
                    "PYTHON_BIN": sys.executable,
                    "RUN_ONCE": "1",
                    "REMOTE_PUBLISH": "0",
                    "DIRECT_EXECUTION_TIMEOUT_SECONDS": "120",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            escalation = json.loads(
                (shared / "MIM_TOD_AUTO_ESCALATION.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            state = json.loads(
                (logs / "mim_tod_consume_timeout_policy.state.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                escalation["action"]["code"],
                "submit_codex_direct_execution_handoff",
            )
            self.assertEqual(state["direct_execution_task_id"], task_id)
            self.assertEqual(state["last_action"], "submit_codex_direct_execution_handoff")
            self.assertTrue(str(escalation["action"]["handoff_id"]).startswith("tod-silence-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
