import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCH_SCRIPT = ROOT / "scripts" / "watch_tod_liveness.sh"


class TodLivenessWatcherTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _set_mtime(self, path: Path, timestamp: str) -> None:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
        epoch = dt.timestamp()
        os.utime(path, (epoch, epoch))

    def test_does_not_emit_freeze_ping_for_stale_loop_journal_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "TOD_MIM_TASK_ACK.latest.json",
                {"generated_at": "2026-03-31T21:58:00Z", "request_id": "objective-97-task-safe-home"},
            )
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {"generated_at": "2026-03-31T21:58:10Z", "request_id": "objective-97-task-safe-home", "status": "completed"},
            )
            self._write_json(
                shared_dir / "TOD_INTEGRATION_STATUS.latest.json",
                {"generated_at": "2026-03-31T21:58:20Z", "compatible": True},
            )
            self._write_json(
                shared_dir / "TOD_LOOP_JOURNAL.latest.json",
                {"generated_at": "2026-03-29T14:00:10Z", "status": "stale"},
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "RUN_ONCE": "1",
                    "STALE_SECONDS": "45",
                    "COOLDOWN_SECONDS": "0",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse((shared_dir / "MIM_TO_TOD_PING.latest.json").exists())

            event_log = shared_dir / "TOD_LIVENESS_EVENTS.latest.jsonl"
            self.assertTrue(event_log.exists())
            self.assertEqual(event_log.read_text(encoding="utf-8"), "")

    def test_emits_freeze_ping_when_all_watched_artifacts_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "TOD_MIM_TASK_ACK.latest.json",
                {"generated_at": "2026-03-31T21:40:00Z", "request_id": "objective-97-task-safe-home"},
            )
            self._set_mtime(shared_dir / "TOD_MIM_TASK_ACK.latest.json", "2026-03-31T21:40:00Z")
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {"generated_at": "2026-03-31T21:40:00Z", "request_id": "objective-97-task-safe-home", "status": "completed"},
            )
            self._set_mtime(shared_dir / "TOD_MIM_TASK_RESULT.latest.json", "2026-03-31T21:40:00Z")
            self._write_json(
                shared_dir / "TOD_INTEGRATION_STATUS.latest.json",
                {"generated_at": "2026-03-31T21:40:20Z", "compatible": True},
            )
            self._set_mtime(shared_dir / "TOD_INTEGRATION_STATUS.latest.json", "2026-03-31T21:40:20Z")

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "RUN_ONCE": "1",
                    "STALE_SECONDS": "45",
                    "COOLDOWN_SECONDS": "0",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue((shared_dir / "MIM_TO_TOD_PING.latest.json").exists())

            event_lines = [
                json.loads(line)
                for line in (shared_dir / "TOD_LIVENESS_EVENTS.latest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(any(item.get("event") == "freeze_suspected" for item in event_lines))

    def test_does_not_emit_freeze_ping_when_any_watched_artifact_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "TOD_MIM_TASK_ACK.latest.json",
                {"generated_at": "2026-03-31T21:40:00Z", "request_id": "objective-97-task-safe-home"},
            )
            self._set_mtime(shared_dir / "TOD_MIM_TASK_ACK.latest.json", "2026-03-31T21:40:00Z")
            self._write_json(
                shared_dir / "TOD_MIM_TASK_RESULT.latest.json",
                {"generated_at": "2026-03-31T21:58:10Z", "request_id": "objective-97-task-safe-home", "status": "completed"},
            )
            self._write_json(
                shared_dir / "TOD_INTEGRATION_STATUS.latest.json",
                {"generated_at": "2026-03-31T21:58:20Z", "compatible": True},
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "RUN_ONCE": "1",
                    "STALE_SECONDS": "45",
                    "COOLDOWN_SECONDS": "0",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse((shared_dir / "MIM_TO_TOD_PING.latest.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)