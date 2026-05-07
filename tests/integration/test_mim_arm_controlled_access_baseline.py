from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.models import CapabilityRegistration
from core.routers import mim_arm


class _FakeExecuteResult:
    def __init__(self, first_value=None):
        self._first_value = first_value

    def scalars(self):
        return self

    def first(self):
        return self._first_value


class _FakeDB:
    def __init__(self):
        self.capabilities: dict[str, CapabilityRegistration] = {}
        self.added: list[object] = []
        self._next_id = 100

    async def execute(self, stmt):
        try:
            params = stmt.compile().params
        except Exception:
            params = {}
        capability_name = ""
        if params:
            capability_name = str(next(iter(params.values())) or "").strip()
        return _FakeExecuteResult(self.capabilities.get(capability_name))

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            self._next_id += 1
            obj.id = self._next_id
        self.added.append(obj)
        if isinstance(obj, CapabilityRegistration):
            self.capabilities[obj.capability_name] = obj

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


class MimArmControlledAccessBaselineTest(unittest.IsolatedAsyncioTestCase):
    def test_resolve_execution_action_name_keeps_action_explicit(self):
        execution = SimpleNamespace(arguments_json={"target_pose": "scan_pose"})

        self.assertEqual(mim_arm._resolve_execution_action_name(execution), "scan_pose")

    def test_resolve_execution_action_name_uses_explicit_action_field(self):
        execution = SimpleNamespace(arguments_json={"action": "capture_frame"})

        self.assertEqual(mim_arm._resolve_execution_action_name(execution), "capture_frame")

    def test_resolve_execution_action_name_does_not_fallback_to_safe_home(self):
        execution = SimpleNamespace(arguments_json={})

        self.assertEqual(mim_arm._resolve_execution_action_name(execution), "")

    def test_status_surface_prefers_direct_artifact_and_keeps_stable_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "host_timestamp": "2026-03-29T20:00:00Z",
                        "source_host": "mim-arm-pi",
                        "uptime": {"seconds": 1234.5},
                        "ui_process_alive": True,
                        "controller_connected": True,
                        "arm_online": True,
                        "arm_status": "online",
                        "app_alive": True,
                        "camera_online": True,
                        "camera_status": "online",
                        "estop_ok": True,
                        "estop_status": "clear",
                        "mode": "idle",
                        "current_pose": "safe_home",
                        "servo_states": {"base": "ready", "wrist": "ready"},
                        "serial_ready": True,
                        "last_command_status": "success",
                        "last_command_result": {"status": "success", "command": "safe_home"},
                        "last_error": None,
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "TOD is ready.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                            "authoritative": True,
                            "evaluated_action": "safe_home",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {"alignment_status": "in_sync"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)

        self.assertTrue(surface["arm_online"])
        self.assertEqual(surface["source_host"], "mim-arm-pi")
        self.assertTrue(surface["ui_process_alive"])
        self.assertTrue(surface["controller_connected"])
        self.assertEqual(surface["current_pose"], "safe_home")
        self.assertEqual(surface["mode"], "idle")
        self.assertEqual(surface["servo_states"]["base"], "ready")
        self.assertTrue(surface["estop_state_explicit"])
        self.assertTrue(surface["tod_execution_allowed"])
        self.assertEqual(surface["tod_execution_block_reason"], "")
        self.assertTrue(surface["motion_allowed"])
        self.assertEqual(surface["motion_block_reasons"], [])
        self.assertEqual(surface["last_command_status"], "success")

    def test_status_surface_falls_back_to_diagnostic_when_direct_status_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_DIAGNOSTIC_ARTIFACT).write_text(
                json.dumps(
                    {
                        "connectivity": {"host_reachable": True},
                        "process_service": {
                            "active_processes": {"ok": True, "stdout": "mim_arm_ui"}
                        },
                        "devices": {
                            "camera_device_availability": {"ok": True},
                            "serial_controller_port_availability": {"ok": True},
                        },
                        "likely_root_cause": {"summary": ""},
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "TOD readiness valid but estop not yet surfaced.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {"alignment_status": "in_sync"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)

        self.assertTrue(surface["arm_online"])
        self.assertTrue(surface["app_alive"])
        self.assertTrue(surface["camera_online"])
        self.assertTrue(surface["serial_ready"])
        self.assertIsNone(surface["estop_ok"])
        self.assertFalse(surface["estop_state_explicit"])
        self.assertEqual(surface["tod_execution_block_reason"], "readiness_not_authoritative")
        self.assertFalse(surface["tod_execution_allowed"])
        self.assertFalse(surface["motion_allowed"])
        self.assertIn("estop_not_confirmed", surface["motion_block_reasons"])
        self.assertIn("tod_execution_not_allowed", surface["motion_block_reasons"])
        self.assertEqual(surface["current_pose"], "unknown")

    def test_status_surface_prefers_real_host_state_artifact_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_HOST_STATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "host_timestamp": "2026-03-29T20:01:00Z",
                        "source_host": "mim-arm-pi",
                        "uptime": {"seconds": 5678.0},
                        "ui_process_alive": True,
                        "controller_connected": True,
                        "arm_online": True,
                        "app_alive": True,
                        "current_pose": "safe_home",
                        "servo_states": {"base": "holding"},
                        "camera_online": True,
                        "camera_status": "online",
                        "estop_ok": True,
                        "estop_status": "clear",
                        "mode": "idle",
                        "serial_ready": True,
                        "last_command_status": "success",
                        "last_command_result": {"status": "success", "command": "safe_home"},
                        "last_error": None,
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "TOD is ready.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                            "authoritative": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {"alignment_status": "in_sync"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)

        self.assertEqual(surface["source_host"], "mim-arm-pi")
        self.assertEqual(surface["current_pose"], "safe_home")
        self.assertEqual(surface["servo_states"]["base"], "holding")
        self.assertTrue(surface["controller_connected"])

    def test_status_surface_preserves_structured_pose_from_host_state_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_HOST_STATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "host_timestamp": "2026-03-31T03:02:11Z",
                        "source_host": "mim-arm-pi",
                        "arm_online": True,
                        "app_alive": True,
                        "camera_online": True,
                        "camera_status": "online",
                        "estop_ok": True,
                        "estop_status": "sim_clear",
                        "mode": "development",
                        "current_pose": [116, 62, 62, 95, 53, 91],
                        "serial_ready": True,
                        "last_command_result": {
                            "commands_total": 84,
                            "acks_total": 84,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-31T03:02:10Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "TOD is ready.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                            "authoritative": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-31T03:02:10Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {"alignment_status": "in_sync"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)

        self.assertEqual(surface["mode"], "development")
        self.assertEqual(surface["current_pose"], [116, 62, 62, 95, 53, 91])
        self.assertEqual(surface["last_command_result"]["commands_total"], 84)

    def test_proposal_only_safe_home_surfaces_postures_without_live_dispatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "arm_online": True,
                        "camera_online": True,
                        "estop_ok": True,
                        "mode": "idle",
                        "current_pose": "scan_pose",
                        "serial_ready": True,
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "TOD is ready.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {"alignment_status": "in_sync"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "degraded"},
            ):
                proposal = mim_arm.build_mim_arm_proposal(
                    action_name="safe_home",
                    capability_name="mim_arm.propose_safe_home",
                    target_pose="safe_home",
                    shared_root=root,
                )

        self.assertEqual(proposal["stage"], "proposal_only")
        self.assertEqual(proposal["proposal"]["requested_executor"], "tod")
        self.assertFalse(proposal["live_dispatch_allowed"])
        self.assertTrue(proposal["operator_approval_required"])
        self.assertEqual(proposal["health_posture"]["status"], "degraded")
        self.assertTrue(proposal["health_posture"]["requires_confirmation"])
        self.assertIn("lowest-risk first live motion", proposal["reasoning"])

    async def test_capability_bootstrap_is_idempotent(self):
        db = _FakeDB()

        with patch.object(mim_arm, "write_journal", AsyncMock()):
            first = await mim_arm.bootstrap_mim_arm_capabilities(db=db)
            second = await mim_arm.bootstrap_mim_arm_capabilities(db=db)

        self.assertEqual(
            len(first["registered_capabilities"]),
            len(mim_arm.MIM_ARM_CAPABILITY_DEFINITIONS),
        )
        self.assertEqual(
            len(second["registered_capabilities"]),
            len(mim_arm.MIM_ARM_CAPABILITY_DEFINITIONS),
        )
        self.assertEqual(
            set(db.capabilities.keys()),
            {
                "mim_arm.get_status",
                "mim_arm.get_control_readiness",
                "mim_arm.refresh_status",
                "mim_arm.get_pose",
                "mim_arm.get_camera_state",
                "mim_arm.get_last_execution",
                "mim_arm.propose_safe_home",
                "mim_arm.propose_scan_pose",
                "mim_arm.propose_capture_frame",
                "mim_arm.execute_safe_home",
                "mim_arm.execute_scan_pose",
                "mim_arm.execute_capture_frame",
            },
        )

    def test_control_readiness_advertises_all_bounded_live_actions(self):
        readiness = mim_arm.build_mim_arm_control_readiness(
            {
                "arm_online": True,
                "app_alive": True,
                "source_host": "raspberrypi",
                "host_timestamp": datetime.now(timezone.utc).isoformat(),
                "arm_state_probe": {"available": True},
                "serial_ready": True,
                "tod_execution_allowed": True,
                "motion_allowed": True,
                "motion_block_reasons": [],
                "estop_ok": True,
                "estop_supported": True,
                "source_artifacts": {
                    "arm_status": __file__,
                    "arm_host_state": __file__,
                },
            }
        )

        self.assertEqual(
            readiness["current_authority"]["allowed_live_actions"],
            ["safe_home", "scan_pose", "capture_frame"],
        )
        self.assertIn("capture_frame", readiness["recommended_next_step"])

    async def test_execute_safe_home_requires_confirmation_without_explicit_approval(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": "scan_pose",
            "mode": "idle",
            "tod_execution_allowed": True,
            "motion_allowed": True,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=501,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="requires_confirmation",
            status="pending_confirmation",
            reason="operator_approval_required",
            feedback_json={"execution_policy_gate": {"dispatch_decision": "requires_confirmation"}},
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ) as binding_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="prepare first live move",
                    explicit_operator_approval=False,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        self.assertEqual(response["resolution"]["outcome"], "requires_confirmation")
        self.assertEqual(response["resolution"]["reason"], "operator_approval_required")
        self.assertEqual(response["execution"]["dispatch_decision"], "requires_confirmation")
        self.assertEqual(response["execution"]["status"], "pending_confirmation")
        self.assertEqual(response["execution"]["requested_executor"], "tod")

    async def test_execute_safe_home_explicit_approval_preserves_both_signals(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": "scan_pose",
            "mode": "idle",
            "tod_execution_allowed": True,
            "motion_allowed": False,
            "self_health": {
                "status": "degraded",
                "requires_confirmation": True,
                "summary": "Self-health is degraded; live arm execution remains confirmation-gated.",
            },
        }
        fake_execution = SimpleNamespace(
            id=777,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="auto_dispatch",
            status="dispatched",
            reason="approved_for_dispatch",
            arguments_json={"target_pose": "safe_home", "action": "safe_home"},
            feedback_json={
                "execution_policy_gate": {"dispatch_decision": "auto_dispatch"},
                "arm_execution": {"approval_state": "explicit_operator_approval"},
            },
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ) as binding_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="approved despite bounded physical risk",
                    explicit_operator_approval=True,
                    shared_workspace_active=True,
                ),
                db=db,
            )

        governance = response["resolution"]["metadata_json"].get("governance", {})
        self.assertEqual(response["resolution"]["outcome"], "auto_execute")
        self.assertEqual(response["resolution"]["reason"], "explicit_operator_approval")
        self.assertEqual(response["execution"]["dispatch_decision"], "auto_dispatch")
        self.assertEqual(governance.get("primary_signal"), "explicit_operator_approval")
        self.assertIn("user_action_safety_risk", governance.get("signal_codes", []))
        self.assertIn("system_health_degraded", governance.get("signal_codes", []))
        self.assertIn("Additionally:", str(governance.get("summary", "")))
        self.assertTrue(bool(binding_mock.await_args.kwargs.get("force_dispatch")))

    async def test_execute_safe_home_explicit_approval_publishes_tod_bridge_projection(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": [116, 62, 62, 95, 53, 91],
            "mode": "development",
            "tod_execution_allowed": True,
            "motion_allowed": True,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=990,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="auto_dispatch",
            status="dispatched",
            reason="approved_for_dispatch",
            feedback_json={
                "execution_policy_gate": {"dispatch_decision": "auto_dispatch"},
            },
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ), patch.object(
            mim_arm,
            "publish_mim_arm_execution_to_tod",
            return_value={"task_id": "objective-97-task-mim-arm-safe-home-990", "local_written": True},
        ) as publish_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="publish bridge projection",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        publish_mock.assert_called_once()
        self.assertEqual(
            response["execution"]["bridge_publication"]["task_id"],
            "objective-97-task-mim-arm-safe-home-990",
        )
        self.assertTrue(
            response["execution"]["feedback_json"]["tod_bridge_publication"]["local_written"]
        )

    async def test_execute_scan_pose_explicit_approval_publishes_tod_bridge_projection(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": "safe_home",
            "mode": "development",
            "tod_execution_allowed": True,
            "motion_allowed": True,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=991,
            capability_name="mim_arm.execute_scan_pose",
            requested_executor="tod",
            dispatch_decision="auto_dispatch",
            status="dispatched",
            reason="approved_for_dispatch",
            feedback_json={
                "execution_policy_gate": {"dispatch_decision": "auto_dispatch"},
            },
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ), patch.object(
            mim_arm,
            "publish_mim_arm_execution_to_tod",
            return_value={"task_id": "objective-109-task-mim-arm-scan-pose-991", "local_written": True},
        ) as publish_mock:
            response = await mim_arm.execute_scan_pose(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="publish scan pose bridge projection",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        publish_mock.assert_called_once()
        self.assertEqual(response["resolution"]["outcome"], "auto_execute")
        self.assertEqual(response["execution"]["capability_name"], "mim_arm.execute_scan_pose")
        self.assertEqual(
            response["execution"]["bridge_publication"]["task_id"],
            "objective-109-task-mim-arm-scan-pose-991",
        )
        self.assertTrue(
            response["execution"]["feedback_json"]["tod_bridge_publication"]["local_written"]
        )

    async def test_execute_capture_frame_explicit_approval_publishes_tod_bridge_projection(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": "scan_pose",
            "mode": "development",
            "tod_execution_allowed": True,
            "motion_allowed": True,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=992,
            capability_name="mim_arm.execute_capture_frame",
            requested_executor="tod",
            dispatch_decision="auto_dispatch",
            status="dispatched",
            reason="approved_for_dispatch",
            feedback_json={
                "execution_policy_gate": {"dispatch_decision": "auto_dispatch"},
            },
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ), patch.object(
            mim_arm,
            "publish_mim_arm_execution_to_tod",
            return_value={"task_id": "objective-110-task-mim-arm-capture-frame-992", "local_written": True},
        ) as publish_mock:
            response = await mim_arm.execute_capture_frame(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="publish capture frame bridge projection",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        publish_mock.assert_called_once()
        self.assertEqual(response["resolution"]["outcome"], "auto_execute")
        self.assertEqual(response["execution"]["capability_name"], "mim_arm.execute_capture_frame")
        self.assertEqual(
            response["execution"]["bridge_publication"]["task_id"],
            "objective-110-task-mim-arm-capture-frame-992",
        )
        self.assertTrue(
            response["execution"]["feedback_json"]["tod_bridge_publication"]["local_written"]
        )

    async def test_execute_safe_home_blocks_when_tod_readiness_is_false_even_with_approval(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "current_pose": "scan_pose",
            "mode": "idle",
            "tod_execution_allowed": False,
            "motion_allowed": False,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=880,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="blocked",
            status="blocked",
            reason="execution_readiness_blocked",
            feedback_json={"execution_policy_gate": {"dispatch_decision": "blocked"}},
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ) as binding_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="try despite readiness block",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        self.assertEqual(response["resolution"]["outcome"], "blocked")
        self.assertEqual(response["resolution"]["reason"], "execution_readiness_blocked")
        self.assertEqual(response["execution"]["dispatch_decision"], "blocked")
        self.assertEqual(response["execution"]["status"], "blocked")
        self.assertIn("TOD readiness is blocked", response["resolution"]["clarification_prompt"])
        self.assertFalse(bool(binding_mock.await_args.kwargs.get("force_dispatch")))

    async def test_execute_safe_home_blocks_when_estop_is_not_clear_even_with_approval(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": False,
            "estop_supported": True,
            "current_pose": "scan_pose",
            "mode": "idle",
            "tod_execution_allowed": True,
            "motion_allowed": False,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=881,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="blocked",
            status="blocked",
            reason="execution_readiness_blocked",
            feedback_json={"execution_policy_gate": {"dispatch_decision": "blocked"}},
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ) as binding_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="try despite estop block",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        self.assertEqual(response["resolution"]["outcome"], "blocked")
        self.assertEqual(response["resolution"]["reason"], "execution_readiness_blocked")
        self.assertEqual(response["execution"]["dispatch_decision"], "blocked")
        self.assertIn("Emergency-stop state is not explicitly confirmed clear", response["resolution"]["clarification_prompt"])
        self.assertFalse(bool(binding_mock.await_args.kwargs.get("force_dispatch")))

    async def test_execute_safe_home_blocks_when_communication_escalation_persists(self):
        db = _FakeDB()
        status_surface = {
            "arm_online": True,
            "camera_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "estop_supported": True,
            "current_pose": "scan_pose",
            "mode": "idle",
            "tod_execution_allowed": False,
            "tod_execution_block_reason": "communication_escalation_persistent",
            "motion_allowed": False,
            "self_health": {
                "status": "healthy",
                "requires_confirmation": False,
                "summary": "Self-health is healthy; bounded arm proposals remain eligible for TOD review.",
            },
        }
        fake_execution = SimpleNamespace(
            id=882,
            capability_name="mim_arm.execute_safe_home",
            requested_executor="tod",
            dispatch_decision="blocked",
            status="blocked",
            reason="execution_readiness_blocked",
            feedback_json={"execution_policy_gate": {"dispatch_decision": "blocked"}},
        )

        with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
            mim_arm.gateway_router,
            "_create_or_update_execution_binding",
            AsyncMock(return_value=fake_execution),
        ) as binding_mock:
            response = await mim_arm.execute_safe_home(
                payload=mim_arm.MimArmExecuteSafeHomeRequest(
                    actor="operator",
                    reason="try despite persistent communication escalation",
                    explicit_operator_approval=True,
                    shared_workspace_active=False,
                ),
                db=db,
            )

        self.assertEqual(response["resolution"]["outcome"], "blocked")
        self.assertEqual(response["resolution"]["reason"], "execution_readiness_blocked")
        self.assertIn("communication_escalation_persistent", response["resolution"]["clarification_prompt"])
        self.assertFalse(bool(binding_mock.await_args.kwargs.get("force_dispatch")))

    def test_control_readiness_surfaces_access_management_and_control_blockers(self):
        status_surface = {
            "arm_online": True,
            "app_alive": True,
            "source_host": "raspberrypi",
            "host_timestamp": datetime.now(timezone.utc).isoformat(),
            "arm_state_probe": {"available": True},
            "serial_ready": True,
            "tod_execution_allowed": True,
            "tod_execution_block_reason": "",
            "motion_allowed": False,
            "motion_block_reasons": ["estop_not_supported"],
            "estop_ok": None,
            "estop_supported": False,
            "source_artifacts": {
                "arm_status": __file__,
                "arm_host_state": __file__,
            },
        }

        readiness = mim_arm.build_mim_arm_control_readiness(status_surface)

        self.assertTrue(readiness["access"]["ready"])
        self.assertTrue(readiness["management"]["ready"])
        self.assertFalse(readiness["control"]["ready"])
        self.assertIn("estop_not_supported", readiness["control"]["blockers"])
        self.assertIn("emergency-stop", str(readiness["recommended_next_step"]).lower())

    def test_control_readiness_prioritizes_tod_gate_over_promotion_caveat(self):
        status_surface = {
            "arm_online": True,
            "app_alive": True,
            "source_host": "raspberrypi",
            "host_timestamp": datetime.now(timezone.utc).isoformat(),
            "arm_state_probe": {"available": True},
            "serial_ready": True,
            "tod_execution_allowed": False,
            "tod_execution_block_reason": "catchup_gate_false",
            "motion_allowed": False,
            "motion_block_reasons": ["tod_execution_not_allowed"],
            "estop_ok": True,
            "estop_supported": False,
            "tod_readiness": {
                "catchup_detail": {
                    "refresh_evidence_ok": False,
                    "fresh": True,
                    "failed_refresh_checks": ["objective_match", "schema_match"],
                }
            },
            "source_artifacts": {
                "arm_status": __file__,
                "arm_host_state": __file__,
            },
        }

        readiness = mim_arm.build_mim_arm_control_readiness(status_surface)

        self.assertTrue(readiness["access"]["ready"])
        self.assertFalse(readiness["control"]["ready"])
        self.assertEqual(readiness["control"]["blockers"], ["tod_execution_not_allowed"])
        self.assertIn("integration status", str(readiness["recommended_next_step"]).lower())
        self.assertIn(
            "estop_not_supported_for_promotion",
            readiness["management"]["promotion_caveats"],
        )
        self.assertFalse(readiness["control"]["tod_catchup_detail"]["refresh_evidence_ok"])
        self.assertIn(
            "objective_match",
            readiness["control"]["tod_catchup_detail"]["failed_refresh_checks"],
        )

    def test_refresh_management_surface_runs_sync_and_status_generation_and_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            status_surface = {
                "arm_online": True,
                "app_alive": True,
                "source_host": "raspberrypi",
                "host_timestamp": datetime.now(timezone.utc).isoformat(),
                "arm_state_probe": {"available": True},
                "serial_ready": True,
                "tod_execution_allowed": True,
                "tod_execution_block_reason": "",
                "motion_allowed": False,
                "motion_block_reasons": [],
                "estop_ok": True,
                "estop_supported": True,
                "source_artifacts": {
                    "arm_status": str(root / mim_arm.ARM_STATUS_ARTIFACT),
                    "arm_host_state": str(root / mim_arm.ARM_HOST_STATE_ARTIFACT),
                },
            }

            def _fake_run(command, capture_output, text, check):
                if mim_arm.ARM_SYNC_SCRIPT.name in command[1]:
                    (root / mim_arm.ARM_HOST_STATE_ARTIFACT).write_text("{}\n", encoding="utf-8")
                if mim_arm.ARM_STATUS_SCRIPT.name in command[1]:
                    (root / mim_arm.ARM_STATUS_ARTIFACT).write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

            with patch.object(mim_arm, "load_mim_arm_status_surface", return_value=status_surface), patch.object(
                mim_arm.subprocess,
                "run",
                side_effect=_fake_run,
            ) as run_mock:
                response = mim_arm.refresh_mim_arm_management_surface(shared_root=root)

            self.assertEqual(run_mock.call_count, 2)
            self.assertTrue((root / mim_arm.ARM_CONTROL_READINESS_ARTIFACT).exists())
            self.assertTrue((root / mim_arm.ARM_REFRESH_STATUS_ARTIFACT).exists())
            self.assertTrue(response["control_readiness"]["access"]["ready"])
            self.assertTrue(response["control_readiness"]["control"]["ready"])
            self.assertTrue(response["refresh"]["commands"])

    def test_status_surface_exposes_tod_block_reason_when_catchup_gate_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "host_timestamp": datetime.now(timezone.utc).isoformat(),
                        "source_host": "mim-arm-pi",
                        "arm_state_probe": {"available": True},
                        "arm_online": True,
                        "app_alive": True,
                        "camera_online": True,
                        "serial_ready": True,
                        "estop_ok": True,
                        "estop_status": "clear",
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "Readiness appears valid.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                            "authoritative": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": False,
                        "promotion_ready": False,
                        "confidence": "medium",
                        "details": {
                            "alignment_status": "catchup_pending",
                            "refresh_ok": False,
                            "refresh_evidence_ok": False,
                            "fresh": False,
                            "freshness_age_seconds": 104331,
                            "freshness_max_age_seconds": 900,
                            "refresh_checks": {
                                "objective_match": False,
                                "schema_match": False,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)

        self.assertFalse(surface["tod_execution_allowed"])
        self.assertEqual(surface["tod_execution_block_reason"], "catchup_gate_false")
        self.assertFalse(surface["motion_allowed"])
        self.assertIn("tod_execution_not_allowed", surface["motion_block_reasons"])
        catchup_detail = surface["tod_readiness"]["catchup_detail"]
        self.assertFalse(catchup_detail["gate_pass"])
        self.assertFalse(catchup_detail["refresh_evidence_ok"])
        self.assertFalse(catchup_detail["fresh"])
        self.assertIn("objective_match", catchup_detail["failed_refresh_checks"])

    def test_status_surface_blocks_dispatch_when_communication_escalation_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.ARM_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "host_timestamp": datetime.now(timezone.utc).isoformat(),
                        "source_host": "mim-arm-pi",
                        "arm_state_probe": {"available": True},
                        "arm_online": True,
                        "app_alive": True,
                        "camera_online": True,
                        "serial_ready": True,
                        "estop_ok": True,
                        "estop_status": "clear",
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_COMMAND_STATUS_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "execution_readiness": {
                            "status": "valid",
                            "detail": "Readiness appears valid.",
                            "execution_allowed": True,
                            "policy_outcome": "allow",
                            "freshness_state": "fresh",
                            "authoritative": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.TOD_CATCHUP_GATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-29T20:00:00Z",
                        "gate_pass": True,
                        "promotion_ready": True,
                        "confidence": "high",
                        "details": {
                            "alignment_status": "in_sync",
                            "refresh_evidence_ok": True,
                            "fresh": True,
                            "refresh_checks": {},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / mim_arm.MIM_DECISION_TASK_ARTIFACT).write_text(
                json.dumps(
                    {
                        "communication_escalation": {
                            "required": True,
                            "code": "ask_tod_status_loudly",
                            "detail": "TOD remains silent.",
                            "required_cycle_count": 4,
                            "block_dispatch_threshold_cycles": 3,
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                mim_arm._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "healthy"},
            ):
                surface = mim_arm.load_mim_arm_status_surface(shared_root=root)
                readiness = mim_arm.build_mim_arm_control_readiness(surface)

        self.assertFalse(surface["tod_execution_allowed"])
        self.assertEqual(surface["tod_execution_block_reason"], "communication_escalation_persistent")
        self.assertIn("tod_execution_not_allowed", surface["motion_block_reasons"])
        self.assertTrue(surface["tod_readiness"]["communication_dispatch_gate"]["active"])
        self.assertFalse(readiness["control"]["ready"])
        self.assertIn("Persistent TOD communication escalation", readiness["recommended_next_step"])

    def test_publish_mim_arm_execution_to_tod_writes_request_and_trigger_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / mim_arm.CONTEXT_EXPORT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "objective_active": "97",
                        "current_next_objective": "97",
                        "release_tag": "objective-97",
                        "schema_version": "2026-03-24-70",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fake_execution = SimpleNamespace(
                id=207747,
                capability_name="mim_arm.execute_safe_home",
                requested_executor="tod",
                arguments_json={"target_pose": "safe_home", "action": "safe_home"},
            )
            status_surface = {
                "arm_online": True,
                "current_pose": [116, 62, 62, 95, 53, 91],
                "mode": "development",
                "camera_online": True,
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": True,
            }
            sequence_outputs = [
                SimpleNamespace(
                    stdout="SEQUENCE=41\nEMITTED_AT=2026-03-31T03:10:00Z\nSOURCE_HOST=MIM\nSOURCE_SERVICE=mim_arm_safe_home_dispatch\nSOURCE_INSTANCE_ID=mim_arm_safe_home_dispatch:207747\n",
                    returncode=0,
                ),
                SimpleNamespace(
                    stdout="SEQUENCE=42\nEMITTED_AT=2026-03-31T03:10:01Z\nSOURCE_HOST=MIM\nSOURCE_SERVICE=mim_arm_safe_home_dispatch\nSOURCE_INSTANCE_ID=mim_arm_safe_home_dispatch:207747\n",
                    returncode=0,
                ),
            ]

            with patch.object(mim_arm.subprocess, "run", side_effect=sequence_outputs), patch.object(
                mim_arm,
                "_env_flag",
                return_value=False,
            ), patch.object(
                mim_arm,
                "_audit_tod_bridge_write",
            ):
                publication = mim_arm.publish_mim_arm_execution_to_tod(
                    execution=fake_execution,
                    status=status_surface,
                    shared_root=root,
                )

            expected_token = int(datetime(2026, 3, 31, 3, 10, 0, tzinfo=timezone.utc).strftime("%Y%m%d%H%M%S"))
            expected_request_id = str(publication["request_id"])

            request = json.loads((root / "MIM_TOD_TASK_REQUEST.latest.json").read_text(encoding="utf-8"))
            trigger = json.loads((root / "MIM_TO_TOD_TRIGGER.latest.json").read_text(encoding="utf-8"))
            dispatch_telemetry = json.loads((root / "MIM_ARM_DISPATCH_TELEMETRY.latest.json").read_text(encoding="utf-8"))
            per_dispatch = json.loads(
                (root / "mim_arm_dispatch_telemetry" / f"{expected_request_id}.json").read_text(encoding="utf-8")
            )

        self.assertTrue(publication["local_written"])
        self.assertEqual(publication["task_id"], expected_request_id)
        self.assertEqual(publication["request_id"], expected_request_id)
        self.assertEqual(request["task_id"], expected_request_id)
        self.assertEqual(request["request_id"], expected_request_id)
        self.assertEqual(request["action"], "safe_home")
        self.assertEqual(request["execution_id"], 207747)
        self.assertEqual(request["freshness_token"], expected_token)
        self.assertEqual(request["publish_index"], 41)
        self.assertEqual(request["feedback_endpoint"], "/gateway/capabilities/executions/207747/feedback")
        self.assertEqual(trigger["trigger"], "task_request_posted")
        self.assertEqual(trigger["task_id"], expected_request_id)
        self.assertEqual(trigger["request_id"], expected_request_id)
        self.assertEqual(trigger["freshness_token"], expected_token)
        self.assertEqual(trigger["artifact"], "MIM_TOD_TASK_REQUEST.latest.json")
        self.assertEqual(dispatch_telemetry["request_id"], expected_request_id)
        self.assertEqual(dispatch_telemetry["task_id"], expected_request_id)
        self.assertEqual(dispatch_telemetry["dispatch_status"], "published_local")
        self.assertEqual(dispatch_telemetry["completion_status"], "pending")
        self.assertEqual(per_dispatch["request_id"], expected_request_id)

    def test_publish_mim_arm_execution_to_tod_requires_explicit_action_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_execution = SimpleNamespace(
                id=207748,
                capability_name="mim_arm.execute_capture_frame",
                requested_executor="tod",
                arguments_json={},
            )

            with self.assertRaisesRegex(RuntimeError, "explicit action identity"):
                mim_arm.publish_mim_arm_execution_to_tod(
                    execution=fake_execution,
                    status={"arm_online": True},
                    shared_root=root,
                )

    def test_publish_capture_frame_to_tod_sets_explicit_tod_bridge_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_execution = SimpleNamespace(
                id=207749,
                capability_name="mim_arm.execute_capture_frame",
                requested_executor="tod",
                arguments_json={"action": "capture_frame"},
            )
            status_surface = {
                "arm_online": True,
                "camera_online": True,
                "current_pose": [90, 90, 90, 90, 90, 90],
                "mode": "development",
                "serial_ready": True,
                "estop_ok": True,
                "tod_execution_allowed": True,
                "motion_allowed": True,
            }
            sequence_outputs = [
                SimpleNamespace(
                    stdout="SEQUENCE=51\nEMITTED_AT=2026-04-07T00:10:01Z\nSOURCE_HOST=MIM\nSOURCE_SERVICE=mim_arm_capture_frame_dispatch\nSOURCE_INSTANCE_ID=mim_arm_capture_frame_dispatch:207749\n",
                    returncode=0,
                ),
                SimpleNamespace(
                    stdout="SEQUENCE=52\nEMITTED_AT=2026-04-07T00:10:02Z\nSOURCE_HOST=MIM\nSOURCE_SERVICE=mim_arm_capture_frame_dispatch\nSOURCE_INSTANCE_ID=mim_arm_capture_frame_dispatch:207749\n",
                    returncode=0,
                ),
            ]

            with patch.object(mim_arm.subprocess, "run", side_effect=sequence_outputs), patch.object(
                mim_arm,
                "_env_flag",
                return_value=False,
            ), patch.object(
                mim_arm,
                "_audit_tod_bridge_write",
            ):
                publication = mim_arm.publish_mim_arm_execution_to_tod(
                    execution=fake_execution,
                    status=status_surface,
                    shared_root=root,
                )

            request = json.loads((root / "MIM_TOD_TASK_REQUEST.latest.json").read_text(encoding="utf-8"))
            bridge_request = json.loads((root / mim_arm.TOD_BRIDGE_REQUEST_ARTIFACT).read_text(encoding="utf-8"))

        self.assertTrue(publication["local_written"])
        self.assertEqual(request["action"], "capture_frame")
        self.assertEqual(request["RequestId"], publication["request_id"])
        self.assertEqual(request["RequestPath"], str(root / mim_arm.TOD_BRIDGE_REQUEST_ARTIFACT))
        self.assertEqual(request["CorrelationId"], publication["correlation_id"])
        self.assertEqual(request["tod_action"], "run-bridge-request")
        self.assertEqual(request["bridge_request_id"], publication["request_id"])
        self.assertEqual(
            request["tod_action_args"],
            {
                "RequestId": publication["request_id"],
                "RequestPath": str(root / mim_arm.TOD_BRIDGE_REQUEST_ARTIFACT),
                "CorrelationId": publication["correlation_id"],
                "Action": "capture_frame",
                "CapabilityName": "mim_arm.execute_capture_frame",
            },
        )
        self.assertEqual(
            request["tod_bridge_request"],
            {
                "action": "capture_frame",
                "Action": "capture_frame",
                "capability_name": "mim_arm.execute_capture_frame",
                "CapabilityName": "mim_arm.execute_capture_frame",
                "execution_lane": "tod_bridge_request",
                "request_id": publication["request_id"],
                "RequestId": publication["request_id"],
                "request_path": str(root / mim_arm.TOD_BRIDGE_REQUEST_ARTIFACT),
                "RequestPath": str(root / mim_arm.TOD_BRIDGE_REQUEST_ARTIFACT),
                "correlation_id": publication["correlation_id"],
                "CorrelationId": publication["correlation_id"],
            },
        )
        self.assertEqual(
            bridge_request,
            {
                "version": "1.0",
                "source": "MIM",
                "target": "TOD",
                "generated_at": request["generated_at"],
                "emitted_at": request["generated_at"],
                "sequence": request["sequence"],
                "objective_id": request["objective_id"],
                "objective": request["objective"],
                "task_id": publication["request_id"],
                "request_id": publication["request_id"],
                "correlation_id": publication["correlation_id"],
                "CorrelationId": publication["correlation_id"],
                "action": "capture_frame",
                "Action": "capture_frame",
                "capability_name": "mim_arm.execute_capture_frame",
                "CapabilityName": "mim_arm.execute_capture_frame",
                "execution_lane": "tod_bridge_request",
                "command": {"name": "capture_frame", "args": {}},
                "tod_action": "capture_frame",
            },
        )

    def test_composed_step_proof_promotes_to_proved_when_ack_result_and_host_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_id = "objective-111-task-mim-arm-safe-home-1"
            telemetry_dir = root / "mim_arm_dispatch_telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            (telemetry_dir / f"{request_id}.json").write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "task_id": request_id,
                        "correlation_id": "corr-safe-home-1",
                        "dispatch_status": "published_remote",
                        "completion_status": "pending",
                        "evidence_sources": [
                            {"kind": "publication_boundary", "matched": True},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / mim_arm.TOD_TASK_RESULT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "task_id": request_id,
                        "correlation_id": "corr-safe-home-1",
                        "generated_at": "2026-04-07T01:05:03Z",
                        "status": "success",
                        "reason": "safe_home_completed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "task_id": request_id,
                        "correlation_id": "corr-safe-home-1",
                        "generated_at": "2026-04-07T01:05:01Z",
                        "status": "accepted",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / mim_arm.ARM_HOST_STATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "last_request_id": request_id,
                        "last_task_id": request_id,
                        "last_correlation_id": "corr-safe-home-1",
                        "last_command_result": {
                            "request_id": request_id,
                            "task_id": request_id,
                            "correlation_id": "corr-safe-home-1",
                        },
                        "command_evidence": {
                            "request_id": request_id,
                            "task_id": request_id,
                            "correlation_id": "corr-safe-home-1",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            step = mim_arm._new_composed_step(0, "safe_home")
            step["request_id"] = request_id
            step["task_id"] = request_id
            step["correlation_id"] = "corr-safe-home-1"

            proof_requirements, proof_chain_complete, reason = mim_arm._step_proof_from_telemetry(
                step,
                shared_root=root,
            )

        self.assertTrue(proof_chain_complete)
        self.assertEqual(step["status"], "proved")
        self.assertEqual(reason, "safe_home_completed")
        self.assertTrue(proof_requirements["tod_ack_result_aligned"])
        self.assertTrue(proof_requirements["explicit_host_attribution_present"])

    def test_composed_step_proof_accepts_result_with_ack_lineage_when_latest_ack_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            request_id = "objective-111-task-mim-arm-safe-home-2"
            telemetry_dir = root / "mim_arm_dispatch_telemetry"
            telemetry_dir.mkdir(parents=True, exist_ok=True)
            (telemetry_dir / f"{request_id}.json").write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "task_id": request_id,
                        "correlation_id": "corr-safe-home-2",
                        "dispatch_status": "published_remote",
                        "completion_status": "pending",
                        "evidence_sources": [
                            {"kind": "publication_boundary", "matched": True},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / mim_arm.TOD_TASK_RESULT_ARTIFACT).write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "task_id": request_id,
                        "correlation_id": "corr-safe-home-2",
                        "generated_at": "2026-04-07T01:05:03Z",
                        "message_kind": "result",
                        "ack_sequence": 42,
                        "acknowledged_trigger_sequence": 84,
                        "status": "success",
                        "reason": "safe_home_completed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-111-task-mim-arm-safe-home-stale",
                        "task_id": "objective-111-task-mim-arm-safe-home-stale",
                        "correlation_id": "corr-safe-home-stale",
                        "generated_at": "2026-04-07T01:05:01Z",
                        "status": "accepted",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / mim_arm.ARM_HOST_STATE_ARTIFACT).write_text(
                json.dumps(
                    {
                        "last_request_id": request_id,
                        "last_task_id": request_id,
                        "last_correlation_id": "corr-safe-home-2",
                        "last_command_result": {
                            "request_id": request_id,
                            "task_id": request_id,
                            "correlation_id": "corr-safe-home-2",
                        },
                        "command_evidence": {
                            "request_id": request_id,
                            "task_id": request_id,
                            "correlation_id": "corr-safe-home-2",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            step = mim_arm._new_composed_step(0, "safe_home")
            step["request_id"] = request_id
            step["task_id"] = request_id
            step["correlation_id"] = "corr-safe-home-2"

            proof_requirements, proof_chain_complete, reason = mim_arm._step_proof_from_telemetry(
                step,
                shared_root=root,
            )

        self.assertTrue(proof_chain_complete)
        self.assertEqual(step["status"], "proved")
        self.assertEqual(reason, "safe_home_completed")
        self.assertTrue(proof_requirements["tod_ack_result_aligned"])
        self.assertTrue(proof_requirements["explicit_host_attribution_present"])

    def test_reconcile_composed_task_retries_retryable_failed_step_within_budget(self):
        task = {
            "trace_id": "trace-objective-112",
            "status": "active",
            "current_step_index": 0,
            "current_step_key": "step_1_safe_home",
            "max_retry_per_step": 1,
            "steps": [
                {
                    "step_index": 0,
                    "step_key": "step_1_safe_home",
                    "action": "safe_home",
                    "status": "failed",
                    "reason": "transport_dispatch_failed",
                    "retry_count": 0,
                    "proof_chain_complete": False,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            reconciled = mim_arm._reconcile_composed_task(
                task,
                shared_root=Path(tmpdir),
                explicit_operator_approval=True,
                allow_retry=True,
            )

        self.assertEqual(reconciled["status"], "recovery_pending")
        self.assertEqual(reconciled["decision"]["code"], "retry_current_step")
        self.assertEqual(reconciled["steps"][0]["failure_classification"], "retryable_transport")

    def test_reconcile_composed_task_surfaces_blocked_step_for_operator_review(self):
        task = {
            "trace_id": "trace-objective-111-blocked",
            "status": "active",
            "current_step_index": 0,
            "current_step_key": "step_1_safe_home",
            "steps": [
                {
                    "step_index": 0,
                    "step_key": "step_1_safe_home",
                    "action": "safe_home",
                    "status": "blocked",
                    "dispatch_decision": "blocked",
                    "reason": "execution_readiness_blocked",
                    "retry_count": 0,
                    "proof_chain_complete": False,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            reconciled = mim_arm._reconcile_composed_task(
                task,
                shared_root=Path(tmpdir),
                explicit_operator_approval=True,
                allow_retry=True,
            )

        self.assertEqual(reconciled["status"], "awaiting_operator")
        self.assertEqual(reconciled["decision"]["code"], "operator_review_current_step")
        self.assertIn("execution_readiness_blocked", reconciled["decision"]["detail"])

    def test_persist_composed_task_snapshot_prunes_older_task_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_dir = root / mim_arm.MIM_ARM_COMPOSED_TASK_DIRNAME
            task_dir.mkdir(parents=True, exist_ok=True)
            for index in range(25):
                (task_dir / f"trace-old-{index}.json").write_text("{}\n", encoding="utf-8")

            mim_arm._persist_composed_task_snapshot(
                {
                    "trace_id": "trace-current",
                    "status": "active",
                    "current_step_index": 0,
                    "steps": [],
                },
                shared_root=root,
            )

            retained = [item for item in task_dir.glob("*.json") if item.is_file()]

        self.assertLessEqual(len(retained), 20)