from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.execution_lane_service import _arm_base_url, arm_live_check_path
from core.routers import mim_arm


def _post_no_body_json(url: str, *, timeout_seconds: int) -> dict[str, object]:
    request = urllib_request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
            return {
                "status_code": int(response.status),
                "payload": json.loads(raw) if raw else {},
            }
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {
            "status_code": int(exc.code),
            "payload": json.loads(raw) if raw else {},
        }


def _get_json(url: str, *, timeout_seconds: int) -> dict[str, object]:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
            return {
                "status_code": int(response.status),
                "payload": json.loads(raw) if raw else {},
            }
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {
            "status_code": int(exc.code),
            "payload": json.loads(raw) if raw else {},
        }


def _utc_expiry(minutes: int = 5) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _same_pose(left: object, right: object) -> bool:
    return isinstance(left, list) and isinstance(right, list) and left == right


def _expected_axis_value(start_pose: list[object], index: int, delta: float) -> int:
    start_value = int(round(float(start_pose[index])))
    limits = {0: (0, 180), 1: (15, 165), 2: (0, 180)}
    low, high = limits[index]
    return max(low, min(high, int(round(start_value + float(delta)))))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live MIM Arm transport check without mixing it into the synthetic contract gate.")
    parser.add_argument("--shared-root", default="runtime/shared", help="Shared root for artifacts.")
    parser.add_argument("--request-id", default="", help="Optional request id.")
    parser.add_argument(
        "--command",
        default="move_to",
        choices=["move_to", "move_relative", "move_relative_then_set_gripper", "pick_at", "place_at", "pick_and_place", "move_home", "open_gripper", "close_gripper", "set_gripper", "set_speed", "stop"],
        help="Command to validate through the live MIM Arm lane.",
    )
    parser.add_argument(
        "--stop-mode",
        default="active",
        choices=["active", "idle"],
        help="When validating stop, check either the active-motion interrupt path or the idle no-motion acceptance path.",
    )
    parser.add_argument("--position", type=float, default=100.0, help="set_gripper position percentage.")
    parser.add_argument("--level", default="normal", help="set_speed level.")
    parser.add_argument("--x", type=float, default=None, help="pick_at/place_at target x value. Defaults to the current pose x.")
    parser.add_argument("--y", type=float, default=None, help="pick_at/place_at target y value. Defaults to the current pose y.")
    parser.add_argument("--z", type=float, default=None, help="pick_at/place_at target z value. Defaults to the current pose z.")
    parser.add_argument("--pick-x", type=float, default=None, help="pick_and_place source x value. Defaults to the current pose x.")
    parser.add_argument("--pick-y", type=float, default=None, help="pick_and_place source y value. Defaults to the current pose y.")
    parser.add_argument("--pick-z", type=float, default=None, help="pick_and_place source z value. Defaults to the current pose z.")
    parser.add_argument("--place-x", type=float, default=None, help="pick_and_place destination x value. Defaults to the current pose x.")
    parser.add_argument("--place-y", type=float, default=None, help="pick_and_place destination y value. Defaults to the current pose y.")
    parser.add_argument("--place-z", type=float, default=None, help="pick_and_place destination z value. Defaults to the current pose z.")
    parser.add_argument("--dx", type=float, default=0.0, help="move_relative dx delta.")
    parser.add_argument("--dy", type=float, default=0.0, help="move_relative dy delta.")
    parser.add_argument("--dz", type=float, default=0.0, help="move_relative dz delta.")
    parser.add_argument("--expect-result-status", default="succeeded", help="Expected terminal result status.")
    parser.add_argument("--expect-reason", default="", help="Optional expected terminal result reason.")
    args = parser.parse_args()

    shared_root = Path(args.shared_root).expanduser().resolve()
    status = mim_arm.load_mim_arm_status_surface(shared_root=shared_root)
    pose = status.get("current_pose") if isinstance(status.get("current_pose"), list) else [90, 90, 90, 90, 90, 50]
    base_url = _arm_base_url(status)
    command_args = {}
    metadata_json = {"check_type": "live_transport_validation"}
    before_pose = None
    if args.command == "move_to":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {"x": pose[0], "y": pose[1], "z": pose[2]}
        metadata_json["check_type"] = "live_transport_noop_pose"
    elif args.command == "move_relative":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {"dx": float(args.dx), "dy": float(args.dy), "dz": float(args.dz)}
        metadata_json["check_type"] = "live_transport_relative_delta"
    elif args.command == "move_relative_then_set_gripper":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {
            "dx": float(args.dx),
            "dy": float(args.dy),
            "dz": float(args.dz),
            "position": float(args.position),
        }
        metadata_json["check_type"] = "live_transport_relative_then_gripper"
    elif args.command == "pick_at":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {
            "x": float(args.x if args.x is not None else (before_pose[0] if isinstance(before_pose, list) else pose[0])),
            "y": float(args.y if args.y is not None else (before_pose[1] if isinstance(before_pose, list) else pose[1])),
            "z": float(args.z if args.z is not None else (before_pose[2] if isinstance(before_pose, list) else pose[2])),
        }
        metadata_json["check_type"] = "live_transport_pick_macro"
    elif args.command == "place_at":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {
            "x": float(args.x if args.x is not None else (before_pose[0] if isinstance(before_pose, list) else pose[0])),
            "y": float(args.y if args.y is not None else (before_pose[1] if isinstance(before_pose, list) else pose[1])),
            "z": float(args.z if args.z is not None else (before_pose[2] if isinstance(before_pose, list) else pose[2])),
        }
        metadata_json["check_type"] = "live_transport_place_macro"
    elif args.command == "pick_and_place":
        before_state = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
        before_pose = before_state.get("payload", {}).get("current_pose") if isinstance(before_state, dict) else None
        command_args = {
            "pick_x": float(args.pick_x if args.pick_x is not None else (before_pose[0] if isinstance(before_pose, list) else pose[0])),
            "pick_y": float(args.pick_y if args.pick_y is not None else (before_pose[1] if isinstance(before_pose, list) else pose[1])),
            "pick_z": float(args.pick_z if args.pick_z is not None else (before_pose[2] if isinstance(before_pose, list) else pose[2])),
            "place_x": float(args.place_x if args.place_x is not None else (before_pose[0] if isinstance(before_pose, list) else pose[0])),
            "place_y": float(args.place_y if args.place_y is not None else (before_pose[1] if isinstance(before_pose, list) else pose[1])),
            "place_z": float(args.place_z if args.place_z is not None else (before_pose[2] if isinstance(before_pose, list) else pose[2])),
        }
        metadata_json["check_type"] = "live_transport_pick_and_place_macro"
    elif args.command == "set_gripper":
        command_args = {"position": float(args.position)}
    elif args.command == "set_speed":
        command_args = {"level": str(args.level or "").strip().lower()}
    elif args.command == "stop":
        metadata_json["check_type"] = (
            "live_transport_stop_during_motion" if args.stop_mode == "active" else "live_transport_stop_idle"
        )
    request = {
        "request_id": str(args.request_id or f"mim-arm-live-check-{int(datetime.now(timezone.utc).timestamp())}"),
        "target": "mim_arm",
        "sequence": 1,
        "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": _utc_expiry(),
        "command": {"name": args.command, "args": command_args},
        "metadata_json": metadata_json,
    }
    go_safe_result: dict[str, object] | None = None
    idle_probe: dict[str, object] = {}
    if args.command == "stop" and args.stop_mode == "active":

        def _run_go_safe() -> None:
            nonlocal go_safe_result
            go_safe_result = _post_no_body_json(f"{base_url}/go_safe", timeout_seconds=20)

        worker = threading.Thread(target=_run_go_safe, daemon=True)
        worker.start()
        time.sleep(0.35)
    elif args.command == "stop":
        idle_probe["before_state"] = _get_json(f"{base_url}/arm_state", timeout_seconds=10)
    submission = mim_arm.submit_mim_arm_execution_request(
        request=request,
        shared_root=shared_root,
        status=status,
        hardware_transport_enabled=True,
    )
    repeated_submission: dict[str, object] | None = None
    if args.command == "stop" and args.stop_mode == "active":
        worker.join(timeout=20)
    elif args.command == "stop":
        repeated_request = {
            **request,
            "request_id": f"{request['request_id']}-repeat",
            "sequence": 2,
            "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_at": _utc_expiry(),
        }
        repeated_submission = mim_arm.submit_mim_arm_execution_request(
            request=repeated_request,
            shared_root=shared_root,
            status=status,
            hardware_transport_enabled=True,
        )
    result = submission.get("result", {})
    passed = bool(
        result.get("result_status") == str(args.expect_result_status or "succeeded")
        and (not args.expect_reason or result.get("reason") == str(args.expect_reason))
    )
    if args.command == "move_relative":
        after_state = ((result.get("output") or {}).get("after_state") or {})
        translation = ((result.get("output") or {}).get("translation") or {})
        after_pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
        if isinstance(before_pose, list) and isinstance(after_pose, list):
            expected_xyz = [
                _expected_axis_value(before_pose, 0, float(args.dx)),
                _expected_axis_value(before_pose, 1, float(args.dy)),
                _expected_axis_value(before_pose, 2, float(args.dz)),
            ]
            passed = bool(
                passed
                and translation.get("projected_pose", [])[:3] == expected_xyz
                and after_pose[:3] == expected_xyz
                and translation.get("starting_pose", [])[:3] == [
                    int(round(float(before_pose[0]))),
                    int(round(float(before_pose[1]))),
                    int(round(float(before_pose[2]))),
                ]
            )
    elif args.command == "move_relative_then_set_gripper":
        after_state = ((result.get("output") or {}).get("after_state") or {})
        translation = ((result.get("output") or {}).get("translation") or {})
        after_pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
        if isinstance(before_pose, list) and isinstance(after_pose, list):
            expected_xyz = [
                _expected_axis_value(before_pose, 0, float(args.dx)),
                _expected_axis_value(before_pose, 1, float(args.dy)),
                _expected_axis_value(before_pose, 2, float(args.dz)),
            ]
            passed = bool(
                passed
                and translation.get("projected_pose", [])[:3] == expected_xyz
                and after_pose[:3] == expected_xyz
                and translation.get("gripper_step", {}).get("requested_position") == float(args.position)
                and len(((result.get("output") or {}).get("dispatches") or [])) == 4
            )
    elif args.command == "pick_at":
        after_state = ((result.get("output") or {}).get("after_state") or {})
        translation = ((result.get("output") or {}).get("translation") or {})
        after_pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
        phase_history = (result.get("output") or {}).get("phase_history") or []
        completed_subactions = (result.get("output") or {}).get("completed_subactions") or []
        if isinstance(before_pose, list) and isinstance(after_pose, list):
            target_x = int(round(float(command_args["x"])))
            target_y = int(round(float(command_args["y"])))
            target_z = int(round(float(command_args["z"])))
            expected_lift_z = _expected_axis_value([target_x, target_y, target_z, 90, 90, 90], 2, 20.0)
            passed = bool(
                passed
                and translation.get("translation_strategy") == "pick_at_macro"
                and translation.get("projected_pose", [])[:3] == [target_x, target_y, expected_lift_z]
                and after_pose[:3] == [target_x, target_y, expected_lift_z]
                and (result.get("output") or {}).get("phase") == "completed"
                and completed_subactions == ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"]
                and (result.get("output") or {}).get("failed_subaction") is None
                and (result.get("output") or {}).get("interruption_cause") is None
                and ((result.get("output") or {}).get("end_effector_state") or {}).get("gripper_state") == "closed"
                and len(phase_history) == 4
                and all(isinstance(item, dict) and item.get("status") == "completed" for item in phase_history)
                and len(((result.get("output") or {}).get("dispatches") or [])) == 10
            )
    elif args.command == "place_at":
        after_state = ((result.get("output") or {}).get("after_state") or {})
        translation = ((result.get("output") or {}).get("translation") or {})
        after_pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
        phase_history = (result.get("output") or {}).get("phase_history") or []
        completed_subactions = (result.get("output") or {}).get("completed_subactions") or []
        if isinstance(before_pose, list) and isinstance(after_pose, list):
            target_x = int(round(float(command_args["x"])))
            target_y = int(round(float(command_args["y"])))
            target_z = int(round(float(command_args["z"])))
            expected_lift_z = _expected_axis_value([target_x, target_y, target_z, 90, 90, 90], 2, 20.0)
            passed = bool(
                passed
                and translation.get("translation_strategy") == "place_at_macro"
                and translation.get("projected_pose", [])[:3] == [target_x, target_y, expected_lift_z]
                and after_pose[:3] == [target_x, target_y, expected_lift_z]
                and (result.get("output") or {}).get("phase") == "completed"
                and completed_subactions == ["move_above_target", "descend_to_target", "open_gripper", "retract_or_lift"]
                and (result.get("output") or {}).get("failed_subaction") is None
                and (result.get("output") or {}).get("interruption_cause") is None
                and ((result.get("output") or {}).get("end_effector_state") or {}).get("gripper_state") == "open"
                and len(phase_history) == 4
                and all(isinstance(item, dict) and item.get("status") == "completed" for item in phase_history)
                and len(((result.get("output") or {}).get("dispatches") or [])) == 10
            )
    elif args.command == "pick_and_place":
        after_state = ((result.get("output") or {}).get("after_state") or {})
        translation = ((result.get("output") or {}).get("translation") or {})
        after_pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
        phase_history = (result.get("output") or {}).get("phase_history") or []
        completed_subactions = (result.get("output") or {}).get("completed_subactions") or []
        if isinstance(before_pose, list) and isinstance(after_pose, list):
            place_x = int(round(float(command_args["place_x"])))
            place_y = int(round(float(command_args["place_y"])))
            place_z = int(round(float(command_args["place_z"])))
            expected_lift_z = _expected_axis_value([place_x, place_y, place_z, 90, 90, 90], 2, 20.0)
            passed = bool(
                passed
                and translation.get("translation_strategy") == "pick_and_place_macro"
                and translation.get("projected_pose", [])[:3] == [place_x, place_y, expected_lift_z]
                and after_pose[:3] == [place_x, place_y, expected_lift_z]
                and (result.get("output") or {}).get("phase") == "completed"
                and completed_subactions
                == [
                    "move_above_pick_target",
                    "descend_to_pick_target",
                    "close_gripper",
                    "lift_from_pick_target",
                    "move_above_place_target",
                    "descend_to_place_target",
                    "open_gripper",
                    "lift_from_place_target",
                ]
                and (result.get("output") or {}).get("failed_subaction") is None
                and (result.get("output") or {}).get("interruption_cause") is None
                and ((result.get("output") or {}).get("end_effector_state") or {}).get("gripper_state") == "open"
                and len(phase_history) == 8
                and all(isinstance(item, dict) and item.get("status") == "completed" for item in phase_history)
                and len(((result.get("output") or {}).get("dispatches") or [])) == 20
            )
    if args.command == "stop" and args.stop_mode == "active":
        dispatch = ((result.get("output") or {}).get("dispatches") or [{}])[0]
        after_state = ((result.get("output") or {}).get("after_state") or {})
        after_serial = (after_state.get("serial") or {}) if isinstance(after_state, dict) else {}
        go_safe_payload = (go_safe_result or {}).get("payload") if isinstance(go_safe_result, dict) else {}
        passed = bool(
            passed
            and dispatch.get("payload", {}).get("response") == "HOST_STOP_CONFIRMED"
            and dispatch.get("payload", {}).get("ack_source") == "go_safe"
            and dispatch.get("status_code") == 200
            and after_serial.get("last_serial_event") == "stop_motion_honored"
            and isinstance(go_safe_payload, dict)
            and go_safe_payload.get("status") == "stopped"
            and (go_safe_result or {}).get("status_code") == 409
        )
    elif args.command == "stop":
        dispatch = ((result.get("output") or {}).get("dispatches") or [{}])[0]
        after_state = ((result.get("output") or {}).get("after_state") or {})
        after_serial = (after_state.get("serial") or {}) if isinstance(after_state, dict) else {}
        second_result = (repeated_submission or {}).get("result", {})
        second_dispatch = (((second_result.get("output") or {}).get("dispatches") or [{}])[0]) if isinstance(second_result, dict) else {}
        second_after_state = ((second_result.get("output") or {}).get("after_state") or {}) if isinstance(second_result, dict) else {}
        second_after_serial = (second_after_state.get("serial") or {}) if isinstance(second_after_state, dict) else {}
        before_state = idle_probe.get("before_state", {}).get("payload", {}) if isinstance(idle_probe.get("before_state"), dict) else {}
        passed = bool(
            passed
            and repeated_submission is not None
            and second_result.get("result_status") == str(args.expect_result_status or "succeeded")
            and dispatch.get("payload", {}).get("response") == "HOST_STOP_IDLE_NO_MOTION"
            and dispatch.get("payload", {}).get("ack_source") == "idle_state"
            and dispatch.get("payload", {}).get("motion_active") is False
            and dispatch.get("status_code") == 200
            and after_serial.get("last_serial_event") == "stop_idle_no_motion"
            and after_serial.get("last_serial_event") != "stop_motion_honored"
            and second_dispatch.get("payload", {}).get("response") == "HOST_STOP_IDLE_NO_MOTION"
            and second_dispatch.get("payload", {}).get("ack_source") == "idle_state"
            and second_dispatch.get("payload", {}).get("motion_active") is False
            and second_after_serial.get("last_serial_event") == "stop_idle_no_motion"
            and _same_pose(before_state.get("current_pose"), after_state.get("current_pose"))
            and _same_pose(after_state.get("current_pose"), second_after_state.get("current_pose"))
        )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "check_type": "mim_arm_live_transport",
        "passed": passed,
        "request": request,
        "submission": submission,
        "repeated_submission": repeated_submission,
        "motion_probe": {"go_safe": go_safe_result} if go_safe_result is not None else {},
        "idle_probe": idle_probe,
        "before_pose": before_pose,
        "expectations": {
            "result_status": str(args.expect_result_status or "succeeded"),
            "reason": str(args.expect_reason or ""),
        },
    }
    output_path = arm_live_check_path(shared_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())