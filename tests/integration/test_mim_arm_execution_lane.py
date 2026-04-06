from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.execution_lane_service import TARGET_MIM_ARM, read_execution_events
from core.routers import mim_arm


def _future_expiry(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class MimArmExecutionLaneTest(unittest.TestCase):
    def test_execution_target_profile_exposes_same_contract_surface(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "mode": "development",
            "current_pose": [1, 2, 3],
            "last_command_result": {"status": "success"},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            profile = mim_arm.get_mim_arm_execution_target_profile(
                shared_root=Path(tmp_dir),
                status=status,
            )

        self.assertEqual(profile["target"], TARGET_MIM_ARM)
        self.assertEqual(
            profile["allowed_commands"],
            ["close_gripper", "move_home", "move_relative", "move_relative_then_set_gripper", "move_to", "open_gripper", "pick_and_place", "pick_at", "place_at", "set_gripper", "set_speed", "stop"],
        )
        self.assertTrue(profile["live_transport_available"])
        self.assertTrue(profile["command_capabilities"]["move_home"]["available"])
        self.assertTrue(profile["command_capabilities"]["move_relative"]["available"])
        self.assertTrue(profile["command_capabilities"]["move_relative_then_set_gripper"]["available"])
        self.assertTrue(profile["command_capabilities"]["pick_and_place"]["available"])
        self.assertTrue(profile["command_capabilities"]["pick_at"]["available"])
        self.assertTrue(profile["command_capabilities"]["place_at"]["available"])
        self.assertTrue(profile["command_capabilities"]["set_speed"]["available"])
        self.assertTrue(profile["command_capabilities"]["stop"]["available"])
        self.assertEqual(
            profile["command_capabilities"]["set_gripper"]["parameter_schema"]["properties"]["position"]["maximum"],
            100,
        )
        self.assertEqual(
            profile["command_capabilities"]["move_relative"]["parameter_schema"]["required"],
            ["dx", "dy", "dz"],
        )
        self.assertEqual(
            profile["command_capabilities"]["move_relative_then_set_gripper"]["parameter_schema"]["required"],
            ["dx", "dy", "dz", "position"],
        )
        self.assertEqual(
            profile["command_capabilities"]["pick_and_place"]["parameter_schema"]["required"],
            ["pick_x", "pick_y", "pick_z", "place_x", "place_y", "place_z"],
        )
        self.assertEqual(
            profile["command_capabilities"]["pick_at"]["parameter_schema"]["required"],
            ["x", "y", "z"],
        )
        self.assertEqual(
            profile["command_capabilities"]["place_at"]["parameter_schema"]["required"],
            ["x", "y", "z"],
        )
        self.assertEqual(
            profile["command_capabilities"]["place_at"]["result_schema"]["required"],
            [
                "phase",
                "phase_history",
                "completed_subactions",
                "failed_subaction",
                "interruption_cause",
                "final_pose_summary",
                "end_effector_state",
            ],
        )

    def test_execution_request_emits_ack_and_failure_result_when_hardware_transport_is_not_configured(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "mode": "development",
            "current_pose": [116, 62, 62, 95, 53, 91],
            "last_command_result": {"status": "success"},
        }
        request = {
            "request_id": "mim-arm-request-001",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_to", "args": {"x": 0.1, "y": 0.2, "z": 0.3}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_root = Path(tmp_dir)
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=shared_root,
                status=status,
                hardware_transport_enabled=False,
            )
            events = read_execution_events(shared_root)

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["ack"]["ack_status"], "accepted")
        self.assertEqual(submission["result"]["result_status"], "failed")
        self.assertEqual(submission["result"]["reason"], "hardware_target_not_configured")
        self.assertEqual(len(events), 2)

    def test_duplicate_mim_arm_request_is_idempotent(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
        }
        request = {
            "request_id": "mim-arm-request-duplicate",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "open_gripper", "args": {}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_root = Path(tmp_dir)
            first = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=shared_root,
                status=status,
                hardware_transport_enabled=False,
            )
            second = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=shared_root,
                status=status,
                hardware_transport_enabled=False,
            )
            events = read_execution_events(shared_root)

        self.assertEqual(first["disposition"], "executed")
        self.assertEqual(second["disposition"], "duplicate")
        self.assertEqual(len(events), 2)

    def test_live_transport_success_uses_move_endpoint_translation(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-live-success",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "open_gripper", "args": {}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": "MOVE 5 125"},
                "url": "http://192.168.1.90:5000/move",
                "servo": 5,
                "angle": 125,
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok", "last_command_result": {"last_command_sent": "MOVE 5 125"}},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["reason"], "hardware_transport_succeeded")
        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertEqual(
            submission["result"]["output"]["translation"]["steps"][0]["angle"],
            125,
        )

    def test_live_transport_timeout_maps_to_timed_out_result(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-live-timeout",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_to", "args": {"x": 116, "y": 62, "z": 62}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            return_value={
                "ok": False,
                "timed_out": True,
                "reason": "execution_timeout",
                "status_code": 504,
                "payload": {"status": "timeout"},
                "url": "http://192.168.1.90:5000/move",
                "servo": 0,
                "angle": 116,
            },
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok"},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "timed_out")
        self.assertEqual(submission["result"]["reason"], "execution_timeout")

    def test_move_home_uses_safe_route_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-home",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_home", "args": {}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_home",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok"},
                "url": "http://192.168.1.90:5000/go_safe",
                "route": "go_safe",
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok", "current_pose": [90, 90, 90, 90, 90, 50]},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "go_safe")
        self.assertEqual(dispatch_mock.call_count, 1)

    def test_move_relative_projects_from_current_pose_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-relative",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_relative", "args": {"dx": 5, "dy": -10, "dz": 3}},
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [121, 52, 65, 95, 53, 91]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["reason"], "hardware_transport_succeeded")
        self.assertEqual(submission["result"]["output"]["translation"]["translation_strategy"], "relative_servo_projection")
        self.assertEqual(submission["result"]["output"]["translation"]["projected_pose"][:3], [121, 52, 65])
        self.assertEqual(submission["result"]["output"]["translation"]["relative_delta"], {"dx": 5.0, "dy": -10.0, "dz": 3.0})
        self.assertEqual(
            [dispatch["angle"] for dispatch in submission["result"]["output"]["dispatches"]],
            [121, 52, 65],
        )
        self.assertEqual(dispatch_mock.call_count, 3)

    def test_move_relative_reports_clamp_truth_when_delta_exceeds_limits(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [170, 20, 178, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-relative-clamped",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_relative", "args": {"dx": 20, "dy": -20, "dz": 10}},
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [170, 20, 178, 95, 53, 91]},
                {"status": "ok", "current_pose": [180, 15, 180, 95, 53, 91]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        translation = submission["result"]["output"]["translation"]
        self.assertTrue(translation["clamp_applied"])
        self.assertEqual(sorted(translation["clamped_axes"]), ["x", "y", "z"])
        self.assertEqual(translation["requested_pose"][:3], [190, 0, 188])
        self.assertEqual(translation["projected_pose"][:3], [180, 15, 180])
        self.assertEqual(translation["actual_delta"], {"dx": 10.0, "dy": -5.0, "dz": 2.0})

    def test_compound_relative_then_set_gripper_runs_in_order_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-compound",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "move_relative_then_set_gripper", "args": {"dx": 5, "dy": -10, "dz": 3, "position": 40}},
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [121, 52, 65, 95, 53, 80]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        translation = submission["result"]["output"]["translation"]
        self.assertEqual(translation["translation_strategy"], "relative_servo_projection_then_gripper")
        self.assertEqual(translation["projected_pose"][:3], [121, 52, 65])
        self.assertEqual(translation["gripper_step"]["angle"], 80)
        self.assertEqual([item["servo"] for item in submission["result"]["output"]["dispatches"]], [0, 1, 2, 5])
        self.assertEqual(dispatch_mock.call_count, 4)

    def test_pick_at_reports_truthful_phase_history_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-pick-at",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "pick_at", "args": {"x": 110, "y": 55, "z": 45}},
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(output["phase"], "completed")
        self.assertEqual(output["completed_subactions"], ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"])
        self.assertIsNone(output["failed_subaction"])
        self.assertIsNone(output["interruption_cause"])
        self.assertEqual([entry["phase"] for entry in output["phase_history"]], ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"])
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed", "completed", "completed", "completed"])
        self.assertEqual(output["translation"]["translation_strategy"], "pick_at_macro")
        self.assertEqual(output["translation"]["projected_pose"][:3], [110, 55, 65])
        self.assertEqual([item["phase"] for item in output["dispatches"][:4]], ["move_above_target", "move_above_target", "move_above_target", "descend_to_target"])
        self.assertEqual(dispatch_mock.call_count, 10)

    def test_pick_at_reports_partial_completion_when_stop_interrupts_grasp_phase(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-pick-at-interrupted",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "pick_at", "args": {"x": 110, "y": 55, "z": 45}},
        }
        dispatch_sequence = [
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 45},
            {"servo": 5, "angle": 50, "ok": False, "reason": "execution_interrupted_by_stop", "payload": {"response": "HOST_STOP_CONFIRMED", "ack_source": "go_safe"}},
        ]

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            current = dispatch_sequence.pop(0)
            return {
                "ok": bool(current.get("ok", True)),
                "timed_out": False,
                "reason": str(current.get("reason", "transport_dispatch_succeeded")),
                "status_code": 200,
                "payload": current.get("payload", {"status": "ok", "sent": f"MOVE {servo} {angle}"}),
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 91], "serial": {"last_serial_event": "stop_motion_honored"}},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "failed")
        self.assertEqual(submission["result"]["reason"], "execution_interrupted_by_stop")
        self.assertEqual(output["phase"], "close_gripper")
        self.assertEqual(output["completed_subactions"], ["move_above_target", "descend_to_target"])
        self.assertEqual(output["failed_subaction"], "close_gripper")
        self.assertEqual(output["interruption_cause"], "execution_interrupted_by_stop")
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed", "completed", "interrupted"])
        self.assertEqual(output["phase_history"][-1]["failure"]["reason"], "execution_interrupted_by_stop")
        self.assertTrue(output["replay"]["eligible"])
        self.assertFalse(output["replay"]["requested"])
        self.assertEqual(output["replay"]["resume_from_phase"], "close_gripper")
        self.assertEqual(output["replay"]["carried_forward_subactions"], ["move_above_target", "descend_to_target"])
        self.assertEqual(output["replay"]["suggested_metadata_json"]["macro_replay"]["replay_of_request_id"], "mim-arm-request-pick-at-interrupted")

    def test_pick_at_replay_resumes_from_failed_phase_without_repeating_completed_steps(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        initial_request = {
            "request_id": "mim-arm-request-pick-at-replay-source",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "pick_at", "args": {"x": 110, "y": 55, "z": 45}},
        }
        replay_request = {
            "request_id": "mim-arm-request-pick-at-replay",
            "target": TARGET_MIM_ARM,
            "sequence": 2,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "pick_at", "args": {"x": 110, "y": 55, "z": 45}},
            "metadata_json": {
                "macro_replay": {
                    "replay_of_request_id": "mim-arm-request-pick-at-replay-source",
                    "resume_from_phase": "close_gripper",
                }
            },
        }
        interrupted_dispatches = [
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 45},
            {"servo": 5, "angle": 50, "ok": False, "reason": "execution_interrupted_by_stop", "payload": {"response": "HOST_STOP_CONFIRMED", "ack_source": "go_safe"}},
        ]
        resumed_dispatches = [
            {"servo": 5, "angle": 50},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
        ]

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            queue = interrupted_dispatches if interrupted_dispatches else resumed_dispatches
            current = queue.pop(0)
            return {
                "ok": bool(current.get("ok", True)),
                "timed_out": False,
                "reason": str(current.get("reason", "transport_dispatch_succeeded")),
                "status_code": 200,
                "payload": current.get("payload", {"status": "ok", "sent": f"MOVE {servo} {angle}"}),
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 91], "serial": {"last_serial_event": "stop_motion_honored"}},
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 91]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
            ],
        ):
            shared_root = Path(tmp_dir)
            interrupted = mim_arm.submit_mim_arm_execution_request(
                request=initial_request,
                shared_root=shared_root,
                status=status,
                hardware_transport_enabled=True,
            )
            replayed = mim_arm.submit_mim_arm_execution_request(
                request=replay_request,
                shared_root=shared_root,
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertEqual(interrupted["result"]["reason"], "execution_interrupted_by_stop")
        output = replayed["result"]["output"]
        self.assertEqual(replayed["result"]["result_status"], "succeeded")
        self.assertEqual(output["completed_subactions"], ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"])
        self.assertEqual([entry["phase"] for entry in output["phase_history"]], ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"])
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed", "completed", "completed", "completed"])
        self.assertTrue(output["phase_history"][0].get("carried_forward"))
        self.assertTrue(output["phase_history"][1].get("carried_forward"))
        self.assertEqual([item["phase"] for item in output["dispatches"]], ["close_gripper", "lift_from_target", "lift_from_target", "lift_from_target"])
        self.assertTrue(output["replay"]["requested"])
        self.assertTrue(output["replay"]["eligible"])
        self.assertEqual(output["replay"]["replay_source_request_id"], "mim-arm-request-pick-at-replay-source")
        self.assertEqual(output["replay"]["resume_from_phase"], "close_gripper")

    def test_place_at_reports_truthful_phase_history_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 50],
        }
        request = {
            "request_id": "mim-arm-request-place-at",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "place_at", "args": {"x": 110, "y": 55, "z": 45}},
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 125]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(output["phase"], "completed")
        self.assertEqual(output["completed_subactions"], ["move_above_target", "descend_to_target", "open_gripper", "retract_or_lift"])
        self.assertIsNone(output["failed_subaction"])
        self.assertIsNone(output["interruption_cause"])
        self.assertEqual([entry["phase"] for entry in output["phase_history"]], ["move_above_target", "descend_to_target", "open_gripper", "retract_or_lift"])
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed", "completed", "completed", "completed"])
        self.assertEqual(output["end_effector_state"]["gripper_state"], "open")
        self.assertIsInstance(output["final_pose_summary"]["starting_pose"], list)
        self.assertIsInstance(output["final_pose_summary"]["projected_pose"], list)
        self.assertEqual(output["translation"]["translation_strategy"], "place_at_macro")
        self.assertEqual(output["translation"]["projected_pose"][:3], [110, 55, 65])
        self.assertEqual([item["phase"] for item in output["dispatches"][:4]], ["move_above_target", "move_above_target", "move_above_target", "descend_to_target"])
        self.assertEqual(dispatch_mock.call_count, 10)

    def test_place_at_reports_partial_completion_when_stop_interrupts_release_phase(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 50],
        }
        request = {
            "request_id": "mim-arm-request-place-at-interrupted",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "place_at", "args": {"x": 110, "y": 55, "z": 45}},
        }
        dispatch_sequence = [
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 45},
            {"servo": 5, "angle": 125, "ok": False, "reason": "execution_interrupted_by_stop", "payload": {"response": "HOST_STOP_CONFIRMED", "ack_source": "go_safe"}},
        ]

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            current = dispatch_sequence.pop(0)
            return {
                "ok": bool(current.get("ok", True)),
                "timed_out": False,
                "reason": str(current.get("reason", "transport_dispatch_succeeded")),
                "status_code": 200,
                "payload": current.get("payload", {"status": "ok", "sent": f"MOVE {servo} {angle}"}),
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50], "serial": {"last_serial_event": "stop_motion_honored"}},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "failed")
        self.assertEqual(submission["result"]["reason"], "execution_interrupted_by_stop")
        self.assertEqual(output["phase"], "open_gripper")
        self.assertEqual(output["completed_subactions"], ["move_above_target", "descend_to_target"])
        self.assertEqual(output["failed_subaction"], "open_gripper")
        self.assertEqual(output["interruption_cause"], "execution_interrupted_by_stop")
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed", "completed", "interrupted"])
        self.assertEqual(output["phase_history"][-1]["failure"]["reason"], "execution_interrupted_by_stop")

    def test_pick_and_place_reports_truthful_phase_history_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 125],
        }
        request = {
            "request_id": "mim-arm-request-pick-and-place",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {
                "name": "pick_and_place",
                "args": {"pick_x": 110, "pick_y": 55, "pick_z": 45, "place_x": 130, "place_y": 60, "place_z": 50},
            },
        }

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            return {
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": f"MOVE {servo} {angle}"},
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 70, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 50, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 50, 95, 53, 125]},
                {"status": "ok", "current_pose": [130, 60, 70, 95, 53, 125]},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(output["phase"], "completed")
        self.assertEqual(
            output["completed_subactions"],
            [
                "move_above_pick_target",
                "descend_to_pick_target",
                "close_gripper",
                "lift_from_pick_target",
                "move_above_place_target",
                "descend_to_place_target",
                "open_gripper",
                "lift_from_place_target",
            ],
        )
        self.assertIsNone(output["failed_subaction"])
        self.assertIsNone(output["interruption_cause"])
        self.assertEqual(
            [entry["phase"] for entry in output["phase_history"]],
            [
                "move_above_pick_target",
                "descend_to_pick_target",
                "close_gripper",
                "lift_from_pick_target",
                "move_above_place_target",
                "descend_to_place_target",
                "open_gripper",
                "lift_from_place_target",
            ],
        )
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed"] * 8)
        self.assertEqual(output["end_effector_state"]["gripper_state"], "open")
        self.assertIsInstance(output["final_pose_summary"]["starting_pose"], list)
        self.assertIsInstance(output["final_pose_summary"]["projected_pose"], list)
        self.assertEqual(output["translation"]["translation_strategy"], "pick_and_place_macro")
        self.assertEqual(output["translation"]["projected_pose"][:3], [130, 60, 70])
        self.assertEqual(dispatch_mock.call_count, 20)

    def test_pick_and_place_reports_partial_completion_when_stop_interrupts_release_phase(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 125],
        }
        request = {
            "request_id": "mim-arm-request-pick-and-place-interrupted",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {
                "name": "pick_and_place",
                "args": {"pick_x": 110, "pick_y": 55, "pick_z": 45, "place_x": 130, "place_y": 60, "place_z": 50},
            },
        }
        dispatch_sequence = [
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 45},
            {"servo": 5, "angle": 50},
            {"servo": 0, "angle": 110},
            {"servo": 1, "angle": 55},
            {"servo": 2, "angle": 65},
            {"servo": 0, "angle": 130},
            {"servo": 1, "angle": 60},
            {"servo": 2, "angle": 70},
            {"servo": 0, "angle": 130},
            {"servo": 1, "angle": 60},
            {"servo": 2, "angle": 50},
            {"servo": 5, "angle": 125, "ok": False, "reason": "execution_interrupted_by_stop", "payload": {"response": "HOST_STOP_CONFIRMED", "ack_source": "go_safe"}},
        ]

        def _dispatch_side_effect(
            base_url: str,
            *,
            servo: int,
            angle: int,
            timeout_seconds: int,
            request_context: dict | None = None,
        ) -> dict[str, object]:
            current = dispatch_sequence.pop(0)
            return {
                "ok": bool(current.get("ok", True)),
                "timed_out": False,
                "reason": str(current.get("reason", "transport_dispatch_succeeded")),
                "status_code": 200,
                "payload": current.get("payload", {"status": "ok", "sent": f"MOVE {servo} {angle}"}),
                "url": f"{base_url}/move",
                "servo": servo,
                "angle": angle,
            }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            side_effect=_dispatch_side_effect,
        ), patch(
            "core.execution_lane_service._fetch_arm_state",
            side_effect=[
                {"status": "ok", "current_pose": [116, 62, 62, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 125]},
                {"status": "ok", "current_pose": [110, 55, 45, 95, 53, 50]},
                {"status": "ok", "current_pose": [110, 55, 65, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 70, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 50, 95, 53, 50]},
                {"status": "ok", "current_pose": [130, 60, 50, 95, 53, 50], "serial": {"last_serial_event": "stop_motion_honored"}},
            ],
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        output = submission["result"]["output"]
        self.assertEqual(submission["result"]["result_status"], "failed")
        self.assertEqual(submission["result"]["reason"], "execution_interrupted_by_stop")
        self.assertEqual(output["phase"], "open_gripper")
        self.assertEqual(
            output["completed_subactions"],
            [
                "move_above_pick_target",
                "descend_to_pick_target",
                "close_gripper",
                "lift_from_pick_target",
                "move_above_place_target",
                "descend_to_place_target",
            ],
        )
        self.assertEqual(output["failed_subaction"], "open_gripper")
        self.assertEqual(output["interruption_cause"], "execution_interrupted_by_stop")
        self.assertEqual([entry["status"] for entry in output["phase_history"]], ["completed"] * 6 + ["interrupted"])
        self.assertEqual(output["phase_history"][-1]["failure"]["reason"], "execution_interrupted_by_stop")

    def test_set_gripper_maps_position_percentage_to_servo_angle(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 95, 53, 91],
        }
        request = {
            "request_id": "mim-arm-request-set-gripper",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "set_gripper", "args": {"position": 40}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_step",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "sent": "MOVE 5 80"},
                "url": "http://192.168.1.90:5000/move",
                "servo": 5,
                "angle": 80,
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok", "last_command_result": {"last_command_sent": "MOVE 5 80"}},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertEqual(submission["result"]["output"]["translation"]["steps"][0]["angle"], 80)

    def test_set_speed_uses_host_speed_route_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
        }
        request = {
            "request_id": "mim-arm-request-speed",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "set_speed", "args": {"level": "fast"}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_speed",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "speed": 75},
                "url": "http://192.168.1.90:5000/set_speed",
                "route": "set_speed",
                "speed_ms": 75,
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok", "serial": {"status": "ok"}},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "set_speed")
        self.assertEqual(submission["result"]["output"]["translation"]["requested_speed_ms"], 75)
        self.assertEqual(dispatch_mock.call_count, 1)

    def test_stop_uses_host_stop_route_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
        }
        request = {
            "request_id": "mim-arm-request-stop",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "stop", "args": {}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_stop",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {"status": "ok", "response": "HOST_STOP_CONFIRMED", "ack_source": "go_safe"},
                "url": "http://192.168.1.90:5000/stop",
                "route": "stop",
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={"status": "ok", "serial": {"last_serial_event": "stop_motion_honored"}},
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["reason"], "hardware_transport_succeeded")
        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "stop")
        self.assertEqual(submission["result"]["output"]["dispatches"][0]["payload"]["response"], "HOST_STOP_CONFIRMED")
        self.assertEqual(
            submission["result"]["output"]["after_state"]["serial"]["last_serial_event"],
            "stop_motion_honored",
        )
        self.assertEqual(dispatch_mock.call_count, 1)

    def test_idle_stop_preserves_no_motion_truth_when_live_transport_is_enabled(self) -> None:
        status = {
            "arm_online": True,
            "serial_ready": True,
            "estop_ok": True,
            "arm_state_probe": {"url": "http://192.168.1.90:5000/arm_state"},
            "current_pose": [116, 62, 62, 90, 90, 90],
        }
        request = {
            "request_id": "mim-arm-request-stop-idle",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "stop", "args": {}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "core.execution_lane_service._dispatch_arm_stop",
            return_value={
                "ok": True,
                "timed_out": False,
                "reason": "transport_dispatch_succeeded",
                "status_code": 200,
                "payload": {
                    "status": "ok",
                    "response": "HOST_STOP_IDLE_NO_MOTION",
                    "ack_source": "idle_state",
                    "motion_active": False,
                },
                "url": "http://192.168.1.90:5000/stop",
                "route": "stop",
            },
        ) as dispatch_mock, patch(
            "core.execution_lane_service._fetch_arm_state",
            return_value={
                "status": "ok",
                "current_pose": [116, 62, 62, 90, 90, 90],
                "serial": {"last_serial_event": "stop_idle_no_motion"},
                "last_error": None,
            },
        ):
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertTrue(submission["accepted"])
        self.assertEqual(submission["result"]["result_status"], "succeeded")
        self.assertEqual(submission["result"]["reason"], "hardware_transport_succeeded")
        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "stop")
        self.assertEqual(
            submission["result"]["output"]["dispatches"][0]["payload"]["response"],
            "HOST_STOP_IDLE_NO_MOTION",
        )
        self.assertIs(submission["result"]["output"]["dispatches"][0]["payload"]["motion_active"], False)
        self.assertEqual(
            submission["result"]["output"]["after_state"]["serial"]["last_serial_event"],
            "stop_idle_no_motion",
        )
        self.assertIsNone(submission["result"]["output"]["after_state"]["last_error"])
        self.assertEqual(dispatch_mock.call_count, 1)

    def test_invalid_set_gripper_args_are_rejected(self) -> None:
        status = {"arm_online": True, "serial_ready": True, "estop_ok": True}
        request = {
            "request_id": "mim-arm-request-invalid-gripper",
            "target": TARGET_MIM_ARM,
            "sequence": 1,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _future_expiry(),
            "command": {"name": "set_gripper", "args": {"position": 120}},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            submission = mim_arm.submit_mim_arm_execution_request(
                request=request,
                shared_root=Path(tmp_dir),
                status=status,
                hardware_transport_enabled=True,
            )

        self.assertEqual(submission["disposition"], "rejected")
        self.assertEqual(submission["ack"]["reason"], "invalid_command_args:position_out_of_range")