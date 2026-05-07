import json
import tempfile
import unittest
from pathlib import Path

from core.primitive_request_recovery_service import load_authoritative_request_status


class PrimitiveRequestRecoveryServiceTests(unittest.TestCase):
    def test_load_authoritative_request_status_uses_latest_observed_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                        "task_id": "objective-2900-task-7117",
                        "objective_id": "objective-2900",
                        "generated_at": "2026-05-02T18:30:58Z",
                        "request_status": "queued",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                        "task_id": "objective-2900-task-7117",
                        "generated_at": "2026-05-02T19:04:23Z",
                        "status": "accepted",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                        "task_id": "objective-2900-task-7117",
                        "generated_at": "2026-05-02T19:15:44Z",
                        "result_status": "failed",
                        "result_reason": "invalid_packet_shape",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = load_authoritative_request_status(shared_root=shared_root)

        self.assertEqual(payload["request_generated_at"], "2026-05-02T18:30:58Z")
        self.assertEqual(payload["ack_generated_at"], "2026-05-02T19:04:23Z")
        self.assertEqual(payload["result_generated_at"], "2026-05-02T19:15:44Z")
        self.assertEqual(payload["generated_at"], "2026-05-02T19:15:44Z")

    def test_load_authoritative_request_status_rejects_mixed_review_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2912-task-008",
                        "task_id": "objective-2912-task-008",
                        "objective_id": "objective-2912",
                        "generated_at": "2026-05-04T04:17:52Z",
                        "request_status": "queued",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2912-task-008",
                        "task_id": "objective-2912-task-008",
                        "generated_at": "2026-05-04T04:22:38Z",
                        "result_status": "failed",
                        "result_reason": "stalled_regression_no_delta",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T04:14:28Z",
                        "state": "failed",
                        "state_reason": "stalled_regression_no_delta",
                        "task": {
                            "active_task_id": "objective-2912-task-7141",
                            "objective_id": "2912",
                            "request_request_id": "objective-2912-task-7141-implement-bounded-work",
                            "result_request_id": "objective-2912-task-7141-implement-bounded-work",
                            "result_status": "failed",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = load_authoritative_request_status(shared_root=shared_root)

        self.assertEqual(payload["request_id"], "objective-2912-task-008")
        self.assertEqual(payload["task_id"], "objective-2912-task-008")
        self.assertEqual(payload["objective_id"], "2912")
        self.assertEqual(payload["result_status"], "rejected_lineage_mismatch")
        self.assertTrue(payload["lineage_mismatch"])
        self.assertEqual(payload["review_generated_at"], "2026-05-04T04:14:28Z")
        self.assertEqual(payload["generated_at"], "2026-05-04T04:22:38Z")

    def test_load_authoritative_request_status_accepts_matching_review_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2913-task-7144-project-3-task-2",
                        "task_id": "objective-2913-task-7144",
                        "objective_id": "2913",
                        "generated_at": "2026-05-04T18:24:50Z",
                        "request_status": "queued",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2913-task-7144-project-3-task-2",
                        "task_id": "objective-2913-task-7144",
                        "objective_id": "2913",
                        "generated_at": "2026-05-04T18:24:51Z",
                        "status": "accepted",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:52Z",
                        "state": "awaiting_result",
                        "state_reason": "trigger_ack_current",
                        "task": {
                            "active_task_id": "objective-2913-task-7144",
                            "objective_id": "2913",
                            "request_request_id": "objective-2913-task-7144-project-3-task-2",
                            "result_status": "awaiting_result",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = load_authoritative_request_status(shared_root=shared_root)

        self.assertEqual(payload["request_id"], "objective-2913-task-7144-project-3-task-2")
        self.assertEqual(payload["task_id"], "objective-2913-task-7144")
        self.assertEqual(payload["result_status"], "awaiting_result")
        self.assertNotIn("lineage_mismatch", payload)
