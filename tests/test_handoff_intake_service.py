import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_handoff_intake_once.py"
WATCHER_SCRIPT = ROOT / "scripts" / "watch_handoff_inbox.py"
WATCHER_GUARD_SCRIPT = ROOT / "scripts" / "check_handoff_watcher_status.py"
WATCHER_RECOVERY_SCRIPT = ROOT / "scripts" / "print_handoff_watcher_recovery.py"
WATCHER_SUMMARY_SCRIPT = ROOT / "scripts" / "print_handoff_watcher_supervision_summary.py"
WATCHER_SUPERVISOR_SCRIPT = ROOT / "scripts" / "watch_handoff_watcher_supervisor.py"
FIXTURES = ROOT / "tests" / "fixtures" / "handoff"


class HandoffIntakeServiceTest(unittest.TestCase):
    @staticmethod
    def _run_ingest_once(*, handoff_root: Path, shared_root: Path) -> dict:
        import asyncio

        from core.handoff_intake_service import ingest_one_handoff_artifact

        return asyncio.run(
            ingest_one_handoff_artifact(
                handoff_root=handoff_root,
                shared_root=shared_root,
            )
        )

    def _write_fake_systemctl(self, *, root: Path, watcher_status_path: Path, active: bool) -> tuple[Path, Path]:
        state_path = root / "fake-systemctl-state.json"
        state_path.write_text(
            json.dumps({"active": active, "restart_count": 0}, indent=2) + "\n",
            encoding="utf-8",
        )
        systemctl_path = root / "fake-systemctl.py"
        systemctl_path.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


