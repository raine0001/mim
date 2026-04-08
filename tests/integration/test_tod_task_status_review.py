import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIB_PATH = ROOT / "scripts" / "tod_status_signal_lib.py"
WATCH_SCRIPT = ROOT / "scripts" / "watch_tod_task_status_review.sh"
DASHBOARD_SCRIPT = ROOT / "scripts" / "tod_status_dashboard.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("tod_status_signal_lib", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TodTaskStatusReviewTest(unittest.TestCase):
    def test_detect_completed_stream_supersession_for_stale_backfill(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 1, 2, 2, tzinfo=timezone.utc)
        supersession = module.detect_completed_stream_supersession(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=10)),
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=8)),
                "trigger": "coordination_ack_posted",
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(hours=7)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=7)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "completed",
                "request_action_raw": "stale_backfill_ignored",
                "stale_request": {
                    "request_id": "objective-97-task-3422",
                    "task_id": "objective-97-task-3422",
                    "reason": "lower_ordinal_backfill_ignored",
                },
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                    }
                },
            },
        )

        self.assertTrue(bool(supersession["active"]))
        self.assertEqual(
            supersession["authoritative_task_id"],
            "objective-97-task-mim-arm-safe-home-207749",
        )
        self.assertEqual(supersession["stale_request_task_id"], "objective-97-task-3422")

    def test_build_system_alert_summary_detects_critical_alerts(self) -> None:
        module = load_module()
        now = datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc)
        summary = module.build_system_alert_summary(
            stale_ack_watchdog={
                "status": "alert",
                "reason": "consecutive_stale_trigger_ack_failures",
                "task_num": "3422",
                "consecutive_stale_failures": 2,
            },
            catchup_status={
                "catchup_gate_pass": False,
                "streak": {"pass_streak": 0, "target": 3},
                "confidence": "medium",
            },
            liveness_events=[
                {
                    "event": "freeze_suspected",
                    "generated_at": iso_utc(now - timedelta(minutes=1)),
                    "stale_seconds": 70000,
                }
            ],
            now=now,
        )

        self.assertTrue(bool(summary["active"]))
        self.assertEqual(summary["highest_severity"], "critical")
        alert_codes = [item["code"] for item in summary["alerts"]]
        self.assertIn("stale_trigger_ack_failures", alert_codes)
        self.assertIn("catchup_gate_blocked", alert_codes)

    def test_build_system_alert_summary_ignores_stale_freeze_events(self) -> None:
        module = load_module()
        now = datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc)
        summary = module.build_system_alert_summary(
            stale_ack_watchdog=None,
            catchup_status=None,
            liveness_events=[
                {
                    "event": "freeze_suspected",
                    "generated_at": iso_utc(now - timedelta(minutes=10)),
                    "stale_seconds": 70000,
                }
            ],
            now=now,
        )

        self.assertFalse(bool(summary["active"]))
        self.assertEqual(summary["highest_severity"], "none")
        self.assertEqual(summary["alerts"], [])

    def test_build_task_status_review_detects_idle_bridge_stall(self) -> None:
        module = load_module()
        now = datetime(2026, 3, 30, 16, 20, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=12)),
                "task_id": "objective-75-task-3271",
                "objective_id": "objective-75",
                "source_service": "objective75_overnight",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=10)),
                "trigger": "task_request_posted",
                "task_id": "objective-97-task-bridge-recovery",
                "objective_id": "objective-97",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(minutes=9)),
                "task_id": "objective-97-task-3422",
            },
            task_ack=None,
            task_result=None,
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(minutes=8)),
                "promotion_ready": False,
                "gate_pass": False,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "queued",
                "title": "Recover TOD ACK bridge and enforce dispatch readiness gate",
            },
            system_alert_summary={
                "active": True,
                "highest_severity": "critical",
                "primary_alert": {
                    "code": "stale_trigger_ack_failures",
                    "detail": "consecutive stale trigger ACK failures",
                },
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "idle_blocked")
        self.assertEqual(review["state_reason"], "task_stream_drift")
        self.assertTrue(review["idle"]["active"])
        self.assertEqual(review["task"]["active_task_id"], "objective-97-task-bridge-recovery")
        self.assertIn("task_stream_drift", review["blocking_reason_codes"])
        self.assertIn("catchup_gate_blocked", review["blocking_reason_codes"])
        self.assertIn("system_alert_critical", review["blocking_reason_codes"])
        self.assertIn("trigger_ack_not_current", review["blocking_reason_codes"])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertIn("acknowledge_and_remediate_system_alerts", action_codes)
        self.assertIn("stabilize_task_stream", action_codes)
        self.assertIn("pass_dispatch_readiness_gate", action_codes)
        self.assertIn("recover_trigger_ack_bridge", action_codes)

    def test_build_task_status_review_prefers_authoritative_completed_result_over_stale_backfill(self) -> None:
        module = load_module()
        now = datetime(2026, 3, 31, 21, 0, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=2)),
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=1)),
                "trigger": "liveness_ping",
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(minutes=1)),
                "task_id": "objective-97-task-3422",
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(hours=1, minutes=50)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=20)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "completed",
                "request_action_raw": "stale_backfill_ignored",
                "stale_request": {
                    "request_id": "objective-97-task-3422",
                    "task_id": "objective-97-task-3422",
                    "reason": "lower_ordinal_backfill_ignored",
                },
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(minutes=1)),
                "promotion_ready": False,
                "gate_pass": False,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "queued",
            },
            system_alert_summary={
                "active": True,
                "highest_severity": "critical",
                "primary_alert": {
                    "code": "catchup_gate_blocked",
                    "detail": "TOD catchup gate is failing.",
                },
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "completed")
        self.assertEqual(review["state_reason"], "task_result_current")
        self.assertEqual(
            review["task"]["active_task_id"],
            "objective-97-task-mim-arm-safe-home-207749",
        )
        self.assertEqual(
            review["task"]["authoritative_task_reason"],
            "task_result_marked_prior_request_stale",
        )
        self.assertIn("catchup_gate_blocked", review["blocking_reason_codes"])
        self.assertIn("system_alert_critical", review["blocking_reason_codes"])

    def test_build_task_status_review_avoids_false_trigger_ack_bridge_recovery_after_authoritative_completion(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 1, 2, 2, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=10)),
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
                "source_service": "objective75_overnight",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=8)),
                "trigger": "coordination_ack_posted",
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(minutes=34)),
                "task_id": "objective-97-task-3422",
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(hours=7)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=7)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "completed",
                "request_action_raw": "stale_backfill_ignored",
                "stale_request": {
                    "request_id": "objective-97-task-3422",
                    "task_id": "objective-97-task-3422",
                    "reason": "lower_ordinal_backfill_ignored",
                },
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(minutes=1)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "queued",
            },
            system_alert_summary={
                "active": False,
                "highest_severity": "none",
                "primary_alert": {},
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "completed")
        self.assertEqual(
            review["task"]["active_task_id"],
            "objective-97-task-mim-arm-safe-home-207749",
        )
        self.assertNotIn("trigger_ack_not_current", review["blocking_reason_codes"])
        self.assertEqual(review["blocking_reason_codes"], [])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertIn("stabilize_publisher_after_completion", action_codes)

    def test_build_task_status_review_classifies_terminal_executor_failure_without_transport_reissue(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 3, 17, 4, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=49)),
                "task_id": "objective-75-task-3422",
                "objective_id": "objective-75",
                "source_service": "objective75_overnight",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=47)),
                "trigger": "coordination_ack_posted",
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger_ack=None,
            task_ack={
                "generated_at": iso_utc(now - timedelta(minutes=63)),
                "request_id": "objective-97-task-mim-arm-safe-home-1775231977",
                "status": "accepted",
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                    }
                },
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=12)),
                "request_id": "objective-97-task-mim-arm-safe-home-1775231977",
                "task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                "status": "failed",
                "result_reason_code": "executor_failed",
                "execution_mode": "direct_script_exception",
                "error": "Exception of type 'System.OutOfMemoryException' was thrown.",
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(seconds=30)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "completed",
            },
            system_alert_summary={
                "active": True,
                "highest_severity": "critical",
                "primary_alert": {
                    "code": "stale_trigger_ack_failures",
                    "detail": "consecutive stale trigger ACK failures",
                },
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "failed")
        self.assertEqual(review["state_reason"], "executor_failed")
        self.assertEqual(
            review["task"]["active_task_id"],
            "objective-97-task-mim-arm-safe-home-1775231977",
        )
        self.assertEqual(
            review["task"]["authoritative_task_reason"],
            "task_ack_and_terminal_result_agree_on_authoritative_task",
        )
        self.assertIn("executor_failed", review["blocking_reason_codes"])
        self.assertIn("executor_memory_pressure", review["blocking_reason_codes"])
        self.assertNotIn("task_stream_drift", review["blocking_reason_codes"])
        self.assertNotIn("trigger_ack_not_current", review["blocking_reason_codes"])
        self.assertNotIn("task_ack_request_mismatch", review["blocking_reason_codes"])
        self.assertNotIn("task_result_request_mismatch", review["blocking_reason_codes"])
        self.assertNotIn("system_alert_critical", review["blocking_reason_codes"])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertIn("remediate_tod_executor_failure", action_codes)
        self.assertNotIn("stabilize_task_stream", action_codes)
        self.assertNotIn("reissue_task_with_matching_ack", action_codes)
        self.assertNotIn("reissue_task_with_matching_result", action_codes)

    def test_build_task_status_review_does_not_block_on_trigger_drift_after_request_cleanup(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 1, 16, 16, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(seconds=30)),
                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                "objective_id": "objective-97",
                "source_service": "mim_cleanup",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=10)),
                "trigger": "coordination_ack_posted",
                "task_id": "objective-97-task-3422",
                "objective_id": "objective-97",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(seconds=20)),
                "task_id": "objective-97-task-3422",
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(hours=7)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=6)),
                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                "status": "completed",
                "request_action_raw": "stale_backfill_ignored",
                "stale_request": {
                    "request_id": "objective-97-task-3422",
                    "task_id": "objective-97-task-3422",
                    "reason": "lower_ordinal_backfill_ignored",
                },
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(minutes=1)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "queued",
            },
            system_alert_summary={
                "active": False,
                "highest_severity": "none",
                "primary_alert": {},
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "completed")
        self.assertEqual(review["blocking_reason_codes"], [])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertIn("stabilize_task_stream", action_codes)
        self.assertIn("stabilize_publisher_after_completion", action_codes)

    def test_build_task_status_review_ignores_stale_task_ack_after_current_terminal_result(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 7, 4, 14, tzinfo=timezone.utc)
        active_task_id = "objective-115-task-mim-arm-capture-frame-20260407033825"
        stale_ack_task_id = "objective-115-task-mim-arm-safe-home-20260407030034"
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=5)),
                "task_id": active_task_id,
                "request_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=5)),
                "trigger": "task_request_posted",
                "task_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(hours=1)),
                "task_id": stale_ack_task_id,
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(hours=1)),
                "request_id": stale_ack_task_id,
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=8)),
                "request_id": active_task_id,
                "task_id": active_task_id,
                "status": "succeeded",
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": active_task_id,
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(seconds=10)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={},
            system_alert_summary={
                "active": False,
                "highest_severity": "none",
                "primary_alert": {},
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "completed")
        self.assertEqual(review["state_reason"], "task_result_current")
        self.assertNotIn("task_ack_request_mismatch", review["blocking_reason_codes"])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertNotIn("reissue_task_with_matching_ack", action_codes)

    def test_build_task_status_review_ignores_stale_persistent_task_when_live_request_advances_objective(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 7, 3, 5, tzinfo=timezone.utc)
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(seconds=30)),
                "task_id": "objective-115-task-mim-arm-safe-home-20260407030034",
                "objective_id": "objective-115",
                "source_service": "mim_arm_safe_home_dispatch",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(seconds=20)),
                "trigger": "liveness_ping",
                "task_id": "",
                "objective_id": "",
            },
            trigger_ack=None,
            task_ack=None,
            task_result=None,
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(seconds=10)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={
                "task_id": 1774884550,
                "objective_id": 97,
                "status": "completed",
                "title": "Recover TOD ACK bridge and enforce dispatch readiness gate",
            },
            system_alert_summary={
                "active": False,
                "highest_severity": "none",
                "primary_alert": {},
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "queued")
        self.assertEqual(
            review["task"]["active_task_id"],
            "objective-115-task-mim-arm-safe-home-20260407030034",
        )
        self.assertEqual(review["task"]["objective_id"], "115")
        self.assertEqual(review["task"]["persistent_task_id"], "")

    def test_build_task_status_review_recognizes_trigger_ack_acknowledges_shape(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 7, 3, 17, 30, tzinfo=timezone.utc)
        active_task_id = "objective-115-task-mim-arm-safe-home-20260407030034"
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(seconds=45)),
                "task_id": active_task_id,
                "request_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(seconds=40)),
                "trigger": "task_request_posted",
                "task_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(seconds=8)),
                "source": "shared-trigger-ack-v1",
                "status": "acknowledged",
                "acknowledges": active_task_id,
                "current_task_id": active_task_id,
                "trigger_context": {
                    "task_id": active_task_id,
                    "request_id": active_task_id,
                },
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": active_task_id,
                    }
                },
            },
            task_ack=None,
            task_result=None,
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(seconds=10)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={},
            system_alert_summary={
                "active": False,
                "highest_severity": "none",
                "primary_alert": {},
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "awaiting_task_ack")
        self.assertEqual(review["task"]["trigger_ack_task_id"], active_task_id)
        self.assertNotIn("trigger_ack_not_current", review["blocking_reason_codes"])

    def test_build_task_status_review_clears_stale_trigger_alert_after_current_lane_success(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 7, 3, 17, 30, tzinfo=timezone.utc)
        active_task_id = "objective-115-task-mim-arm-safe-home-20260407030034"
        review = module.build_task_status_review(
            task_request={
                "generated_at": iso_utc(now - timedelta(minutes=16)),
                "task_id": active_task_id,
                "request_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger={
                "generated_at": iso_utc(now - timedelta(minutes=16)),
                "trigger": "task_request_posted",
                "task_id": active_task_id,
                "objective_id": "objective-115",
            },
            trigger_ack={
                "generated_at": iso_utc(now - timedelta(minutes=15, seconds=38)),
                "acknowledges": active_task_id,
                "current_task_id": active_task_id,
            },
            task_ack={
                "generated_at": iso_utc(now - timedelta(minutes=15, seconds=20)),
                "request_id": active_task_id,
                "status": "accepted",
            },
            task_result={
                "generated_at": iso_utc(now - timedelta(seconds=10)),
                "request_id": active_task_id,
                "task_id": active_task_id,
                "status": "succeeded",
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": active_task_id,
                    }
                },
            },
            catchup_gate={
                "generated_at": iso_utc(now - timedelta(seconds=20)),
                "promotion_ready": True,
                "gate_pass": True,
            },
            troubleshooting_authority={
                "authority": {
                    "mim": {"permissions": ["read", "write"]},
                    "tod": {"permissions": ["read", "write"]},
                },
                "enforcement": {
                    "access_failure_action": "no_go",
                    "reason_code": "troubleshooting_access_denied",
                },
            },
            persistent_task={},
            system_alert_summary={
                "active": True,
                "highest_severity": "critical",
                "primary_alert": {
                    "code": "stale_trigger_ack_failures",
                    "detail": "consecutive stale trigger ACK failures",
                },
            },
            idle_seconds=120,
            now=now,
        )

        self.assertEqual(review["state"], "completed")
        self.assertEqual(review["task"]["trigger_ack_task_id"], active_task_id)
        self.assertNotIn("trigger_ack_not_current", review["blocking_reason_codes"])
        self.assertNotIn("system_alert_critical", review["blocking_reason_codes"])
        action_codes = [item["code"] for item in review["pending_actions"]]
        self.assertNotIn("acknowledge_and_remediate_system_alerts", action_codes)

    def test_build_mim_tod_decision_snapshot_detects_tod_silence(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 6, 18, 0, tzinfo=timezone.utc)
        snapshot = module.build_mim_tod_decision_snapshot(
            review={
                "generated_at": iso_utc(now),
                "state": "idle_blocked",
                "state_reason": "trigger_ack_not_current",
                "blocking_reason_codes": [
                    "trigger_ack_not_current",
                    "consume_watch_timeout",
                ],
                "task": {
                    "active_task_id": "objective-110-task-5001",
                    "objective_id": "objective-110",
                    "trigger_name": "task_request_posted",
                    "trigger_ack_task_id": "",
                    "task_ack_request_id": "",
                    "result_request_id": "",
                },
                "idle": {
                    "active": True,
                    "latest_progress_age_seconds": 240,
                },
            },
            next_action={
                "selected_action": {
                    "code": "recover_trigger_ack_bridge",
                    "detail": "Recover the trigger ack bridge.",
                }
            },
            system_alert_summary={
                "highest_severity": "warning",
                "primary_alert": {"code": "tod_freeze_suspected"},
            },
            coordination_request=None,
            coordination_ack=None,
            ping_response=None,
            console_probe=None,
        )

        self.assertFalse(snapshot["questions"]["tod_knows_what_mim_did"]["known"])
        self.assertFalse(snapshot["questions"]["mim_knows_what_tod_did"]["known"])
        self.assertEqual(snapshot["questions"]["tod_liveness"]["status"], "silent")
        self.assertTrue(bool(snapshot["communication_escalation"]["required"]))
        self.assertEqual(snapshot["communication_escalation"]["code"], "ask_tod_status_loudly")
        self.assertEqual(
            snapshot["communication_escalation"]["console_url"],
            "http://192.168.1.161:8844",
        )

    def test_build_mim_tod_decision_snapshot_records_supplemental_console_probe(self) -> None:
        module = load_module()
        now = datetime(2026, 4, 6, 18, 0, tzinfo=timezone.utc)
        snapshot = module.build_mim_tod_decision_snapshot(
            review={
                "generated_at": iso_utc(now),
                "state": "idle_blocked",
                "state_reason": "trigger_ack_not_current",
                "blocking_reason_codes": ["trigger_ack_not_current"],
                "task": {
                    "active_task_id": "objective-110-task-5001",
                    "objective_id": "objective-110",
                    "trigger_name": "task_request_posted",
                },
                "idle": {
                    "active": True,
                    "latest_progress_age_seconds": 240,
                },
            },
            next_action={
                "selected_action": {
                    "code": "recover_trigger_ack_bridge",
                    "detail": "Recover the trigger ack bridge.",
                }
            },
            system_alert_summary={
                "highest_severity": "warning",
                "primary_alert": {"code": "tod_freeze_suspected"},
            },
            coordination_request=None,
            coordination_ack=None,
            ping_response=None,
            console_probe={
                "generated_at": iso_utc(now - timedelta(seconds=30)),
                "status": "reachable",
                "http_status": 200,
            },
            now=now,
        )

        self.assertEqual(snapshot["questions"]["tod_liveness"]["console_probe_status"], "reachable")
        self.assertEqual(snapshot["questions"]["tod_liveness"]["console_probe_http_status"], 200)
        self.assertEqual(snapshot["communication_escalation"]["supplemental_console_probe"]["status"], "reachable")
        self.assertFalse(bool(snapshot["communication_escalation"]["supplemental_console_probe"]["authoritative"]))
        self.assertIn("tod_console_probe_recent", snapshot["questions"]["mim_knows_what_tod_did"]["evidence"])

    def test_watch_tod_task_status_review_emits_artifact(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15)),
                        "task_id": "objective-75-task-3271",
                        "objective_id": "objective-75",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=14)),
                        "trigger": "task_request_posted",
                        "task_id": "objective-97-task-bridge-recovery",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=13)),
                        "task_id": "objective-97-task-3422",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=12)),
                        "promotion_ready": False,
                        "gate_pass": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 97,
                        "status": "queued",
                        "title": "Recover TOD ACK bridge and enforce dispatch readiness gate",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                    "IDLE_SECONDS": "60",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            review = json.loads(
                (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review["state"], "idle_blocked")
            self.assertEqual(review["task"]["active_task_id"], "objective-97-task-bridge-recovery")
            self.assertIn("trigger_ack_not_current", review["blocking_reason_codes"])
            self.assertTrue(review["pending_actions"])

            system_alerts = json.loads(
                (shared_dir / "MIM_SYSTEM_ALERTS.latest.json").read_text(encoding="utf-8")
            )
            self.assertIn("active", system_alerts)
            self.assertIn("highest_severity", system_alerts)

            next_action = json.loads(
                (shared_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(next_action["state"], "idle_blocked")
            self.assertTrue(bool(next_action["escalation_recommended"]))
            self.assertIn("system_alerts", next_action)
            self.assertEqual(
                next_action["selected_action"]["code"],
                "stabilize_task_stream",
            )

            decision_task = json.loads(
                (shared_dir / "MIM_DECISION_TASK.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(decision_task["owner_actor"], "MIM")
            self.assertEqual(decision_task["target_actor"], "TOD")
            self.assertEqual(decision_task["decision"]["decision_owner"], "MIM")
            self.assertEqual(decision_task["decision"]["code"], "stabilize_task_stream")
            self.assertIn("decision_process", decision_task)
            self.assertIn("communication_escalation", decision_task)
            self.assertFalse(
                bool(
                    decision_task["decision_process"]["questions"]["tod_knows_what_mim_did"]["known"]
                )
            )
            self.assertEqual(
                decision_task["communication_escalation"]["code"],
                "ask_tod_status_loudly",
            )
            self.assertEqual(
                decision_task["communication_escalation"]["console_url"],
                "http://192.168.1.161:8844",
            )

    def test_watch_tod_task_status_review_increments_escalation_cycles(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15)),
                        "task_id": "objective-110-task-5001",
                        "objective_id": "objective-110",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=14)),
                        "trigger": "task_request_posted",
                        "task_id": "objective-110-task-5001",
                        "objective_id": "objective-110",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CONSOLE_PROBE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=20)),
                        "type": "tod_console_probe_v1",
                        "status": "reachable",
                        "http_status": 200,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_DECISION_TASK.latest.json").write_text(
                json.dumps(
                    {
                        "communication_escalation": {
                            "required": True,
                            "required_cycle_count": 3,
                            "block_dispatch_threshold_cycles": 3,
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 110,
                        "status": "queued",
                        "title": "Recover TOD communication lane",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                    "IDLE_SECONDS": "60",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            decision_task = json.loads(
                (shared_dir / "MIM_DECISION_TASK.latest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(decision_task["communication_escalation"]["required_cycle_count"], 4)
        self.assertEqual(decision_task["communication_escalation"]["block_dispatch_threshold_cycles"], 3)
        self.assertEqual(
            decision_task["decision_process"]["questions"]["tod_liveness"]["console_probe_status"],
            "reachable",
        )

    def test_watch_tod_task_status_review_refreshes_alignment_artifacts_when_live_request_advances(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=30)),
                        "task_id": "objective-115-task-mim-arm-safe-home-20260407030034",
                        "objective_id": "objective-115",
                        "request_id": "objective-115-task-mim-arm-safe-home-20260407030034",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {
                        "objective_active": 110,
                        "objective_in_flight": 110,
                        "current_next_objective": 110,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "objective_alignment": {
                            "status": "in_sync",
                            "tod_current_objective": 110,
                            "mim_objective_active": 110,
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=20)),
                        "promotion_ready": True,
                        "gate_pass": True,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 97,
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                    "AUTO_REFRESH_ALIGNMENT": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            alignment_request = json.loads(
                (shared_dir / "MIM_TOD_ALIGNMENT_REQUEST.latest.json").read_text(encoding="utf-8")
            )
            integration = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            review = json.loads(
                (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(str(alignment_request["mim_truth"]["objective_active"]), "152")
            self.assertEqual(
                str(integration["objective_alignment"]["mim_objective_active"]),
                "152",
            )
            self.assertEqual(review["task"]["objective_id"], "115")

    def test_watch_tod_task_status_review_clears_stale_trigger_alert_after_current_success(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            active_task_id = "objective-115-task-mim-arm-safe-home-20260407030034"

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=16)),
                        "task_id": active_task_id,
                        "request_id": active_task_id,
                        "objective_id": "objective-115",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=16)),
                        "trigger": "task_request_posted",
                        "task_id": active_task_id,
                        "objective_id": "objective-115",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15, seconds=38)),
                        "source": "shared-trigger-ack-v1",
                        "status": "acknowledged",
                        "acknowledges": active_task_id,
                        "current_task_id": active_task_id,
                        "trigger_context": {
                            "task_id": active_task_id,
                            "request_id": active_task_id,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15, seconds=20)),
                        "request_id": active_task_id,
                        "task_id": active_task_id,
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=10)),
                        "request_id": active_task_id,
                        "task_id": active_task_id,
                        "status": "succeeded",
                        "bridge_runtime": {
                            "current_processing": {
                                "task_id": active_task_id,
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=20)),
                        "promotion_ready": True,
                        "gate_pass": True,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (log_dir / "objective75_stale_ack_watchdog.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=5)),
                        "status": "alert",
                        "reason": "consecutive_stale_trigger_ack_failures",
                        "task_num": "3422",
                        "consecutive_stale_failures": 2,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 115,
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            system_alerts = json.loads(
                (shared_dir / "MIM_SYSTEM_ALERTS.latest.json").read_text(encoding="utf-8")
            )
            review = json.loads(
                (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").read_text(encoding="utf-8")
            )
            next_action = json.loads(
                (shared_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").read_text(encoding="utf-8")
            )

            self.assertFalse(bool(system_alerts["active"]))
            self.assertEqual(system_alerts["highest_severity"], "none")
            self.assertEqual(review["blocking_reason_codes"], [])
            self.assertFalse(bool(review["system_alerts"]["active"]))
            self.assertEqual(review["system_alerts"]["highest_severity"], "none")
            self.assertFalse(bool(next_action["escalation_recommended"]))
            self.assertEqual(next_action["selected_action"]["code"], "monitor_only")


    def test_watch_tod_task_status_review_ignores_consume_timeout_after_completion(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15)),
                        "task_id": "objective-97-task-3422",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=14)),
                        "trigger": "coordination_ack_posted",
                        "task_id": "objective-97-task-3422",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=1)),
                        "task_id": "objective-97-task-3422",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(hours=5)),
                        "request_id": "objective-97-task-mim-arm-safe-home-207749",
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=10)),
                        "request_id": "objective-97-task-mim-arm-safe-home-207749",
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                        "status": "completed",
                        "request_action_raw": "stale_backfill_ignored",
                        "stale_request": {
                            "request_id": "objective-97-task-3422",
                            "task_id": "objective-97-task-3422",
                            "reason": "lower_ordinal_backfill_ignored",
                        },
                        "bridge_runtime": {
                            "current_processing": {
                                "task_id": "objective-97-task-mim-arm-safe-home-207749",
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=1)),
                        "promotion_ready": True,
                        "gate_pass": True,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_CONSUME_EVIDENCE.latest.json").write_text(
                json.dumps(
                    {
                        "task_id": "objective-97-task-mim-arm-safe-home-207749",
                        "watch": {
                            "phase": "timeout",
                            "timed_out": True,
                        },
                        "first_mutations": {
                            "task_ack": None,
                            "task_result": {
                                "request_id": "objective-97-task-mim-arm-safe-home-207749",
                                "status": "completed",
                            },
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 97,
                        "status": "queued",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            review = json.loads(
                (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review["state"], "completed")
            self.assertNotIn("consume_watch_timeout", review["blocking_reason_codes"])

    def test_watch_tod_task_status_review_pins_executor_memory_pressure_incident(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            task_state_file = root / "tasks.json"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=1)),
                        "task_id": "objective-97-task-smoke-20260403171131",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=50)),
                        "trigger": "task_request_posted",
                        "task_id": "objective-97-task-smoke-20260403171131",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=5)),
                        "request_id": "objective-97-task-mim-arm-safe-home-1775231977",
                        "status": "accepted",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=10)),
                        "request_id": "objective-97-task-mim-arm-safe-home-1775231977",
                        "task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                        "status": "failed",
                        "result_reason_code": "executor_failed",
                        "execution_mode": "direct_script_exception",
                        "error": "Exception of type 'System.OutOfMemoryException' was thrown.",
                        "bridge_runtime": {
                            "current_processing": {
                                "task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(seconds=30)),
                        "promotion_ready": True,
                        "gate_pass": True,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            task_state_file.write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 97,
                        "status": "completed",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(WATCH_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "TASK_STATE_FILE": str(task_state_file),
                    "RUN_ONCE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            incident = json.loads(
                (shared_dir / "MIM_OPERATOR_INCIDENT.latest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(bool(incident["active"]))
            self.assertEqual(incident["communication"]["state"], "healthy")
            self.assertEqual(incident["execution"]["state"], "failed")
            self.assertEqual(incident["execution"]["failure"], "executor_failed")
            self.assertEqual(incident["execution"]["subtype"], "executor_memory_pressure")

            pinned_review = json.loads(Path(incident["review_path"]).read_text(encoding="utf-8"))
            pinned_next_action = json.loads(Path(incident["next_action_path"]).read_text(encoding="utf-8"))
            pinned_decision = json.loads(Path(incident["decision_task_path"]).read_text(encoding="utf-8"))
            self.assertEqual(pinned_review["state_reason"], "executor_failed")
            self.assertEqual(
                pinned_next_action["selected_action"]["code"],
                "remediate_tod_executor_failure",
            )
            self.assertEqual(
                pinned_decision["decision"]["code"],
                "remediate_tod_executor_failure",
            )

    def test_tod_status_dashboard_prints_task_review(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root
            tod_state_dir = root / "tod" / "state"
            tod_state_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=15)),
                        "task_id": "objective-75-task-3271",
                        "objective_id": "objective-75",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=14)),
                        "trigger": "task_request_posted",
                        "task_id": "objective-97-task-bridge-recovery",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=13)),
                        "task_id": "objective-97-task-3422",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_CATCHUP_GATE.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=12)),
                        "promotion_ready": False,
                        "gate_pass": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json").write_text(
                json.dumps(
                    {
                        "authority": {
                            "mim": {"permissions": ["read", "write"]},
                            "tod": {"permissions": ["read", "write"]},
                        },
                        "enforcement": {
                            "access_failure_action": "no_go",
                            "reason_code": "troubleshooting_access_denied",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps({"compatible": True}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_LOOP_JOURNAL.latest.json").write_text(
                json.dumps({"ok": True}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps({}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps({"mim_refresh": {}}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_COORDINATION_ACK.latest.json").write_text(
                json.dumps({}, indent=2) + "\n",
                encoding="utf-8",
            )
            (tod_state_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "task_id": 1774884550,
                        "objective_id": 97,
                        "status": "queued",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(DASHBOARD_SCRIPT)],
                cwd=root,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "STALE_SECONDS": "999999",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("task_review_state: idle_blocked", completed.stdout)
            self.assertIn("task_review_reason: task_stream_drift", completed.stdout)
            self.assertIn("blocking_reasons: task_stream_drift, catchup_gate_blocked, trigger_ack_not_current", completed.stdout)

    def test_tod_status_dashboard_prefers_active_operator_incident(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root
            incidents_dir = shared_dir / "incidents"
            tod_state_dir = root / "tod" / "state"
            incidents_dir.mkdir(parents=True, exist_ok=True)
            tod_state_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=2)),
                        "task_id": "objective-97-task-smoke-20260403171131",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TO_TOD_TRIGGER.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now - timedelta(minutes=2)),
                        "trigger": "task_request_posted",
                        "task_id": "objective-97-task-smoke-20260403171131",
                        "objective_id": "objective-97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-97-task-smoke-20260403171131",
                        "status": "succeeded",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps({"mim_refresh": {}}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_LOOP_JOURNAL.latest.json").write_text(
                json.dumps({"ok": True}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps({}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_COORDINATION_ACK.latest.json").write_text(
                json.dumps({}, indent=2) + "\n",
                encoding="utf-8",
            )
            (tod_state_dir / "tasks.json").write_text(
                json.dumps({"task_id": 1774884550, "objective_id": 97, "status": "completed"}, indent=2) + "\n",
                encoding="utf-8",
            )

            incident_review_path = incidents_dir / "objective-97-executor_memory_pressure.review.json"
            incident_review_path.write_text(
                json.dumps(
                    {
                        "generated_at": iso_utc(now),
                        "task": {
                            "active_task_id": "objective-97-task-mim-arm-safe-home-1775231977",
                            "objective_id": "97",
                            "trigger_name": "coordination_ack_posted",
                        },
                        "state": "failed",
                        "state_reason": "executor_failed",
                        "blocking_reason_codes": ["executor_failed", "executor_memory_pressure"],
                        "idle": {"active": False, "latest_progress_age_seconds": 11},
                        "pending_actions": [
                            {
                                "code": "remediate_tod_executor_failure",
                                "detail": "Treat the communication lane as healthy and remediate TOD executor stability before publishing more work.",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_OPERATOR_INCIDENT.latest.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "precedence": "prefer_incident_over_latest",
                        "review_path": str(incident_review_path),
                        "communication": {"state": "healthy"},
                        "execution": {
                            "state": "failed",
                            "failure": "executor_failed",
                            "subtype": "executor_memory_pressure",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(DASHBOARD_SCRIPT)],
                cwd=root,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "STALE_SECONDS": "999999",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("task_review_state: failed", completed.stdout)
            self.assertIn("task_review_reason: executor_failed", completed.stdout)
            self.assertIn("communication_state: healthy", completed.stdout)
            self.assertIn("execution_subtype: executor_memory_pressure", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)