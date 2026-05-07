import argparse
import json
import tempfile
import unittest
from pathlib import Path

from scripts import tod_bridge_audit


class TodBridgeAuditTests(unittest.TestCase):
    def test_build_event_records_absolute_and_real_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact = root / "runtime" / "shared" / "MIM_TOD_TASK_REQUEST.latest.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('{"task_id":"objective-97-task-3422"}\n', encoding="utf-8")

            args = argparse.Namespace(
                event="local_request_write",
                caller="test_case",
                service_name="unit_test",
                task_id="objective-97-task-3422",
                objective_id="objective-97",
                publish_target="/tmp/local",
                remote_host="",
                remote_root="",
                publish_attempted="false",
                publish_succeeded="false",
                publish_returncode=0,
                publish_output="",
                artifact_path=[str(artifact)],
                extra_json="",
            )

            event = tod_bridge_audit.build_event(args)

            self.assertEqual(event["event"], "local_request_write")
            self.assertEqual(event["task_id"], "objective-97-task-3422")
            self.assertEqual(len(event["artifacts"]), 1)
            artifact_summary = event["artifacts"][0]
            self.assertEqual(artifact_summary["absolute_path"], str(artifact))
            self.assertEqual(artifact_summary["realpath"], str(artifact.resolve()))
            self.assertTrue(artifact_summary["exists"])
            self.assertTrue(artifact_summary["sha256"])

    def test_write_event_updates_jsonl_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jsonl_path = root / "tod_bridge_write_audit.jsonl"
            latest_path = root / "tod_bridge_write_audit.latest.json"
            event = {
                "generated_at": "2026-04-02T00:00:00Z",
                "type": "tod_bridge_write_audit_v1",
                "event": "remote_publish_transport",
                "task_id": "objective-97-task-3422",
                "artifacts": [],
            }

            tod_bridge_audit.write_event(event, jsonl_path=jsonl_path, latest_path=latest_path)

            jsonl_rows = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(jsonl_rows), 1)
            self.assertEqual(json.loads(jsonl_rows[0])["event"], "remote_publish_transport")
            latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
            self.assertEqual(latest_payload["task_id"], "objective-97-task-3422")


if __name__ == "__main__":
    unittest.main()