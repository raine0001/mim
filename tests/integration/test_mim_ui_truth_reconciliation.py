import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.routers import mim_ui


class MimUiTruthReconciliationTest(unittest.TestCase):
    def test_truth_reconciliation_keeps_stale_guard_residue_non_blocking_under_full_authority(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "status": "contract_violation_rejected",
                        "request_id": "objective-2912-task-008",
                        "task_id": "objective-2912-task-008",
                        "stale_guard": {
                            "detected": True,
                            "status": "execution_blocked_by_stale_guard",
                            "reason": "higher_authoritative_task_ordinal_active",
                            "current_request": {"request_id": "objective-2912-task-008", "task_id": "objective-2912-task-008"},
                            "high_watermark": {
                                "request_id": "objective-2912-task-7141-next-authoritative-step",
                                "ordinal": 7141,
                            },
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2912-task-008",
                        "task_id": "objective-2912-task-008",
                        "status": "succeeded",
                        "result_status": "succeeded",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            snapshot = mim_ui._build_tod_truth_reconciliation_snapshot(
                initiative_driver={"active_objective": {"objective_id": "2912"}},
                authoritative_request={"objective_id": "2912", "request_id": "objective-2912-task-008", "task_id": "objective-2912-task-008"},
                shared_root=shared_root,
            )

        self.assertEqual(snapshot["state"], "execution_confirmed")
        self.assertTrue(snapshot["execution_confirmed"])
        self.assertEqual(snapshot["command_status"], "contract_violation_rejected")
        self.assertFalse(snapshot["requires_human"])

    def test_truth_reconciliation_flags_missing_coordination_ack(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_COORDINATION_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:00:00Z",
                        "status": "active",
                        "request_id": "objective-153-coordination",
                        "objective_id": "objective-153",
                        "issue_code": "publication_surface_divergence",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:00:01Z",
                        "execution_state": "waiting_on_dependency",
                        "decision_outcome": "acknowledge_and_wait_on_dependency",
                        "summary": "Request is waiting on external bridge coordination to restore canonical objective alignment.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_EXECUTION_TRUTH.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:00:02Z",
                        "recent_execution_truth": [],
                        "summary": {"execution_count": 0},
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            snapshot = mim_ui._build_tod_truth_reconciliation_snapshot(
                initiative_driver={"active_objective": {"objective_id": "153"}},
                authoritative_request={
                    "objective_id": "153",
                    "request_id": "handoff-153-77",
                    "task_id": "objective-153-task-77",
                },
                shared_root=shared_root,
            )

        self.assertEqual(snapshot["state"], "coordination_response_missing")
        self.assertTrue(snapshot["coordination_response_missing"])
        self.assertFalse(snapshot["execution_confirmed"])
        self.assertEqual(snapshot["authoritative_source"], "TOD")
        self.assertIn("waiting on MIM", snapshot["summary"])

    def test_system_activity_overrides_completion_without_tod_confirmation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:00Z",
                        "execution_state": "waiting_on_dependency",
                        "decision_outcome": "acknowledge_and_wait_on_dependency",
                        "summary": "Request is waiting on external bridge coordination to restore canonical objective alignment.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_EXECUTION_TRUTH.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:01Z",
                        "recent_execution_truth": [],
                        "summary": {"execution_count": 0},
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "completed", "summary": "MIM thinks the task is finished."},
                        "active_task": {},
                        "next_task": {},
                        "active_objective": {"objective_id": "153"},
                        "progress": {"percent": 100},
                    },
                    operator_reasoning={
                        "execution_readiness": {"execution_allowed": True, "summary": "Execution is allowed.", "gate_state": "open"},
                        "active_work": {"state": "completed", "summary": "Tracked work is complete."},
                        "stability_guard": {},
                    },
                    runtime_health={
                        "latest": {
                            "camera": {"last_seen_at": (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")},
                        },
                        "frontend_media": {
                            "camera": {"last_reported_at": (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")},
                        },
                    },
                    runtime_recovery={},
                    authoritative_request={
                        "objective_id": "153",
                        "generated_at": (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
                    },
                    collaboration_progress={
                        "generated_at": (datetime.now(timezone.utc) - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    dispatch_telemetry={
                        "dispatch_timestamp": (datetime.now(timezone.utc) - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    tod_decision_process={},
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        reconciliation = snapshot["tod_truth_reconciliation"]
        self.assertEqual(snapshot["status_code"], "idle")
        self.assertEqual(snapshot["status_label"], "IDLE")
        self.assertEqual(snapshot["authoritative_source"], "TOD")
        self.assertTrue(reconciliation["autonomous_completion_authority"])
        self.assertFalse(reconciliation["should_override_completion"])
        self.assertEqual(snapshot["execution_allowed_label"], "Allowed")
        self.assertIn("healthy", snapshot["headline"].lower())

    def test_system_activity_uses_normalized_integration_objective_for_alignment(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:00Z",
                        "execution_state": "waiting_on_dependency",
                        "decision_outcome": "acknowledge_and_wait_on_dependency",
                        "summary": "Request is waiting on external bridge coordination to restore canonical objective alignment.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_EXECUTION_TRUTH.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:01Z",
                        "recent_execution_truth": [],
                        "summary": {"execution_count": 0},
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "mim_status": {"objective_active": "665"},
                        "mim_handshake": {"current_next_objective": "665"},
                        "objective_alignment": {"mim_objective_active": "665", "tod_current_objective": "665", "status": "in_sync"},
                        "live_task_request": {
                            "objective_id": "objective-663",
                            "normalized_objective_id": "665",
                            "promotion_applied": True,
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            now = datetime.now(timezone.utc)
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "completed", "summary": "MIM thinks the task is finished."},
                        "active_task": {},
                        "next_task": {},
                        "active_objective": {"objective_id": "665"},
                        "progress": {"percent": 100},
                    },
                    operator_reasoning={
                        "execution_readiness": {"execution_allowed": True, "summary": "Execution is allowed.", "gate_state": "open"},
                        "active_work": {"state": "completed", "summary": "Tracked work is complete."},
                        "stability_guard": {},
                    },
                    runtime_health={},
                    runtime_recovery={},
                    authoritative_request={"objective_id": "objective-663", "generated_at": "2026-04-20T04:09:59Z"},
                    collaboration_progress={},
                    dispatch_telemetry={},
                    tod_decision_process={},
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertEqual(snapshot["canonical_objective_id"], "665")
        self.assertEqual(snapshot["live_request_objective_id"], "665")
        self.assertEqual(snapshot["alignment_label"], "Aligned")
        self.assertEqual(
            snapshot["alignment_summary"],
            "MIM and TOD agree on the active objective.",
        )

    def test_system_activity_uses_fresh_execution_evidence_for_heartbeat(self) -> None:
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "working", "summary": "Active bounded work is executing."},
                        "active_task": {"task_id": "objective-2834-task-6985"},
                        "next_task": {},
                        "active_objective": {"objective_id": "2834"},
                        "progress": {"percent": 42},
                    },
                    operator_reasoning={
                        "execution_readiness": {
                            "execution_allowed": True,
                            "summary": "Execution is allowed.",
                            "gate_state": "open",
                        },
                        "active_work": {"state": "working", "summary": "Tracked work is active."},
                        "stability_guard": {},
                    },
                    runtime_health={
                        "latest": {
                            "microphone": {"last_seen_at": (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z")},
                        },
                        "frontend_media": {
                            "camera": {"last_reported_at": (now - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")},
                        },
                    },
                    runtime_recovery={},
                    authoritative_request={
                        "objective_id": "2834",
                        "generated_at": (now - timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
                    },
                    collaboration_progress={
                        "generated_at": (now - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    dispatch_telemetry={
                        "dispatch_timestamp": (now - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    tod_decision_process={
                        "generated_at": (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
                    },
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertIn(snapshot["heartbeat_state"], {"fresh", "aging"})
        self.assertNotEqual(snapshot["status_code"], "frozen")
        self.assertEqual(snapshot["status_code"], "active")

    def test_system_activity_prefers_current_task_ack_wait_over_stale_tod_decision(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "status": "hard_boundary_escalated",
                        "request_id": "objective-2913-task-7144-project-3-task-2-patch-token-extraction-so-only-the-identifier-value-is-captured",
                        "task_id": "objective-2913-task-7144",
                        "decision": {
                            "reason_code": "hard_boundary_requires_human",
                            "requires_human": True,
                            "summary": "Request crosses a hard boundary and requires human escalation.",
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:52Z",
                        "request_id": "objective-2913-task-7144-project-3-task-2-patch-token-extraction-so-only-the-identifier-value-is-captured",
                        "task_id": "objective-2913-task-7144",
                        "decision_outcome": "escalate_hard_boundary",
                        "reason_code": "hard_boundary_requires_human",
                        "summary": "Request crosses a hard boundary and requires human escalation.",
                        "execution_state": "awaiting_human_boundary",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_EXECUTION_TRUTH.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:53Z",
                        "recent_execution_truth": [],
                        "summary": {"execution_count": 0},
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_DECISION_TASK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:54Z",
                        "state": "awaiting_task_ack",
                        "state_reason": "trigger_ack_current",
                        "active_task_id": "objective-2913-task-7144",
                        "objective_id": "2913",
                        "blocking_reason_codes": [],
                        "decision": {"code": "monitor_only"},
                        "decision_process": {
                            "generated_at": "2026-05-04T18:24:54Z",
                            "state": "awaiting_task_ack",
                            "state_reason": "trigger_ack_current",
                            "active_task_id": "objective-2913-task-7144",
                            "objective_id": "2913",
                            "questions": {
                                "tod_knows_what_mim_did": {"known": True, "evidence": ["trigger_ack_current"]},
                                "mim_knows_what_tod_did": {"known": True, "evidence": ["tod_coordination_request_seen"]},
                                "tod_current_work": {
                                    "known": True,
                                    "task_id": "objective-2913-task-7144",
                                    "objective_id": "2913",
                                    "phase": "tod_has_seen_request_waiting_acceptance",
                                    "detail": "review_state=awaiting_task_ack trigger=task_request_posted",
                                },
                                "tod_liveness": {
                                    "status": "alive",
                                    "ask_required": False,
                                    "latest_progress_age_seconds": 20,
                                    "ping_response_age_seconds": 338,
                                },
                            },
                            "communication_escalation": {
                                "required": False,
                                "code": "monitor_only",
                                "detail": "Keep observing the current TOD lane.",
                            },
                            "selected_action": {
                                "code": "monitor_only",
                                "detail": "No blocking action selected; continue monitoring.",
                            },
                        },
                        "communication_escalation": {
                            "required": False,
                            "code": "monitor_only",
                            "detail": "Keep observing the current TOD lane.",
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            now = datetime.now(timezone.utc)
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "working", "summary": "Current bounded task is active."},
                        "active_task": {"task_id": "objective-2913-task-7144"},
                        "next_task": {},
                        "active_objective": {"objective_id": "2913"},
                        "progress": {"percent": 25},
                    },
                    operator_reasoning={
                        "execution_readiness": {
                            "execution_allowed": False,
                            "summary": "Execution readiness artifact is older than policy allows.",
                            "gate_state": "degraded",
                        },
                        "active_work": {"state": "working", "summary": "Tracked work is active."},
                        "stability_guard": {},
                    },
                    runtime_health={
                        "latest": {
                            "camera": {"last_seen_at": (now - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")},
                        },
                        "frontend_media": {
                            "camera": {"last_reported_at": (now - timedelta(seconds=15)).isoformat().replace("+00:00", "Z")},
                        },
                    },
                    runtime_recovery={},
                    authoritative_request={
                        "objective_id": "2913",
                        "request_id": "objective-2913-task-7144-project-3-task-2-patch-token-extraction-so-only-the-identifier-value-is-captured",
                        "task_id": "objective-2913-task-7144",
                        "generated_at": (now - timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
                    },
                    collaboration_progress={
                        "generated_at": (now - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    dispatch_telemetry={
                        "dispatch_timestamp": (now - timedelta(seconds=18)).isoformat().replace("+00:00", "Z"),
                    },
                    tod_decision_process=mim_ui._operator_tod_decision_process_snapshot(shared_root),
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertEqual(snapshot["status_code"], "active")
        self.assertEqual(snapshot["status_label"], "WAITING")
        self.assertEqual(snapshot["headline"], "WAITING - current TOD lane is healthy and awaiting task ACK")
        self.assertTrue(snapshot["execution_allowed"])
        self.assertFalse(snapshot["tod_truth_reconciliation"]["requires_human"])
        self.assertTrue(snapshot["tod_truth_reconciliation"]["autonomy_override_active"])
        self.assertEqual(snapshot["relation"]["bridge_health"], "Healthy")
        self.assertEqual(snapshot["relation"]["execution_flow"], "Waiting")
        self.assertTrue(snapshot["tod_truth_reconciliation"]["current_task_waiting_on_ack"])
        self.assertIn("waiting for the live TOD task ACK", snapshot["summary"])

    def test_system_activity_does_not_block_completion_on_stale_guard_under_full_authority(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "status": "contract_violation_rejected",
                        "request_id": "objective-2912-task-008",
                        "task_id": "objective-2912-task-008",
                        "stale_guard": {
                            "detected": True,
                            "status": "execution_blocked_by_stale_guard",
                            "reason": "higher_authoritative_task_ordinal_active",
                            "high_watermark": {
                                "request_id": "objective-2912-task-7141-next-authoritative-step",
                                "ordinal": 7141,
                            },
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "idle", "summary": "No current initiative-driver task is visible."},
                        "active_task": {},
                        "next_task": {},
                        "active_objective": {"objective_id": "2912"},
                        "progress": {"percent": 100},
                    },
                    operator_reasoning={
                        "execution_readiness": {"execution_allowed": True, "summary": "Execution is allowed.", "gate_state": "open"},
                        "active_work": {"state": "completed", "summary": "Tracked work is complete."},
                        "stability_guard": {},
                    },
                    runtime_health={},
                    runtime_recovery={},
                    authoritative_request={"objective_id": "2912", "request_id": "objective-2912-task-008", "task_id": "objective-2912-task-008"},
                    collaboration_progress={},
                    dispatch_telemetry={},
                    tod_decision_process={},
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertEqual(snapshot["status_code"], "idle")
        self.assertEqual(snapshot["status_label"], "IDLE")
        self.assertEqual(snapshot["headline"], "IDLE - healthy, no live task right now")
        self.assertTrue(snapshot["execution_allowed"])
        self.assertEqual(snapshot["execution_allowed_label"], "Allowed")
        self.assertTrue(snapshot["tod_truth_reconciliation"]["autonomous_completion_authority"])
        self.assertFalse(snapshot["tod_truth_reconciliation"]["should_override_completion"])

    def test_truth_reconciliation_accepts_mim_fallback_takeover(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:00Z",
                        "execution_state": "waiting_on_dependency",
                        "decision_outcome": "acknowledge_and_wait_on_dependency",
                        "summary": "TOD has not yet confirmed execution.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_EXECUTION_TRUTH.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:10:01Z",
                        "recent_execution_truth": [],
                        "summary": {"execution_count": 0},
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TOD_FALLBACK_ACTIVATION.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-04-20T04:11:00Z",
                        "objective_id": "objective-153",
                        "task_id": "objective-153-task-77",
                        "request_id": "handoff-153-77",
                        "correlation_id": "handoff-153-77",
                        "message_kind": "fallback",
                        "sequence": 1,
                        "packet_type": "tod-mim-fallback-activation-v1",
                        "schema_version": "2026-04-02-communication-contract-v1",
                        "contract_version": "v1",
                        "source_identity": {"actor": "MIM", "service_name": "core.self_optimizer_service"},
                        "transport": {"transport_id": "mim_server_shared_artifact_boundary", "surface": "/home/testpilot/mim/runtime/shared"},
                        "fallback_reason_code": "tod_silence_direct_execution_ready",
                        "primary_transport_state": "blocked",
                        "fallback_scope": "objective-153-task-77",
                        "execution_state": "running",
                        "decision_outcome": "mim_direct_execution_takeover",
                        "summary": "MIM claimed bounded fallback authority and is executing the active task locally.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            snapshot = mim_ui._build_tod_truth_reconciliation_snapshot(
                initiative_driver={"active_objective": {"objective_id": "153"}},
                authoritative_request={
                    "objective_id": "153",
                    "request_id": "handoff-153-77",
                    "task_id": "objective-153-task-77",
                },
                shared_root=shared_root,
            )

        self.assertEqual(snapshot["state"], "execution_confirmed")
        self.assertTrue(snapshot["execution_confirmed"])
        self.assertEqual(snapshot["authoritative_source"], "MIM")
        self.assertTrue(snapshot["fallback_active"])

    def test_truth_reconciliation_rejects_mismatched_bridge_lineage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:50Z",
                        "request_id": "objective-2913-task-7144-project-3-task-2",
                        "task_id": "objective-2913-task-7144",
                        "objective_id": "2913",
                        "request_status": "queued",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:51Z",
                        "request_id": "objective-2913-task-1777951503-old",
                        "task_id": "objective-2913-task-1777951503",
                        "objective_id": "2913",
                        "status": "accepted",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-04T18:24:52Z",
                        "request_id": "objective-2913-task-1777951503-old",
                        "task_id": "objective-2913-task-1777951503",
                        "objective_id": "2913",
                        "status": "succeeded",
                        "result_status": "succeeded",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            snapshot = mim_ui._build_tod_truth_reconciliation_snapshot(
                initiative_driver={"active_objective": {"objective_id": "2913"}},
                authoritative_request={
                    "objective_id": "2913",
                    "request_id": "objective-2913-task-7144-project-3-task-2",
                    "task_id": "objective-2913-task-7144",
                },
                shared_root=shared_root,
            )

        self.assertEqual(snapshot["state"], "lineage_mismatch")
        self.assertFalse(snapshot["execution_confirmed"])
        self.assertFalse(snapshot["bridge_request_confirmed"])
        self.assertTrue(snapshot["lineage_mismatch"])

    def test_system_activity_surfaces_tod_executor_failure_remediation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "state_reason": "stalled_regression_no_delta",
                        "task": {
                            "active_task_id": "objective-2912-task-7141",
                            "objective_id": "2912",
                            "result_status": "failed",
                        },
                        "blocking_reason_codes": ["stalled_regression_no_delta"],
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "selected_action": {
                            "code": "remediate_tod_executor_failure",
                            "detail": "Treat the communication lane as healthy and remediate TOD executor stability before publishing more work.",
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "working", "summary": "Current bounded task is active."},
                        "active_task": {"task_id": "objective-2912-task-7141"},
                        "next_task": {},
                        "active_objective": {"objective_id": "2912"},
                        "progress": {"percent": 40},
                    },
                    operator_reasoning={
                        "execution_readiness": {"execution_allowed": True, "summary": "Execution is allowed.", "gate_state": "open"},
                        "active_work": {"state": "working", "summary": "Tracked work is active."},
                        "stability_guard": {},
                    },
                    runtime_health={},
                    runtime_recovery={},
                    authoritative_request={"objective_id": "2912", "request_id": "objective-2912-task-7141", "task_id": "objective-2912-task-7141"},
                    collaboration_progress={},
                    dispatch_telemetry={},
                    tod_decision_process={},
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertEqual(snapshot["status_code"], "active")
        self.assertEqual(snapshot["status_label"], "ACTIVE")
        self.assertEqual(snapshot["relation"]["bridge_health"], "Healthy")
        self.assertEqual(snapshot["relation"]["execution_flow"], "Flowing")
        self.assertEqual(snapshot["execution_allowed_label"], "Allowed")
        self.assertEqual(snapshot["summary"], "Current bounded task is active.")

    def test_system_activity_ignores_stale_guard_residue_for_older_lineage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir)
            (shared_root / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "status": "contract_violation_rejected",
                        "request_id": "objective-2912-task-7141-implement-bounded-work",
                        "task_id": "objective-2912-task-7141",
                        "stale_guard": {
                            "detected": True,
                            "status": "execution_blocked_by_stale_guard",
                            "reason": "higher_authoritative_task_ordinal_active",
                            "current_request": {
                                "request_id": "objective-2912-task-008",
                                "task_id": "objective-2912-task-008",
                            },
                            "high_watermark": {
                                "request_id": "objective-2912-task-7141-implement-bounded-work",
                                "ordinal": 7141,
                            },
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "state_reason": "stalled_regression_no_delta",
                        "task": {
                            "active_task_id": "objective-2912-task-7141",
                            "objective_id": "2912",
                            "result_status": "failed",
                        },
                        "blocking_reason_codes": ["stalled_regression_no_delta"],
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            (shared_root / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "selected_action": {
                            "code": "remediate_tod_executor_failure",
                            "detail": "Treat the communication lane as healthy and remediate TOD executor stability before publishing more work.",
                        },
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            original_shared_root = mim_ui.SHARED_RUNTIME_ROOT
            mim_ui.SHARED_RUNTIME_ROOT = shared_root
            try:
                snapshot = mim_ui._build_system_activity_snapshot(
                    initiative_driver={
                        "activity": {"state": "working", "summary": "Current bounded task is active."},
                        "active_task": {"task_id": "objective-2912-task-7141"},
                        "next_task": {},
                        "active_objective": {"objective_id": "2912"},
                        "progress": {"percent": 40},
                    },
                    operator_reasoning={
                        "execution_readiness": {"execution_allowed": True, "summary": "Execution is allowed.", "gate_state": "open"},
                        "active_work": {"state": "working", "summary": "Tracked work is active."},
                        "stability_guard": {},
                    },
                    runtime_health={},
                    runtime_recovery={},
                    authoritative_request={
                        "objective_id": "2912",
                        "request_id": "objective-2912-task-7141-implement-bounded-work",
                        "task_id": "objective-2912-task-7141",
                    },
                    collaboration_progress={},
                    dispatch_telemetry={},
                    tod_decision_process={},
                )
            finally:
                mim_ui.SHARED_RUNTIME_ROOT = original_shared_root

        self.assertEqual(snapshot["status_code"], "active")
        self.assertEqual(snapshot["status_label"], "ACTIVE")
        self.assertEqual(snapshot["relation"]["bridge_health"], "Healthy")
        self.assertEqual(snapshot["relation"]["execution_flow"], "Flowing")
        self.assertEqual(snapshot["execution_allowed_label"], "Allowed")


if __name__ == "__main__":
    unittest.main()