state_path = Path(os.environ[\"FAKE_SYSTEMCTL_STATE_PATH\"])
watcher_status_path = Path(os.environ[\"FAKE_WATCHER_STATUS_PATH\"])


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(\"+00:00\", \"Z\")


def load_state() -> dict:
    if not state_path.exists():
        return {\"active\": False, \"restart_count\": 0}
    return json.loads(state_path.read_text(encoding=\"utf-8\"))


def save_state(payload: dict) -> None:
    state_path.write_text(json.dumps(payload, indent=2) + \"\\n\", encoding=\"utf-8\")


def refresh_watcher_status() -> None:
    handoff_root = watcher_status_path.parent.parent
    watcher_status_path.parent.mkdir(parents=True, exist_ok=True)
    watcher_status_path.write_text(
        json.dumps(
            {
                \"artifact_type\": \"mim-handoff-watcher-status-v1\",
                \"updated_at\": utc_now(),
                \"lifecycle_state\": \"starting\",
                \"poll_interval_seconds\": 1.0,
                \"stale_after_seconds\": 30,
                \"stale\": False,
                \"stale_reason\": \"\",
                \"poll_count\": 1,
                \"processed_count\": 0,
                \"handoff_root\": str(handoff_root),
                \"last_result\": {
                    \"status\": \"idle\",
                    \"mode\": \"\",
                    \"handoff_id\": \"\",
                    \"reason\": \"watcher_restarted\",
                },
            },
            indent=2,
        )
        + \"\\n\",
        encoding=\"utf-8\",
    )


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != \"--user\"]
    state = load_state()
    if not args:
        return 1
    command = args[0]
    if command == \"is-active\":
        return 0 if state.get(\"active\") else 3
    if command in {\"restart\", \"start\"}:
        state[\"active\"] = True
        state[\"restart_count\"] = int(state.get(\"restart_count\", 0)) + 1
        state[\"last_action\"] = command
        save_state(state)
        refresh_watcher_status()
        return 0
    return 1


if __name__ == \"__main__\":
    raise SystemExit(main())
""",
            encoding="utf-8",
        )
        systemctl_path.chmod(0o755)
        return systemctl_path, state_path

    def _run_cli(self, *, handoff_root: Path, shared_root: Path, extra_env: dict[str, str] | None = None) -> dict:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env={
                **os.environ,
                "MIM_HANDOFF_ROOT": str(handoff_root),
                "MIM_SHARED_ROOT": str(shared_root),
                **(extra_env or {}),
            },
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_ingests_sample_handoff_and_selects_bounded_tod_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            fixture = json.loads((FIXTURES / "sample_tod_recent_changes_handoff.json").read_text(encoding="utf-8"))
            artifact_path = inbox / "sample.json"
            artifact_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")

            with patch("core.handoff_intake_service.live_openai_broker_configured", return_value=False), patch(
                "core.local_broker_boundary.live_openai_broker_configured", return_value=False
            ):
                result = self._run_ingest_once(
                    handoff_root=handoff_root,
                    shared_root=shared_root,
                )

            self.assertEqual(result["mode"], "bounded_tod_dispatch")
            self.assertEqual(result["status"], "completed")
            self.assertTrue((handoff_root / "done" / "sample.json").exists())
            status_payload = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["handoff_id"], fixture["handoff_id"])
            self.assertEqual(status_payload["execution_owner"], "tod")
            self.assertEqual(status_payload["assistance_mode"], "bounded_tod_dispatch")
            self.assertEqual(status_payload["latest_result"]["selected_mode"], "bounded_tod_dispatch")
            task_payload = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))
            self.assertEqual(task_payload["handoff_id"], fixture["handoff_id"])
            self.assertEqual(task_payload["execution_owner"], "tod")
            self.assertEqual(
                status_payload["latest_result"]["dispatch"]["dispatch_kind"],
                "bounded_recent_changes_request",
            )
            result_artifact = json.loads((shared_root / "TOD_MIM_TASK_RESULT.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(result_artifact["request_id"], fixture["handoff_id"])
            self.assertEqual(result_artifact["dispatch_kind"], "bounded_recent_changes_request")

    def test_selects_direct_mim_answer_when_no_tod_action_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-direct-answer-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Need a quick explanation",
                "summary": "Explain the next bounded step without dispatching TOD.",
                "requested_outcome": "Answer directly with a short local explanation.",
                "constraints": ["Local only"],
                "next_bounded_steps": ["State the next bounded step clearly."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "direct.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            result = self._run_cli(
                handoff_root=handoff_root,
                shared_root=shared_root,
                extra_env={
                    "OPENAI_API_KEY": "",
                    "MIM_OPENAI_API_KEY": "",
                },
            )

            self.assertEqual(result["mode"], "direct_mim_answer")
            self.assertEqual(result["status"], "completed")
            status_payload = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["execution_owner"], "mim")
            self.assertEqual(status_payload["assistance_mode"], "local_direct_answer")
            self.assertIn("Requested outcome:", status_payload["latest_result"]["summary"])

    def test_codex_assisted_handoff_blocks_when_local_broker_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-codex-broker-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement a bounded local broker prep",
                "summary": "Prepare a bounded local watcher and broker interface without a live OpenAI call.",
                "requested_outcome": "Implement the smallest local broker-ready path for future API assistance.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Prepare one local broker boundary.", "Keep watcher single-threaded."],
                "bounded_actions_allowed": [],
                "dispatch_contract": {
                    "objective_id": 12,
                    "task_id": 34,
                    "execution_scope": "bounded_development",
                    "start_now": True,
                    "human_prompt_required": False,
                },
                "status": "pending",
            }
            (inbox / "codex.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            with patch("core.handoff_intake_service.live_openai_broker_configured", return_value=False), patch(
                "core.local_broker_boundary.live_openai_broker_configured", return_value=False
            ):
                result = self._run_ingest_once(
                    handoff_root=handoff_root,
                    shared_root=shared_root,
                )

            self.assertEqual(result["mode"], "codex_assisted_bounded_implementation")
            self.assertEqual(result["status"], "blocked")
            status_payload = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["status"], "blocked")
            self.assertEqual(status_payload["active_step_id"], "blocked_local_broker_unavailable")
            self.assertEqual(status_payload["result_authority"], "local_broker_unavailable")
            broker_preparation = status_payload["latest_result"]["broker_preparation"]
            self.assertEqual(broker_preparation["status"], "prepared")
            task_payload = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))
            self.assertEqual(task_payload["dispatch_contract"]["objective_id"], 12)
            self.assertTrue(task_payload["dispatch_contract"]["start_now"])
            self.assertEqual(
                broker_preparation["tool_names"],
                [
                    "get_current_objective",
                    "get_tod_status",
                    "get_recent_changes",
                    "get_current_warnings",
                    "get_bridge_warning_explanation",
                    "get_bridge_warning_next_step",
                    "list_bounded_actions",
                    "run_bounded_action",
                ],
            )
            self.assertEqual(
                broker_preparation["broker_response"]["status"],
                "not_configured",
            )
            broker_request_payload = json.loads(
                Path(broker_preparation["broker_request_artifact"]).read_text(encoding="utf-8")
            )
            broker_result_payload = json.loads(
                Path(broker_preparation["broker_result_artifact"]).read_text(encoding="utf-8")
            )
            self.assertEqual(broker_request_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(
                broker_request_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-codex-broker-001",
            )
            self.assertEqual(
                broker_request_payload["tool_names"],
                broker_preparation["tool_names"],
            )
            self.assertEqual(broker_result_payload["response_kind"], "not_configured")
            self.assertEqual(
                broker_result_payload["response"]["reason"],
                "local_broker_client_not_configured",
            )
            self.assertEqual(status_payload["execution_owner"], "codex")
            task_payload = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))
            self.assertIn("broker_preparation", task_payload)
            self.assertEqual(
                task_payload["broker_preparation"]["broker_result_artifact"],
                broker_preparation["broker_result_artifact"],
            )

    def test_codex_assisted_handoff_automatically_runs_live_broker_when_configured(self) -> None:
        class _FakeOpenAIResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return self._payload

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-codex-auto-live-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement a bounded live broker response",
                "summary": "Automatically invoke the existing live broker worker during intake.",
                "requested_outcome": "Write one real model response into the existing broker result artifact during codex-assisted intake.",
                "constraints": ["No shell access", "Response only"],
                "next_bounded_steps": ["Invoke the live broker worker automatically once."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "codex-auto-live.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
            observed_requests: list[dict[str, object]] = []

            def fake_urlopen(request, timeout=0):
                observed_requests.append(json.loads(request.data.decode("utf-8")))
                return _FakeOpenAIResponse(
                    {
                        "id": "chatcmpl-auto-live-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "Automatic live broker response: codex-assisted handoff is queued with a real model reply.",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 110, "completion_tokens": 16, "total_tokens": 126},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    result = self._run_ingest_once(
                        handoff_root=handoff_root,
                        shared_root=shared_root,
                    )

            self.assertEqual(result["mode"], "codex_assisted_bounded_implementation")
            self.assertEqual(result["status"], "queued")
            status_payload = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["status"], "queued")
            self.assertEqual(status_payload["active_step_id"], "queue_codex_bounded_implementation")
            self.assertEqual(status_payload["result_authority"], "local_task_queue")
            broker_preparation = status_payload["latest_result"]["broker_preparation"]
            self.assertEqual(broker_preparation["broker_response"]["status"], "completed")
            self.assertEqual(broker_preparation["automatic_live_response"]["status"], "completed")
            self.assertEqual(
                broker_preparation["automatic_live_interpretation"]["status"],
                "completed",
            )
            self.assertEqual(
                broker_preparation["automatic_live_interpretation"]["classification"],
                "model_response_text",
            )
            broker_result_payload = json.loads(
                Path(broker_preparation["broker_result_artifact"]).read_text(encoding="utf-8")
            )
            self.assertEqual(broker_result_payload["response_kind"], "model_response")
            self.assertEqual(
                broker_result_payload["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(
                broker_result_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-codex-auto-live-001",
            )
            self.assertEqual(
                broker_result_payload["response"]["output_text"],
                "Automatic live broker response: codex-assisted handoff is queued with a real model reply.",
            )
            self.assertIsNone(broker_result_payload["response"]["tool_call_intent"])
            self.assertIsNone(broker_result_payload["response"]["executed_result"])
            self.assertEqual(broker_result_payload["response"]["model_response"]["provider"], "openai")
            self.assertEqual(
                broker_result_payload["interpretation"]["classification"],
                "model_response_text",
            )
            self.assertEqual(
                broker_result_payload["interpretation"]["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            task_payload = json.loads(Path(result["task_path"]).read_text(encoding="utf-8"))
            self.assertEqual(task_payload["state"], "queued")
            self.assertEqual(task_payload["result_authority"], "local_task_queue")
            self.assertIn("Queued bounded implementation task", status_payload["latest_result"]["summary"])
            self.assertEqual(len(observed_requests), 2)

    def test_invalid_handoff_is_moved_to_failed_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            invalid_artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-invalid-001",
                "created_at": "not-a-timestamp",
                "source": "strategy-conversation",
                "topic": "Broken handoff",
                "summary": "Missing required list fields.",
                "requested_outcome": "Should fail validation.",
                "status": "pending",
            }
            (inbox / "invalid.json").write_text(json.dumps(invalid_artifact, indent=2) + "\n", encoding="utf-8")

            result = self._run_cli(
                handoff_root=handoff_root,
                shared_root=shared_root,
            )

            self.assertEqual(result["mode"], "blocked")
            self.assertEqual(result["status"], "failed")
            self.assertTrue((handoff_root / "failed" / "invalid.json").exists())
            status_payload = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["status"], "failed")
            self.assertIn("validation_errors", status_payload["latest_result"])

    def test_watcher_processes_newly_dropped_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)

            watcher = subprocess.Popen(
                [sys.executable, str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "OPENAI_API_KEY": "",
                    "MIM_OPENAI_API_KEY": "",
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                    "MIM_HANDOFF_POLL_INTERVAL_SECONDS": "0.1",
                    "MIM_HANDOFF_MAX_POLLS": "40",
                    "MIM_HANDOFF_EXIT_AFTER_PROCESSED": "1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                fixture = json.loads((FIXTURES / "sample_tod_recent_changes_handoff.json").read_text(encoding="utf-8"))
                artifact_path = inbox / "watcher-sample.json"
                artifact_path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")

                stdout, stderr = watcher.communicate(timeout=10)
            finally:
                if watcher.poll() is None:
                    watcher.kill()
                    watcher.wait(timeout=5)

            if watcher.returncode != 0:
                raise AssertionError(f"watcher exited with {watcher.returncode}: {stderr}")

            summary = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["processed_count"], 1)
            self.assertEqual(summary["last_result"]["mode"], "bounded_tod_dispatch")
            self.assertEqual(summary["last_result"]["status"], "completed")
            self.assertTrue((handoff_root / "done" / "watcher-sample.json").exists())
            watcher_status = json.loads(Path(summary["watcher_status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(watcher_status["lifecycle_state"], "completed")
            self.assertEqual(watcher_status["processed_count"], 1)
            self.assertEqual(watcher_status["last_result"]["mode"], "bounded_tod_dispatch")
            status_payload = json.loads(Path(summary["last_result"]["status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["execution_owner"], "tod")

    def test_watcher_processes_codex_handoff_with_broker_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)

            watcher = subprocess.Popen(
                [sys.executable, str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                    "MIM_HANDOFF_POLL_INTERVAL_SECONDS": "0.1",
                    "MIM_HANDOFF_MAX_POLLS": "40",
                    "MIM_HANDOFF_EXIT_AFTER_PROCESSED": "1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                artifact = {
                    "schema_version": "mim-handoff-input-v1",
                    "handoff_id": "handoff-codex-watcher-001",
                    "created_at": "2026-04-13T12:00:00Z",
                    "source": "strategy-conversation",
                    "topic": "Implement a bounded watcher prep",
                    "summary": "Prepare local broker use through the watcher path.",
                    "requested_outcome": "Implement the watcher and broker prep boundary locally.",
                    "constraints": ["No UI", "No arbitrary commands"],
                    "next_bounded_steps": ["Prepare the broker boundary."],
                    "bounded_actions_allowed": [],
                    "status": "pending",
                }
                artifact_path = inbox / "watcher-codex.json"
                artifact_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

                stdout, stderr = watcher.communicate(timeout=10)
            finally:
                if watcher.poll() is None:
                    watcher.kill()
                    watcher.wait(timeout=5)

            if watcher.returncode != 0:
                raise AssertionError(f"watcher exited with {watcher.returncode}: {stderr}")

            summary = json.loads(stdout.strip().splitlines()[-1])
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["processed_count"], 1)
            self.assertEqual(summary["last_result"]["mode"], "codex_assisted_bounded_implementation")
            status_payload = json.loads(Path(summary["last_result"]["status_path"]).read_text(encoding="utf-8"))
            self.assertIn(summary["last_result"]["status"], {"blocked", "queued"})
            broker_preparation = status_payload["latest_result"]["broker_preparation"]
            if "automatic_live_response" in broker_preparation:
                self.assertEqual(status_payload["status"], "queued")
                self.assertEqual(status_payload["active_step_id"], "queue_codex_bounded_implementation")
                self.assertEqual(status_payload["result_authority"], "local_task_queue")
                self.assertEqual(broker_preparation["automatic_live_response"]["status"], "completed")
                self.assertEqual(
                    broker_preparation["automatic_live_interpretation"]["classification"],
                    "model_response_text",
                )
            else:
                self.assertEqual(broker_preparation["broker_response"]["status"], "not_configured")
                self.assertEqual(status_payload["status"], "blocked")


    def test_stale_watcher_guard_marks_expired_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            watcher_status_path = status_dir / "HANDOFF_WATCHER.latest.json"
            watcher_status_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "polling",
                        "poll_interval_seconds": 1.0,
                        "stale_after_seconds": 1,
                        "stale": False,
                        "stale_reason": "",
                        "poll_count": 3,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "no_handoff_artifact_found",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_GUARD_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "stale")
            self.assertEqual(result["reason"], "heartbeat_expired")
            self.assertEqual(result["recommended_next_action"], "restart_local_handoff_watcher")

            updated_payload = json.loads(watcher_status_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_payload["lifecycle_state"], "stale")
            self.assertTrue(updated_payload["stale"])
            self.assertEqual(updated_payload["stale_reason"], "heartbeat_expired")
            self.assertEqual(updated_payload["recommended_next_action"], "restart_local_handoff_watcher")

    def test_recovery_helper_prints_deterministic_instruction_from_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            watcher_status_path = status_dir / "HANDOFF_WATCHER.latest.json"
            watcher_status_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "stale",
                        "poll_interval_seconds": 1.0,
                        "stale_after_seconds": 1,
                        "stale": True,
                        "stale_reason": "heartbeat_expired",
                        "recommended_next_action": "restart_local_handoff_watcher",
                        "poll_count": 3,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "heartbeat_expired",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_RECOVERY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip(),
                "Watcher recovery instruction: restart_local_handoff_watcher",
            )

    def test_supervision_summary_helper_prints_concise_operator_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "polling",
                        "poll_interval_seconds": 2.0,
                        "stale_after_seconds": 6,
                        "stale": False,
                        "stale_reason": "",
                        "poll_count": 4,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "no_handoff_artifact_found",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-recovery-v1",
                        "status": "healthy",
                        "reason": "watcher_status_fresh",
                        "recommended_next_action": "none",
                        "restart_attempt_count": 0,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: polling",
                    "Recovery state: healthy",
                    "Manual action needed: no",
                ],
            )

    def test_supervision_summary_helper_reports_manual_action_for_failed_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "stale",
                        "poll_interval_seconds": 2.0,
                        "stale_after_seconds": 6,
                        "stale": True,
                        "stale_reason": "heartbeat_expired",
                        "poll_count": 4,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "heartbeat_expired",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-recovery-v1",
                        "status": "recovery_failed",
                        "reason": "recovery_verification_failed",
                        "recommended_next_action": "restart_local_handoff_watcher",
                        "restart_attempt_count": 2,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: stale (stale)",
                    "Recovery state: recovery_failed",
                    "Manual action needed: yes",
                ],
            )

    def test_supervision_summary_helper_handles_missing_watcher_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-recovery-v1",
                        "status": "healthy",
                        "reason": "watcher_status_fresh",
                        "recommended_next_action": "none",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: missing",
                    "Recovery state: healthy",
                    "Manual action needed: yes",
                ],
            )

    def test_supervision_summary_helper_handles_missing_recovery_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "polling",
                        "poll_interval_seconds": 2.0,
                        "stale_after_seconds": 6,
                        "stale": False,
                        "stale_reason": "",
                        "poll_count": 4,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "no_handoff_artifact_found",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: polling",
                    "Recovery state: missing",
                    "Manual action needed: yes",
                ],
            )

    def test_supervision_summary_helper_handles_malformed_watcher_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER.latest.json").write_text(
                "{not-json\n",
                encoding="utf-8",
            )
            (status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-recovery-v1",
                        "status": "healthy",
                        "reason": "watcher_status_fresh",
                        "recommended_next_action": "none",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: malformed",
                    "Recovery state: healthy",
                    "Manual action needed: yes",
                ],
            )

    def test_supervision_summary_helper_handles_malformed_recovery_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            (status_dir / "HANDOFF_WATCHER.latest.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "polling",
                        "poll_interval_seconds": 2.0,
                        "stale_after_seconds": 6,
                        "stale": False,
                        "stale_reason": "",
                        "poll_count": 4,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "no_handoff_artifact_found",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json").write_text(
                "[not-a-dict]\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUMMARY_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                completed.stdout.strip().splitlines(),
                [
                    "Watcher state: polling",
                    "Recovery state: malformed",
                    "Manual action needed: yes",
                ],
            )

    def test_supervisor_restarts_stale_watcher_and_records_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            watcher_status_path = status_dir / "HANDOFF_WATCHER.latest.json"
            watcher_status_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "polling",
                        "poll_interval_seconds": 1.0,
                        "stale_after_seconds": 1,
                        "stale": False,
                        "stale_reason": "",
                        "poll_count": 3,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "no_handoff_artifact_found",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            fake_systemctl, state_path = self._write_fake_systemctl(
                root=root,
                watcher_status_path=watcher_status_path,
                active=True,
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUPERVISOR_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_HANDOFF_SYSTEMCTL_BIN": str(fake_systemctl),
                    "MIM_HANDOFF_WATCHER_SERVICE_SCOPE": "user",
                    "MIM_HANDOFF_WATCHER_SERVICE_NAME": "mim-handoff-watcher.service",
                    "MIM_HANDOFF_RECOVERY_RUN_ONCE": "1",
                    "MIM_HANDOFF_RECOVERY_STARTUP_GRACE_SECONDS": "0",
                    "FAKE_SYSTEMCTL_STATE_PATH": str(state_path),
                    "FAKE_WATCHER_STATUS_PATH": str(watcher_status_path),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["cycle_count"], 1)
            self.assertEqual(result["last_result"]["status"], "recovered")
            self.assertEqual(result["last_result"]["service_action"]["action"], "restart")
            self.assertTrue(result["last_result"]["service_action"]["succeeded"])
            self.assertEqual(result["last_result"]["post_recovery_guard"]["status"], "ok")

            recovery_payload = json.loads(Path(result["recovery_status_path"]).read_text(encoding="utf-8"))
            self.assertEqual(recovery_payload["status"], "recovered")
            self.assertEqual(recovery_payload["restart_attempt_count"], 1)

            fake_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_state["restart_count"], 1)
            self.assertEqual(fake_state["last_action"], "restart")

    def test_supervisor_respects_recovery_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            status_dir = handoff_root / "status"
            status_dir.mkdir(parents=True, exist_ok=True)
            watcher_status_path = status_dir / "HANDOFF_WATCHER.latest.json"
            watcher_status_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-status-v1",
                        "updated_at": "2026-04-13T00:00:00Z",
                        "lifecycle_state": "stale",
                        "poll_interval_seconds": 1.0,
                        "stale_after_seconds": 1,
                        "stale": True,
                        "stale_reason": "heartbeat_expired",
                        "recommended_next_action": "restart_local_handoff_watcher",
                        "poll_count": 3,
                        "processed_count": 0,
                        "handoff_root": str(handoff_root),
                        "last_result": {
                            "status": "idle",
                            "mode": "",
                            "handoff_id": "",
                            "reason": "heartbeat_expired",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            recovery_path = status_dir / "HANDOFF_WATCHER_RECOVERY.latest.json"
            recovery_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "mim-handoff-watcher-recovery-v1",
                        "last_recovery_started_at": "2999-01-01T00:00:00Z",
                        "restart_attempt_count": 1,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            fake_systemctl, state_path = self._write_fake_systemctl(
                root=root,
                watcher_status_path=watcher_status_path,
                active=True,
            )

            completed = subprocess.run(
                [sys.executable, str(WATCHER_SUPERVISOR_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_HANDOFF_SYSTEMCTL_BIN": str(fake_systemctl),
                    "MIM_HANDOFF_WATCHER_SERVICE_SCOPE": "user",
                    "MIM_HANDOFF_WATCHER_SERVICE_NAME": "mim-handoff-watcher.service",
                    "MIM_HANDOFF_RECOVERY_RUN_ONCE": "1",
                    "MIM_HANDOFF_RECOVERY_COOLDOWN_SECONDS": "3600",
                    "FAKE_SYSTEMCTL_STATE_PATH": str(state_path),
                    "FAKE_WATCHER_STATUS_PATH": str(watcher_status_path),
                },
                check=True,
                capture_output=True,
                text=True,
            )

            result = json.loads(completed.stdout)
            self.assertEqual(result["last_result"]["status"], "cooldown_active")
            self.assertGreater(result["last_result"]["cooldown_remaining_seconds"], 0)

            fake_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_state["restart_count"], 0)


if __name__ == "__main__":
    unittest.main()