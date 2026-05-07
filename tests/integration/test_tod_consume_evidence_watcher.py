import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCHER_SCRIPT = ROOT / "scripts" / "watch_tod_consume_evidence.sh"


def iso_now(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class TodConsumeEvidenceWatcherTest(unittest.TestCase):
    def test_writes_collaboration_progress_from_current_tod_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            target_task_id = "objective-97-task-smoke-collab-progress"

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-30),
                        "task": {"active_task_id": target_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-30),
                        "task_id": target_task_id,
                        "request_id": target_task_id,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-5),
                        "request_id": target_task_id,
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-3),
                        "request_id": target_task_id,
                        "status": "succeeded",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TOD_MANUAL_DISPATCH_LOCK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-60),
                        "active": True,
                        "task_id": target_task_id,
                        "reason": "preserve_authoritative_request_lane",
                        "expires_at": iso_now(300),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_RECOVERY_ALERT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-20),
                        "task_id": target_task_id,
                        "task_state": "failed",
                        "progress_classification": "no_heartbeats_recovery_in_progress",
                        "issue_code": "publication_surface_divergence",
                        "issue_detail": "Remote canonical request surface diverges from the expected live publication boundary.",
                        "recovery_action": "observe_publication_boundary",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            progress = json.loads(
                (shared / "MIM_TOD_COLLAB_PROGRESS.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(progress["execution_id"], target_task_id)
            self.assertEqual(progress["id_kind"], "bridge_request_id")
            self.assertEqual(progress["execution_lane"], "tod_bridge_request")
            self.assertEqual(progress["task_id"], target_task_id)
            self.assertEqual(progress["request_id"], target_task_id)
            self.assertEqual(progress["type"], "mim_tod_collaboration_progress_v1")

            workstreams = {item["name"]: item for item in progress["workstreams"]}
            self.assertEqual(
                workstreams["consume_mutation_tracking"]["mim_status"],
                "auto_watch_captured_consume_mutation",
            )
            self.assertEqual(
                workstreams["consume_mutation_tracking"]["tod_status"],
                "result_published_for_target_task",
            )
            self.assertEqual(
                workstreams["publisher_guard"]["mim_status"],
                "manual_dispatch_lock_active",
            )
            self.assertEqual(
                workstreams["tod_recovery_progress"]["tod_status"],
                "no_heartbeats_recovery_in_progress",
            )
            self.assertIn(
                "publication_surface_divergence",
                workstreams["tod_recovery_progress"]["latest_observation"],
            )

    def test_captures_existing_matching_ack_and_result_when_target_switches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            old_task_id = "objective-97-task-3422"
            new_task_id = "objective-97-task-mim-arm-safe-home-207749"

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-30),
                        "task": {"active_task_id": old_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": old_task_id,
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": old_task_id,
                        "status": "",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            first = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "task": {"active_task_id": new_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-5),
                        "request_id": new_task_id,
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-3),
                        "request_id": new_task_id,
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            second = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            evidence = json.loads(
                (shared / "MIM_TOD_CONSUME_EVIDENCE.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["task_id"], new_task_id)
            self.assertEqual(evidence["watch"]["phase"], "captured")
            self.assertEqual(
                evidence["first_mutations"]["task_ack"]["request_id"], new_task_id
            )
            self.assertEqual(
                evidence["first_mutations"]["task_result"]["request_id"], new_task_id
            )

    def test_ignores_stale_recovery_alert_for_different_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            active_task_id = "objective-115-task-mim-arm-capture-frame-20260407033825"
            stale_task_id = "objective-115-task-mim-arm-safe-home-20260407030000"

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-10),
                        "task": {"active_task_id": active_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-10),
                        "task_id": active_task_id,
                        "request_id": active_task_id,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-3),
                        "request_id": active_task_id,
                        "status": "succeeded",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_RECOVERY_ALERT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-20),
                        "task_id": stale_task_id,
                        "task_state": "failed",
                        "progress_classification": "no_heartbeats_recovery_in_progress",
                        "issue_code": "bridge_remote_publish_unverified",
                        "issue_detail": "Listener-stage artifacts are current, but remote publish verification failed.",
                        "recovery_action": "refresh_shared_state_sync",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            progress = json.loads(
                (shared / "MIM_TOD_COLLAB_PROGRESS.latest.json").read_text(encoding="utf-8")
            )
            workstreams = {item["name"]: item for item in progress["workstreams"]}
            self.assertEqual(
                workstreams["tod_recovery_progress"]["mim_status"],
                "stale_tod_recovery_signal_ignored",
            )
            self.assertEqual(
                workstreams["tod_recovery_progress"]["tod_status"],
                "no_recovery_alert_present",
            )
            self.assertIn(
                stale_task_id,
                workstreams["tod_recovery_progress"]["latest_observation"],
            )

    def test_captures_first_ack_and_result_mutations_for_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            target_task_id = "objective-97-task-collab-watch"

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-30),
                        "task": {"active_task_id": target_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": "older-task",
                        "status": "acknowledged",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": "older-task",
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            first = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

            # Mutate both ACK and RESULT to the collaborative task id.
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "request_id": target_task_id,
                        "status": "acknowledged",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "request_id": target_task_id,
                        "status": "in_progress",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            second = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "900",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            evidence = json.loads(
                (shared / "MIM_TOD_CONSUME_EVIDENCE.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["task_id"], target_task_id)
            self.assertEqual(evidence["watch"]["phase"], "captured")
            self.assertIsInstance(evidence["first_mutations"]["task_ack"], dict)
            self.assertIsInstance(evidence["first_mutations"]["task_result"], dict)
            self.assertEqual(
                evidence["first_mutations"]["task_ack"]["request_id"], target_task_id
            )
            self.assertEqual(
                evidence["first_mutations"]["task_result"]["request_id"],
                target_task_id,
            )

    def test_marks_timeout_when_watch_window_elapses_without_full_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            target_task_id = "objective-97-task-timeout-watch"
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-60),
                        "task": {"active_task_id": target_task_id},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": "older-task",
                        "status": "acknowledged",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(-120),
                        "request_id": "older-task",
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            state_path = logs / "mim_tod_consume_evidence.state.json"
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            state_payload["watch_started_at"] = iso_now(-300)
            state_path.write_text(
                json.dumps(state_payload, indent=2) + "\n", encoding="utf-8"
            )

            # Run one more cycle so elapsed time crosses the watch window.
            completed2 = subprocess.run(
                ["bash", str(WATCHER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "WATCH_WINDOW_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed2.returncode, 0, completed2.stdout + completed2.stderr)

            evidence = json.loads(
                (shared / "MIM_TOD_CONSUME_EVIDENCE.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["task_id"], target_task_id)
            self.assertEqual(evidence["watch"]["phase"], "timeout")
            self.assertTrue(bool(evidence["watch"]["timed_out"]))
            self.assertIsNone(evidence["first_mutations"]["task_ack"])
            self.assertIsNone(evidence["first_mutations"]["task_result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
