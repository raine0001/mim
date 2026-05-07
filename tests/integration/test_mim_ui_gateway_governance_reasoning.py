import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from core.routers import mim_ui
from core.ui_health_service import (
    MIM_UI_CAMERA_DEVICE_ID,
    MIM_UI_CAMERA_SESSION_ID,
    _select_preferred_perception_row,
    assess_perception_lane,
    assess_speech_lane,
    build_mim_ui_health_snapshot_from_rows,
    summarize_runtime_health,
)


class MimUiGatewayGovernanceReasoningTest(unittest.TestCase):
    def test_snapshot_normalization(self):
        snapshot = mim_ui._operator_gateway_governance_snapshot(
            {
                "applied_reason": "system_health_degraded",
                "applied_outcome": "requires_confirmation",
                "primary_signal": "degraded_health_confirmation",
                "system_health_status": "degraded",
                "summary": "System health is degraded; automatic execution is paused.",
                "signal_codes": ["system_health_degraded"],
                "precedence_order": [
                    "explicit_operator_approval",
                    "hard_safety_escalation",
                    "degraded_health_confirmation",
                    "benign_healthy_auto_execution",
                ],
            }
        )
        self.assertEqual(snapshot["primary_signal"], "degraded_health_confirmation")
        self.assertEqual(snapshot["system_health_status"], "degraded")
        self.assertIn("system_health_degraded", snapshot["signal_codes"])

    def test_summary_includes_gateway_governance(self):
        summary = mim_ui._build_operator_reasoning_summary(
            goal={},
            inquiry={},
            governance={},
            gateway_governance={
                "summary": "System health is degraded; automatic execution is paused.",
            },
            autonomy={},
            stewardship={},
            execution_readiness={},
            execution_recovery={},
            commitment={},
            commitment_monitoring={},
            commitment_outcome={},
            learned_preferences=[],
            proposal_policy={},
            conflict_resolution={},
            runtime_health={},
            runtime_recovery={},
        )
        self.assertIn("Gateway governance", summary)
        self.assertIn("degraded", summary)

    def test_active_work_snapshot_marks_reply_only_when_no_tracked_work_exists(self):
        snapshot = mim_ui._operator_active_work_snapshot({}, {})

        self.assertFalse(snapshot["tracked"])
        self.assertEqual(snapshot["state"], "reply_only")
        self.assertIn("No tracked work is active", snapshot["summary"])

    def test_active_work_snapshot_marks_working_when_collaboration_request_exists(self):
        snapshot = mim_ui._operator_active_work_snapshot(
            {
                "request_id": "mim-request-123",
                "task_id": "handoff-task-conversation-mim-request-123",
                "execution_id": "mim-request-123",
                "execution_id_label": "request mim-request-123",
                "summary": "request mim-request-123 | queued | decision_recorded",
                "active_workstream": {
                    "name": "conversation_handoff",
                    "tod_status": "queued",
                    "latest_observation": "Bounded implementation task staged.",
                },
            },
            {},
        )

        self.assertTrue(snapshot["tracked"])
        self.assertEqual(snapshot["state"], "working")
        self.assertEqual(snapshot["badge"], "Working now")
        self.assertEqual(snapshot["request_id"], "mim-request-123")
        self.assertIn("Bounded implementation task staged", snapshot["summary"])

    def test_summary_includes_active_work_signal(self):
        summary = mim_ui._build_operator_reasoning_summary(
            goal={},
            inquiry={},
            governance={},
            gateway_governance={},
            autonomy={},
            stewardship={},
            execution_readiness={},
            execution_recovery={},
            commitment={},
            commitment_monitoring={},
            commitment_outcome={},
            learned_preferences=[],
            proposal_policy={},
            conflict_resolution={},
            active_work={
                "summary": "request mim-request-123 is working now.",
            },
            runtime_health={},
            runtime_recovery={},
        )

        self.assertIn("Active work", summary)
        self.assertIn("working now", summary)

        def test_summary_includes_tod_decision_process(self):
                summary = mim_ui._build_operator_reasoning_summary(
                        goal={},
                        inquiry={},
                        governance={},
                        gateway_governance={},
                        autonomy={},
                        stewardship={},
                        execution_readiness={},
                        execution_recovery={},
                        commitment={},
                        commitment_monitoring={},
                        commitment_outcome={},
                        learned_preferences=[],
                        proposal_policy={},
                        conflict_resolution={},
                        tod_decision_process={
                                "summary": "TOD does not know what MIM did; escalation required",
                        },
                        runtime_health={},
                        runtime_recovery={},
                )

                self.assertIn("TOD decision", summary)
                self.assertIn("escalation required", summary)

        def test_tod_decision_process_snapshot_normalizes_latest_decision_artifact(self):
                with TemporaryDirectory() as tmpdir:
                        shared_root = Path(tmpdir)
                        (shared_root / "MIM_DECISION_TASK.latest.json").write_text(
                                """{
    "generated_at": "2026-04-07T12:00:00+00:00",
    "state": "watching",
    "decision_process": {
        "generated_at": "2026-04-07T12:00:00+00:00",
        "state": "watching",
        "questions": {
            "tod_knows_what_mim_did": {
                "known": false,
                "detail": "No TOD acknowledgement yet",
                "evidence": ["pending_ack"]
            },
            "mim_knows_what_tod_did": {
                "known": true,
                "detail": "TOD reported it is reviewing the task",
                "evidence": ["task_status"]
            },
            "tod_current_work": {
                "known": true,
                "task_id": "task-42",
                "objective_id": "objective-9",
                "phase": "reviewing_request",
                "detail": "TOD is checking the latest request"
            },
            "tod_liveness": {
                "status": "degraded",
                "ask_required": true,
                "latest_progress_age_seconds": 91,
                "ping_response_age_seconds": 31,
                "console_probe_age_seconds": null,
                "console_probe_status": "",
                "primary_alert_code": "tod_silent"
            }
        },
        "communication_escalation": {
            "required": true,
            "code": "tod_silent",
            "detail": "TOD has not responded in time",
            "console_url": "http://192.168.1.161:8844",
            "kick_hint": "ask_loudly"
        },
        "selected_action": {
            "code": "ask_loudly",
            "detail": "Ask TOD loudly for status"
        }
    },
    "blocking_reason_codes": ["communication_escalation"]
}
""",
                                encoding="utf-8",
                        )

                        snapshot = mim_ui._operator_tod_decision_process_snapshot(shared_root)

                self.assertEqual(snapshot["state"], "watching")
                self.assertFalse(snapshot["tod_knows_what_mim_did"]["known"])
                self.assertTrue(snapshot["mim_knows_what_tod_did"]["known"])
                self.assertEqual(snapshot["tod_current_work"]["task_id"], "task-42")
                self.assertEqual(snapshot["tod_liveness"]["status"], "degraded")
                self.assertTrue(snapshot["communication_escalation"]["required"])
                self.assertEqual(snapshot["selected_action"]["code"], "ask_loudly")
                self.assertIn("TOD does not know what MIM did", snapshot["summary"])
                self.assertIn("escalation required", snapshot["summary"])

    def test_perception_lane_treats_long_idle_gap_as_idle_not_stale(self):
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            last_seen_at=now - timedelta(hours=2),
            status="active",
            health_status="healthy",
            device_id="cam-1",
            metadata_json={"last_adapter_status": "accepted"},
        )

        snapshot = assess_perception_lane(
            lane="camera",
            row=row,
            now=now,
            stale_seconds=30.0,
        )

        self.assertTrue(snapshot["ok"], snapshot)
        self.assertEqual(snapshot["status"], "idle", snapshot)
        self.assertFalse(snapshot["diagnostic_code"], snapshot)

    def test_perception_lane_reports_recent_stale_gap(self):
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            last_seen_at=now - timedelta(seconds=45),
            status="active",
            health_status="healthy",
            device_id="mic-1",
            metadata_json={"last_adapter_status": "heartbeat_no_transcript"},
        )

        snapshot = assess_perception_lane(
            lane="microphone",
            row=row,
            now=now,
            stale_seconds=30.0,
        )

        self.assertFalse(snapshot["ok"], snapshot)
        self.assertEqual(snapshot["status"], "stale", snapshot)
        self.assertEqual(snapshot["diagnostic_code"], "microphone_signal_stale", snapshot)

    def test_speech_lane_treats_old_queued_output_as_idle_between_utterances(self):
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(
            created_at=now - timedelta(minutes=5),
            delivery_status="queued",
        )

        snapshot = assess_speech_lane(row=row, now=now)

        self.assertTrue(snapshot["ok"], snapshot)
        self.assertEqual(snapshot["status"], "idle", snapshot)

    def test_runtime_health_summary_uses_diagnostics(self):
        summary = summarize_runtime_health(
            {
                "diagnostics": [
                    {
                        "lane": "camera",
                        "detail": "No camera event for 45 seconds; last adapter status was accepted",
                    }
                ]
            }
        )

        self.assertIn("Camera", summary)
        self.assertIn("45 seconds", summary)

    def test_health_selection_prefers_embedded_mim_camera_device(self):
        rows = [
            SimpleNamespace(
                id=100,
                device_id="runtime-proof-camera",
                session_id="runtime-proof",
            ),
            SimpleNamespace(
                id=99,
                device_id=MIM_UI_CAMERA_DEVICE_ID,
                session_id=MIM_UI_CAMERA_SESSION_ID,
            ),
        ]

        selected = _select_preferred_perception_row(
            rows=rows,
            preferred_device_id=MIM_UI_CAMERA_DEVICE_ID,
            preferred_session_id=MIM_UI_CAMERA_SESSION_ID,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.device_id, MIM_UI_CAMERA_DEVICE_ID)

    def test_health_selection_falls_back_to_matching_session(self):
        rows = [
            SimpleNamespace(
                id=100,
                device_id="camera-other",
                session_id="runtime-proof",
            ),
            SimpleNamespace(
                id=99,
                device_id="camera-fallback",
                session_id=MIM_UI_CAMERA_SESSION_ID,
            ),
        ]

        selected = _select_preferred_perception_row(
            rows=rows,
            preferred_device_id=MIM_UI_CAMERA_DEVICE_ID,
            preferred_session_id=MIM_UI_CAMERA_SESSION_ID,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.session_id, MIM_UI_CAMERA_SESSION_ID)

    def test_embedded_camera_stale_path_ignores_fresher_synthetic_rows(self):
        now = datetime.now(timezone.utc)
        embedded_camera = SimpleNamespace(
            id=277,
            last_seen_at=now - timedelta(seconds=65),
            status="active",
            health_status="healthy",
            device_id=MIM_UI_CAMERA_DEVICE_ID,
            session_id=MIM_UI_CAMERA_SESSION_ID,
            metadata_json={"last_adapter_status": "heartbeat_frame_seen"},
        )
        synthetic_camera = SimpleNamespace(
            id=999,
            last_seen_at=now - timedelta(seconds=1),
            status="active",
            health_status="healthy",
            device_id="runtime-proof-camera",
            session_id="runtime-proof",
            metadata_json={"last_adapter_status": "accepted"},
        )

        selected_stale = _select_preferred_perception_row(
            rows=[synthetic_camera, embedded_camera],
            preferred_device_id=MIM_UI_CAMERA_DEVICE_ID,
            preferred_session_id=MIM_UI_CAMERA_SESSION_ID,
        )
        stale_snapshot = build_mim_ui_health_snapshot_from_rows(
            now=now,
            speech_row=None,
            camera_row=selected_stale,
            mic_row=None,
        )

        self.assertEqual(
            stale_snapshot["checks"]["camera"]["device_id"],
            MIM_UI_CAMERA_DEVICE_ID,
        )
        self.assertEqual(stale_snapshot["checks"]["camera"]["status"], "stale")
        self.assertEqual(
            stale_snapshot["checks"]["camera"]["last_adapter_status"],
            "heartbeat_frame_seen",
        )

        embedded_camera.last_seen_at = now - timedelta(seconds=2)
        selected_recovered = _select_preferred_perception_row(
            rows=[synthetic_camera, embedded_camera],
            preferred_device_id=MIM_UI_CAMERA_DEVICE_ID,
            preferred_session_id=MIM_UI_CAMERA_SESSION_ID,
        )
        recovered_snapshot = build_mim_ui_health_snapshot_from_rows(
            now=now,
            speech_row=None,
            camera_row=selected_recovered,
            mic_row=None,
        )

        self.assertEqual(
            recovered_snapshot["checks"]["camera"]["device_id"],
            MIM_UI_CAMERA_DEVICE_ID,
        )
        self.assertEqual(recovered_snapshot["checks"]["camera"]["status"], "healthy")
