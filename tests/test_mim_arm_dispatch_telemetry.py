from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.mim_arm_dispatch_telemetry import (
    dispatch_telemetry_record_path,
    load_latest_dispatch_telemetry,
    record_dispatch_telemetry_from_publish,
    refresh_dispatch_telemetry_record,
    update_dispatch_telemetry_from_feedback,
)


class MimArmDispatchTelemetryTests(unittest.TestCase):
    def test_record_dispatch_telemetry_from_publish_writes_latest_and_per_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_path = root / "MIM_TOD_TASK_REQUEST.latest.json"
            trigger_path = root / "MIM_TO_TOD_TRIGGER.latest.json"
            request_path.write_text("{}\n", encoding="utf-8")
            trigger_path.write_text("{}\n", encoding="utf-8")

            payload = record_dispatch_telemetry_from_publish(
                shared_root=root,
                execution_id=501,
                capability_name="mim_arm.execute_safe_home",
                execution_lane="tod",
                request_payload={
                    "request_id": "objective-108-task-mim-arm-safe-home-1",
                    "correlation_id": "obj108-mim-arm-safe-home-1",
                    "generated_at": "2026-04-06T16:40:00Z",
                    "action": "safe_home",
                    "feedback_endpoint": "/gateway/capabilities/executions/501/feedback",
                    "handoff_endpoint": "/gateway/capabilities/executions/501/handoff",
                },
                trigger_payload={
                    "task_id": "objective-108-task-mim-arm-safe-home-1",
                    "generated_at": "2026-04-06T16:40:01Z",
                },
                request_path=request_path,
                trigger_path=trigger_path,
                remote_publish={"attempted": False, "succeeded": False},
            )

            self.assertEqual(payload["request_id"], "objective-108-task-mim-arm-safe-home-1")
            self.assertEqual(payload["task_id"], "objective-108-task-mim-arm-safe-home-1")
            self.assertEqual(payload["dispatch_status"], "published_local")
            self.assertEqual(payload["completion_status"], "pending")
            self.assertTrue(dispatch_telemetry_record_path(root, payload["request_id"]).exists())
            self.assertEqual(load_latest_dispatch_telemetry(root)["request_id"], payload["request_id"])

    def test_refresh_dispatch_telemetry_record_promotes_ack_and_result_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_id = "objective-108-task-mim-arm-safe-home-2"
            record_dispatch_telemetry_from_publish(
                shared_root=root,
                execution_id=502,
                capability_name="mim_arm.execute_safe_home",
                execution_lane="tod",
                request_payload={
                    "request_id": request_id,
                    "correlation_id": "obj108-mim-arm-safe-home-2",
                    "generated_at": "2026-04-06T16:41:00Z",
                    "action": "safe_home",
                },
                trigger_payload={
                    "task_id": request_id,
                    "generated_at": "2026-04-06T16:41:01Z",
                },
                request_path=root / "MIM_TOD_TASK_REQUEST.latest.json",
                trigger_path=root / "MIM_TO_TOD_TRIGGER.latest.json",
                remote_publish={"attempted": False, "succeeded": False},
            )

            (root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps({
                    "request_id": request_id,
                    "generated_at": "2026-04-06T16:41:05Z",
                    "status": "accepted",
                }) + "\n",
                encoding="utf-8",
            )
            (root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps({
                    "request_id": request_id,
                    "generated_at": "2026-04-06T16:41:12Z",
                    "status": "success",
                    "reason": "safe_home_completed",
                }) + "\n",
                encoding="utf-8",
            )

            payload = refresh_dispatch_telemetry_record(root, request_id=request_id)

            self.assertEqual(payload["host_received_timestamp"], "2026-04-06T16:41:05Z")
            self.assertEqual(payload["host_completed_timestamp"], "2026-04-06T16:41:12Z")
            self.assertEqual(payload["dispatch_status"], "completed")
            self.assertEqual(payload["completion_status"], "completed")
            self.assertEqual(payload["result_reason"], "safe_home_completed")
            evidence_kinds = {item.get("kind") for item in payload.get("evidence_sources", []) if isinstance(item, dict)}
            self.assertIn("task_ack_artifact", evidence_kinds)
            self.assertIn("task_result_artifact", evidence_kinds)

    def test_update_dispatch_telemetry_from_feedback_uses_execution_feedback_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_id = "objective-108-task-mim-arm-safe-home-3"
            record_dispatch_telemetry_from_publish(
                shared_root=root,
                execution_id=503,
                capability_name="mim_arm.execute_safe_home",
                execution_lane="tod",
                request_payload={
                    "request_id": request_id,
                    "correlation_id": "obj108-mim-arm-safe-home-3",
                    "generated_at": "2026-04-06T16:42:00Z",
                    "action": "safe_home",
                },
                trigger_payload={
                    "task_id": request_id,
                    "generated_at": "2026-04-06T16:42:01Z",
                },
                request_path=root / "MIM_TOD_TASK_REQUEST.latest.json",
                trigger_path=root / "MIM_TO_TOD_TRIGGER.latest.json",
                remote_publish={"attempted": False, "succeeded": False},
            )

            execution = SimpleNamespace(
                id=503,
                feedback_json={"tod_bridge_publication": {"request_id": request_id}},
            )

            payload = update_dispatch_telemetry_from_feedback(
                shared_root=root,
                execution=execution,
                feedback_status="succeeded",
                resolved_reason="completed_on_host",
                runtime_outcome="success",
                correlation_json={"host_received_timestamp": "2026-04-06T16:42:03Z"},
                feedback_json={"host_completed_timestamp": "2026-04-06T16:42:10Z"},
                execution_truth={"published_at": "2026-04-06T16:42:10Z"},
            )

            self.assertEqual(payload["host_received_timestamp"], "2026-04-06T16:42:03Z")
            self.assertEqual(payload["host_completed_timestamp"], "2026-04-06T16:42:10Z")
            self.assertEqual(payload["dispatch_status"], "completed")
            self.assertEqual(payload["completion_status"], "completed")
            self.assertEqual(payload["result_reason"], "completed_on_host")

    def test_update_dispatch_telemetry_from_feedback_prefers_executor_timestamp_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_id = "objective-109-task-mim-arm-scan-pose-1"
            record_dispatch_telemetry_from_publish(
                shared_root=root,
                execution_id=504,
                capability_name="mim_arm.execute_scan_pose",
                execution_lane="tod",
                request_payload={
                    "request_id": request_id,
                    "correlation_id": "obj109-mim-arm-scan-pose-1",
                    "generated_at": "2026-04-06T16:43:00Z",
                    "action": "scan_pose",
                },
                trigger_payload={
                    "task_id": request_id,
                    "generated_at": "2026-04-06T16:43:01Z",
                },
                request_path=root / "MIM_TOD_TASK_REQUEST.latest.json",
                trigger_path=root / "MIM_TO_TOD_TRIGGER.latest.json",
                remote_publish={"attempted": False, "succeeded": False},
            )

            execution = SimpleNamespace(
                id=504,
                feedback_json={"tod_bridge_publication": {"request_id": request_id}},
            )

            payload = update_dispatch_telemetry_from_feedback(
                shared_root=root,
                execution=execution,
                feedback_status="succeeded",
                resolved_reason="scan_pose_completed",
                runtime_outcome="success",
                correlation_json={
                    "host_received_timestamp": "2026-04-06T16:43:02Z",
                    "executor_timestamps": {"host_received_timestamp": "2026-04-06T16:43:03Z"},
                },
                feedback_json={
                    "host_completed_timestamp": "2026-04-06T16:43:09Z",
                    "executor_timestamps": {"host_completed_timestamp": "2026-04-06T16:43:10Z"},
                },
                execution_truth={"published_at": "2026-04-06T16:43:11Z"},
            )

            self.assertEqual(payload["host_received_timestamp"], "2026-04-06T16:43:03Z")
            self.assertEqual(payload["host_completed_timestamp"], "2026-04-06T16:43:10Z")
            self.assertEqual(payload["dispatch_status"], "completed")
            self.assertEqual(payload["completion_status"], "completed")
            self.assertEqual(payload["result_reason"], "scan_pose_completed")


if __name__ == "__main__":
    unittest.main()