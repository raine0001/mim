import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESPONDER_SCRIPT = ROOT / "scripts" / "watch_mim_coordination_responder.sh"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MimCoordinationResponderTest(unittest.TestCase):
    def test_emits_pending_ack_for_active_coordination_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            request_payload = {
                "generated_at": iso_now(),
                "source": "tod-mim-coordination-request-v1",
                "status": "active",
                "priority": "high",
                "escalation_level": 2,
                "request_id": "handoff-alias-manual-20260330t163803z",
                "objective_id": "objective-97",
                "issue_code": "handoff_artifact_alias_detected",
                "issue_summary": "TOD detected alias handoff artifact and requires explicit MIM coordination.",
                "requested_action": "acknowledge_and_coordinate",
                "correlation_id": "obj97-task3422-coord-ack",
            }
            (shared / "TOD_MIM_COORDINATION_REQUEST.latest.json").write_text(
                json.dumps(request_payload, indent=2) + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            ack = json.loads(
                (shared / "MIM_TOD_COORDINATION_ACK.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(ack["request_id"], "handoff-alias-manual-20260330t163803z")
            self.assertEqual(ack["objective_id"], "objective-97")
            self.assertEqual(ack["ack_status"], "pending")
            self.assertEqual(ack["status"], "pending")
            self.assertEqual(ack["coordination"]["status"], "pending")
            self.assertEqual(
                ack["coordination"]["pending_request_id"],
                "handoff-alias-manual-20260330t163803z",
            )

            status_payload = json.loads(
                (logs / "mim_coordination_responder.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["state"], "ack_emitted_pending")
            self.assertTrue(bool(status_payload["ack_written"]))
            self.assertEqual(
                status_payload["pending_request_id"],
                "handoff-alias-manual-20260330t163803z",
            )

    def test_refreshes_ack_for_resolved_request_when_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared"
            logs = root / "logs"
            shared.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)

            (shared / "TOD_MIM_COORDINATION_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_now(),
                        "status": "resolved",
                        "request_id": "objective-97-task-3422",
                        "objective_id": "objective-97",
                        "issue_code": "stalled_regression_no_delta_resolved",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            original_ack = {
                "request_id": "older-request",
                "ack_status": "pending",
                "coordination": {"status": "pending"},
            }
            ack_path = shared / "MIM_TOD_COORDINATION_ACK.latest.json"
            ack_path.write_text(json.dumps(original_ack, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                ["bash", str(RESPONDER_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared),
                    "LOG_DIR": str(logs),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                    "ALLOW_RESOLVED_REQUESTS": "0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            ack_after = json.loads(ack_path.read_text(encoding="utf-8"))
            self.assertEqual(ack_after["request_id"], "objective-97-task-3422")
            self.assertEqual(ack_after["ack_status"], "resolved")
            self.assertEqual(ack_after["status"], "resolved")
            self.assertEqual(ack_after["coordination"]["status"], "resolved")

            status_payload = json.loads(
                (logs / "mim_coordination_responder.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(status_payload["state"], "ack_emitted_resolved")
            self.assertTrue(bool(status_payload["ack_written"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
