import unittest
from datetime import datetime, timedelta, timezone
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
