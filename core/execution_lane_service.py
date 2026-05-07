from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


EXECUTION_LANE_DIRNAME = "execution_lane"
REQUEST_LOG_ARTIFACT = "TOD_MIM_EXECUTION_REQUESTS.jsonl"
EVENT_LOG_ARTIFACT = "TOD_MIM_EXECUTION_EVENTS.jsonl"
STATE_ARTIFACT = "TOD_MIM_EXECUTION_STATE.latest.json"
ARM_REQUEST_ARTIFACT = "MIM_ARM_EXECUTION_REQUEST.latest.json"
ARM_LIVE_CHECK_ARTIFACT = "MIM_ARM_LIVE_TRANSPORT_CHECK.latest.json"
TOD_REQUEST_ARTIFACT = "TOD_MIM_ARM_EXECUTION_REQUEST.latest.json"

TARGET_SYNTHETIC_ARM = "synthetic_arm"
TARGET_MIM_ARM = "mim_arm"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIM_ARM_ENV_FILE = PROJECT_ROOT / "env" / ".env"

MIM_ARM_SERVO_LIMITS = {
    0: (0, 180),
    1: (15, 165),
    2: (0, 180),
    3: (0, 180),
    4: (0, 180),
    5: (0, 180),
}
MIM_ARM_CLAW_OPEN_ANGLE = 125
MIM_ARM_CLAW_CLOSE_ANGLE = 50
MIM_ARM_DEFAULT_POSE = [90, 90, 90, 90, 90, 50]
MIM_ARM_HOME_POSE = [90, 90, 90, 90, 90, 50]
MIM_ARM_SPEED_LEVELS = {"slow": 250, "normal": 150, "fast": 75}
MIM_ARM_PICK_AT_APPROACH_DELTA = 20
MIM_ARM_PLACE_AT_APPROACH_DELTA = 20
MIM_ARM_PICK_AND_PLACE_APPROACH_DELTA = 20

MACRO_PHASE_RESULT_SCHEMA = {
    "type": "object",
    "required": [
        "phase",
        "phase_history",
        "completed_subactions",
        "failed_subaction",
        "interruption_cause",
        "final_pose_summary",
        "end_effector_state",
    ],
    "properties": {
        "phase": {"type": "string"},
        "phase_history": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["phase", "status"],
                "properties": {
                    "phase": {"type": "string"},
                    "status": {"type": "string"},
                    "command": {"type": "object"},
                    "translation": {"type": "object"},
                    "dispatches": {"type": "array"},
                    "after_state": {"type": "object"},
                    "failure": {"type": "object"},
                },
            },
        },
        "completed_subactions": {"type": "array", "items": {"type": "string"}},
        "failed_subaction": {"type": ["string", "null"]},
        "interruption_cause": {"type": ["string", "null"]},
        "final_pose_summary": {
            "type": "object",
            "required": ["starting_pose", "projected_pose", "after_pose"],
            "properties": {
                "starting_pose": {"type": "array", "items": {"type": "number"}},
                "projected_pose": {"type": "array", "items": {"type": "number"}},
                "after_pose": {"type": ["array", "null"], "items": {"type": "number"}},
            },
        },
        "end_effector_state": {
            "type": "object",
            "required": ["gripper_state"],
            "properties": {
                "gripper_state": {"type": "string"},
                "gripper_angle": {"type": ["number", "null"]},
            },
        },
        "replay": {
            "type": "object",
            "required": [
                "eligible",
                "requested",
                "replay_source_request_id",
                "resume_from_phase",
                "carried_forward_subactions",
                "replayable_phases_remaining",
                "replay_reason",
            ],
            "properties": {
                "eligible": {"type": "boolean"},
                "requested": {"type": "boolean"},
                "replay_source_request_id": {"type": ["string", "null"]},
                "resume_from_phase": {"type": ["string", "null"]},
                "carried_forward_subactions": {"type": "array", "items": {"type": "string"}},
                "replayable_phases_remaining": {"type": "array", "items": {"type": "string"}},
                "replay_reason": {"type": "string"},
                "suggested_metadata_json": {"type": "object"},
            },
        },
    },
}

COMMAND_SPECS = {
    "move_to": {
        "required_args": {"x", "y", "z"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
        "safety_constraints": {
            "motion_class": "cartesian_projection",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180]},
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "move_relative": {
        "required_args": {"dx", "dy", "dz"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "dx": {"type": "number"},
                "dy": {"type": "number"},
                "dz": {"type": "number"},
            },
            "required": ["dx", "dy", "dz"],
        },
        "safety_constraints": {
            "motion_class": "relative_cartesian_projection",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180]},
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "move_relative_then_set_gripper": {
        "required_args": {"dx", "dy", "dz", "position"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "dx": {"type": "number"},
                "dy": {"type": "number"},
                "dz": {"type": "number"},
                "position": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "required": ["dx", "dy", "dz", "position"],
        },
        "safety_constraints": {
            "motion_class": "relative_cartesian_projection_with_gripper",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180], "5": [0, 180]},
            "gripper_position_percent": [0, 100],
            "gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE],
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "pick_at": {
        "required_args": {"x", "y", "z"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
        "safety_constraints": {
            "motion_class": "bounded_pick_macro",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180], "5": [0, 180]},
            "gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE],
            "approach_height_delta": MIM_ARM_PICK_AT_APPROACH_DELTA,
            "phases": ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"],
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
        "result_schema": MACRO_PHASE_RESULT_SCHEMA,
    },
    "place_at": {
        "required_args": {"x", "y", "z"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["x", "y", "z"],
        },
        "safety_constraints": {
            "motion_class": "bounded_place_macro",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180], "5": [0, 180]},
            "gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE],
            "approach_height_delta": MIM_ARM_PLACE_AT_APPROACH_DELTA,
            "phases": ["move_above_target", "descend_to_target", "open_gripper", "retract_or_lift"],
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
        "result_schema": MACRO_PHASE_RESULT_SCHEMA,
    },
    "pick_and_place": {
        "required_args": {"pick_x", "pick_y", "pick_z", "place_x", "place_y", "place_z"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "pick_x": {"type": "number"},
                "pick_y": {"type": "number"},
                "pick_z": {"type": "number"},
                "place_x": {"type": "number"},
                "place_y": {"type": "number"},
                "place_z": {"type": "number"},
            },
            "required": ["pick_x", "pick_y", "pick_z", "place_x", "place_y", "place_z"],
        },
        "safety_constraints": {
            "motion_class": "bounded_pick_and_place_macro",
            "servo_limits": {"0": [0, 180], "1": [15, 165], "2": [0, 180], "5": [0, 180]},
            "gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE],
            "approach_height_delta": MIM_ARM_PICK_AND_PLACE_APPROACH_DELTA,
            "phases": [
                "move_above_pick_target",
                "descend_to_pick_target",
                "close_gripper",
                "lift_from_pick_target",
                "move_above_place_target",
                "descend_to_place_target",
                "open_gripper",
                "lift_from_place_target",
            ],
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
        "result_schema": MACRO_PHASE_RESULT_SCHEMA,
    },
    "move_home": {
        "required_args": set(),
        "parameter_schema": {"type": "object", "properties": {}, "required": []},
        "safety_constraints": {
            "motion_class": "safe_home",
            "target_pose": list(MIM_ARM_HOME_POSE),
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "open_gripper": {
        "required_args": set(),
        "parameter_schema": {"type": "object", "properties": {}, "required": []},
        "safety_constraints": {"gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE]},
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "close_gripper": {
        "required_args": set(),
        "parameter_schema": {"type": "object", "properties": {}, "required": []},
        "safety_constraints": {"gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE]},
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "set_gripper": {
        "required_args": {"position"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "position": {"type": "number", "minimum": 0, "maximum": 100},
            },
            "required": ["position"],
        },
        "safety_constraints": {
            "gripper_position_percent": [0, 100],
            "gripper_angle_range": [MIM_ARM_CLAW_CLOSE_ANGLE, MIM_ARM_CLAW_OPEN_ANGLE],
        },
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "set_speed": {
        "required_args": {"level"},
        "parameter_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": sorted(MIM_ARM_SPEED_LEVELS.keys())},
            },
            "required": ["level"],
        },
        "safety_constraints": {"speed_levels": sorted(MIM_ARM_SPEED_LEVELS.keys())},
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
    "stop": {
        "required_args": set(),
        "parameter_schema": {"type": "object", "properties": {}, "required": []},
        "safety_constraints": {"cancellation_scope": "transport_backed_stop"},
        "transport_support": {TARGET_SYNTHETIC_ARM: "supported", TARGET_MIM_ARM: "supported"},
    },
}

COMMAND_BEHAVIOR = {
    "idempotency": "duplicate request_id returns the recorded ACK/RESULT without emitting new events",
    "timeout_behavior": "accepted commands may finish with result_status=timed_out and reason=execution_timeout",
    "cancellation_behavior": "in-flight cancellation is not generic; stop is a separate primitive and transport-backed on supported targets",
    "macro_replay_behavior": "interrupted macro commands may be replayed by submitting a new request with metadata_json.macro_replay.replay_of_request_id and metadata_json.macro_replay.resume_from_phase",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_dict(raw: object) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _json_list(raw: object) -> list[Any]:
    return raw if isinstance(raw, list) else []


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        text = str(raw_line).strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def execution_lane_root(shared_root: Path) -> Path:
    return shared_root / EXECUTION_LANE_DIRNAME


def _request_log_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / REQUEST_LOG_ARTIFACT


def _event_log_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / EVENT_LOG_ARTIFACT


def _state_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / STATE_ARTIFACT


def _arm_request_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / ARM_REQUEST_ARTIFACT


def arm_live_check_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / ARM_LIVE_CHECK_ARTIFACT


def tod_request_path(shared_root: Path) -> Path:
    return execution_lane_root(shared_root) / TOD_REQUEST_ARTIFACT


def _load_env_defaults() -> None:
    if not MIM_ARM_ENV_FILE.exists():
        return
    for raw_line in MIM_ARM_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _env_text(name: str, default: str = "") -> str:
    _load_env_defaults()
    return str(os.getenv(name, default) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_text(name, str(default)) or str(default))
    except Exception:
        return default


def _clamp_servo(servo_id: int, angle: int) -> int:
    low, high = MIM_ARM_SERVO_LIMITS.get(servo_id, (0, 180))
    return max(low, min(high, int(angle)))


def _gripper_angle_for_position(position: float) -> int:
    bounded = max(0.0, min(100.0, float(position)))
    delta = MIM_ARM_CLAW_OPEN_ANGLE - MIM_ARM_CLAW_CLOSE_ANGLE
    return _clamp_servo(5, int(round(MIM_ARM_CLAW_CLOSE_ANGLE + (delta * (bounded / 100.0)))))


def _project_relative_translation(
    pose: list[int],
    *,
    dx: float,
    dy: float,
    dz: float,
    include_gripper_position: float | None = None,
) -> dict[str, Any]:
    projected = list(pose)
    requested_pose = list(pose)
    relative_delta = {"dx": float(dx), "dy": float(dy), "dz": float(dz)}
    axis_defs = [(0, "x", float(dx)), (1, "y", float(dy)), (2, "z", float(dz))]
    steps: list[dict[str, Any]] = []
    clamp_events: list[dict[str, Any]] = []
    clamped_axes: list[str] = []
    actual_delta: dict[str, float] = {}

    for servo, axis, requested_delta in axis_defs:
        requested_angle = int(round(float(projected[servo]) + requested_delta))
        clamped_angle = _clamp_servo(servo, requested_angle)
        requested_pose[servo] = requested_angle
        projected[servo] = clamped_angle
        actual_axis_delta = float(clamped_angle - int(pose[servo]))
        actual_delta[f"d{axis}"] = actual_axis_delta
        step = {
            "servo": servo,
            "angle": clamped_angle,
            "axis": axis,
            "delta": requested_delta,
            "requested_angle": requested_angle,
            "actual_delta": actual_axis_delta,
            "clamped": clamped_angle != requested_angle,
        }
        if step["clamped"]:
            clamped_axes.append(axis)
            clamp_events.append(
                {
                    "axis": axis,
                    "servo": servo,
                    "requested_angle": requested_angle,
                    "applied_angle": clamped_angle,
                }
            )
        steps.append(step)

    translation: dict[str, Any] = {
        "translation_strategy": "relative_servo_projection",
        "steps": steps,
        "projected_pose": projected,
        "requested_pose": requested_pose,
        "relative_delta": relative_delta,
        "actual_delta": actual_delta,
        "starting_pose": list(pose),
        "clamp_applied": bool(clamp_events),
        "clamped_axes": clamped_axes,
        "clamp_events": clamp_events,
    }
    if include_gripper_position is not None:
        target_angle = _gripper_angle_for_position(float(include_gripper_position))
        projected[5] = _clamp_servo(5, target_angle)
        translation["translation_strategy"] = "relative_servo_projection_then_gripper"
        translation["gripper_step"] = {
            "servo": 5,
            "angle": projected[5],
            "axis": "claw",
            "requested_position": float(include_gripper_position),
        }
        translation["requested_position"] = float(include_gripper_position)
        translation["projected_pose"] = projected
    return translation


def _project_direct_translation(
    pose: list[int],
    *,
    x: float,
    y: float,
    z: float,
) -> dict[str, Any]:
    projected = list(pose)
    requested_pose = list(pose)
    axis_defs = [(0, "x", float(x)), (1, "y", float(y)), (2, "z", float(z))]
    steps: list[dict[str, Any]] = []
    clamp_events: list[dict[str, Any]] = []
    clamped_axes: list[str] = []
    actual_delta: dict[str, float] = {}

    for servo, axis, requested_value in axis_defs:
        requested_angle = int(round(requested_value))
        clamped_angle = _clamp_servo(servo, requested_angle)
        requested_pose[servo] = requested_angle
        projected[servo] = clamped_angle
        actual_axis_delta = float(clamped_angle - int(pose[servo]))
        actual_delta[f"d{axis}"] = actual_axis_delta
        step = {
            "servo": servo,
            "angle": clamped_angle,
            "axis": axis,
            "requested_angle": requested_angle,
            "actual_delta": actual_axis_delta,
            "clamped": clamped_angle != requested_angle,
        }
        if step["clamped"]:
            clamped_axes.append(axis)
            clamp_events.append(
                {
                    "axis": axis,
                    "servo": servo,
                    "requested_angle": requested_angle,
                    "applied_angle": clamped_angle,
                }
            )
        steps.append(step)

    return {
        "translation_strategy": "direct_servo_projection",
        "steps": steps,
        "projected_pose": projected,
        "requested_pose": requested_pose,
        "starting_pose": list(pose),
        "actual_delta": actual_delta,
        "clamp_applied": bool(clamp_events),
        "clamped_axes": clamped_axes,
        "clamp_events": clamp_events,
    }


def _build_phased_macro_translation(
    *,
    status_surface: dict[str, Any],
    translation_strategy: str,
    starting_pose: list[int],
    phase_specs: list[tuple[str, str, dict[str, Any]]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    working_pose = list(starting_pose)
    phases: list[dict[str, Any]] = []
    clamped_phases: list[str] = []

    for phase_name, command_name, phase_args in phase_specs:
        translation = _translate_mim_arm_steps(
            {"command": {"name": command_name, "args": phase_args}},
            {**status_surface, "current_pose": working_pose},
        )
        phases.append(
            {
                "phase": phase_name,
                "command": {"name": command_name, "args": phase_args},
                "translation": translation,
            }
        )
        if bool(_json_dict(translation).get("clamp_applied")):
            clamped_phases.append(phase_name)
        projected_pose = _json_dict(translation).get("projected_pose")
        if isinstance(projected_pose, list) and len(projected_pose) >= 6:
            working_pose = [int(round(float(item))) for item in projected_pose[:6]]

    return {
        "translation_strategy": translation_strategy,
        **metadata,
        "phases": phases,
        "projected_pose": list(working_pose),
        "clamp_applied": bool(clamped_phases),
        "clamped_phases": clamped_phases,
    }


def _build_pick_at_translation(status_surface: dict[str, Any], command_args: dict[str, Any]) -> dict[str, Any]:
    starting_pose = _current_pose(status_surface)
    requested_target = {
        "x": float(command_args.get("x", starting_pose[0])),
        "y": float(command_args.get("y", starting_pose[1])),
        "z": float(command_args.get("z", starting_pose[2])),
    }
    approach_height = int(MIM_ARM_PICK_AT_APPROACH_DELTA)
    phase_specs = [
        (
            "move_above_target",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"] + approach_height,
            },
        ),
        (
            "descend_to_target",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"],
            },
        ),
        ("close_gripper", "close_gripper", {}),
        (
            "lift_from_target",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"] + approach_height,
            },
        ),
    ]
    return _build_phased_macro_translation(
        status_surface=status_surface,
        translation_strategy="pick_at_macro",
        starting_pose=starting_pose,
        phase_specs=phase_specs,
        metadata={
            "starting_pose": starting_pose,
            "requested_target": requested_target,
            "approach_height": approach_height,
        },
    )


def _build_place_at_translation(status_surface: dict[str, Any], command_args: dict[str, Any]) -> dict[str, Any]:
    starting_pose = _current_pose(status_surface)
    requested_target = {
        "x": float(command_args.get("x", starting_pose[0])),
        "y": float(command_args.get("y", starting_pose[1])),
        "z": float(command_args.get("z", starting_pose[2])),
    }
    approach_height = int(MIM_ARM_PLACE_AT_APPROACH_DELTA)
    phase_specs = [
        (
            "move_above_target",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"] + approach_height,
            },
        ),
        (
            "descend_to_target",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"],
            },
        ),
        ("open_gripper", "open_gripper", {}),
        (
            "retract_or_lift",
            "move_to",
            {
                "x": requested_target["x"],
                "y": requested_target["y"],
                "z": requested_target["z"] + approach_height,
            },
        ),
    ]
    return _build_phased_macro_translation(
        status_surface=status_surface,
        translation_strategy="place_at_macro",
        starting_pose=starting_pose,
        phase_specs=phase_specs,
        metadata={
            "starting_pose": starting_pose,
            "requested_target": requested_target,
            "approach_height": approach_height,
        },
    )


def _build_pick_and_place_translation(status_surface: dict[str, Any], command_args: dict[str, Any]) -> dict[str, Any]:
    starting_pose = _current_pose(status_surface)
    requested_pick_target = {
        "x": float(command_args.get("pick_x", starting_pose[0])),
        "y": float(command_args.get("pick_y", starting_pose[1])),
        "z": float(command_args.get("pick_z", starting_pose[2])),
    }
    requested_place_target = {
        "x": float(command_args.get("place_x", starting_pose[0])),
        "y": float(command_args.get("place_y", starting_pose[1])),
        "z": float(command_args.get("place_z", starting_pose[2])),
    }
    approach_height = int(MIM_ARM_PICK_AND_PLACE_APPROACH_DELTA)
    phase_specs = [
        (
            "move_above_pick_target",
            "move_to",
            {
                "x": requested_pick_target["x"],
                "y": requested_pick_target["y"],
                "z": requested_pick_target["z"] + approach_height,
            },
        ),
        (
            "descend_to_pick_target",
            "move_to",
            {
                "x": requested_pick_target["x"],
                "y": requested_pick_target["y"],
                "z": requested_pick_target["z"],
            },
        ),
        ("close_gripper", "close_gripper", {}),
        (
            "lift_from_pick_target",
            "move_to",
            {
                "x": requested_pick_target["x"],
                "y": requested_pick_target["y"],
                "z": requested_pick_target["z"] + approach_height,
            },
        ),
        (
            "move_above_place_target",
            "move_to",
            {
                "x": requested_place_target["x"],
                "y": requested_place_target["y"],
                "z": requested_place_target["z"] + approach_height,
            },
        ),
        (
            "descend_to_place_target",
            "move_to",
            {
                "x": requested_place_target["x"],
                "y": requested_place_target["y"],
                "z": requested_place_target["z"],
            },
        ),
        ("open_gripper", "open_gripper", {}),
        (
            "lift_from_place_target",
            "move_to",
            {
                "x": requested_place_target["x"],
                "y": requested_place_target["y"],
                "z": requested_place_target["z"] + approach_height,
            },
        ),
    ]
    return _build_phased_macro_translation(
        status_surface=status_surface,
        translation_strategy="pick_and_place_macro",
        starting_pose=starting_pose,
        phase_specs=phase_specs,
        metadata={
            "starting_pose": starting_pose,
            "requested_pick_target": requested_pick_target,
            "requested_place_target": requested_place_target,
            "approach_height": approach_height,
        },
    )


def _dispatch_reflects_stop_interruption(dispatch: dict[str, Any]) -> bool:
    reason = str(dispatch.get("reason") or "").strip().lower()
    payload = _json_dict(dispatch.get("payload"))
    response = str(payload.get("response") or "").strip().upper()
    ack_source = str(payload.get("ack_source") or "").strip().lower()
    return bool(
        reason in {"execution_interrupted_by_stop", "stopped", "stop_interrupted"}
        or response in {"HOST_STOP_CONFIRMED", "HOST_STOP_IDLE_NO_MOTION"}
        or ack_source in {"go_safe", "idle_state", "stop"}
    )


def _dispatch_failure_outcome(dispatch: dict[str, Any]) -> tuple[str, str, str]:
    if bool(dispatch.get("timed_out")):
        return "timed_out", str(dispatch.get("reason") or "execution_timeout"), "timed_out"
    if _dispatch_reflects_stop_interruption(dispatch):
        return "failed", "execution_interrupted_by_stop", "interrupted"
    return "failed", str(dispatch.get("reason") or "transport_dispatch_failed"), "failed"


def _after_pose_from_state(after_state: dict[str, Any]) -> list[int] | None:
    pose = after_state.get("current_pose") if isinstance(after_state, dict) else None
    if not isinstance(pose, list) or len(pose) < 6:
        return None
    return [int(round(float(item))) for item in pose[:6]]


def _macro_end_effector_state(*, command_name: str, after_pose: list[int] | None) -> dict[str, Any]:
    gripper_angle = None if after_pose is None else int(after_pose[5])
    if command_name == "pick_at":
        gripper_state = "closed"
    elif command_name == "place_at":
        gripper_state = "open"
    elif command_name == "pick_and_place":
        gripper_state = "open"
    else:
        gripper_state = "unknown"
    return {
        "gripper_state": gripper_state,
        "gripper_angle": gripper_angle,
    }


def _macro_result_output(
    *,
    command_name: str,
    translation: dict[str, Any],
    phase: str,
    phase_history: list[dict[str, Any]],
    completed_subactions: list[str],
    failed_subaction: str | None,
    interruption_cause: str | None,
    after_state: dict[str, Any],
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projected_pose = translation.get("projected_pose") if isinstance(translation.get("projected_pose"), list) else []
    starting_pose = translation.get("starting_pose") if isinstance(translation.get("starting_pose"), list) else []
    after_pose = _after_pose_from_state(after_state)
    payload = {
        "phase": phase,
        "phase_history": phase_history,
        "completed_subactions": completed_subactions,
        "failed_subaction": failed_subaction,
        "interruption_cause": interruption_cause,
        "final_pose_summary": {
            "starting_pose": starting_pose,
            "projected_pose": projected_pose,
            "after_pose": after_pose,
        },
        "end_effector_state": _macro_end_effector_state(command_name=command_name, after_pose=after_pose),
    }
    if replay is not None:
        payload["replay"] = replay
    return payload


def _macro_phase_names(translation: dict[str, Any]) -> list[str]:
    return [
        str(_json_dict(phase).get("phase") or "").strip()
        for phase in _json_list(translation.get("phases"))
        if str(_json_dict(phase).get("phase") or "").strip()
    ]


def _macro_replay_descriptor(
    *,
    request_id: str | None,
    resume_from_phase: str | None,
    carried_forward_subactions: list[str],
    replayable_phases_remaining: list[str],
    requested: bool,
    eligible: bool,
    replay_reason: str,
) -> dict[str, Any]:
    descriptor = {
        "eligible": bool(eligible),
        "requested": bool(requested),
        "replay_source_request_id": str(request_id or "").strip() or None,
        "resume_from_phase": str(resume_from_phase or "").strip() or None,
        "carried_forward_subactions": list(carried_forward_subactions),
        "replayable_phases_remaining": list(replayable_phases_remaining),
        "replay_reason": replay_reason,
    }
    if descriptor["eligible"] and descriptor["replay_source_request_id"] and descriptor["resume_from_phase"]:
        descriptor["suggested_metadata_json"] = {
            "macro_replay": {
                "replay_of_request_id": descriptor["replay_source_request_id"],
                "resume_from_phase": descriptor["resume_from_phase"],
            }
        }
    return descriptor


def _macro_replay_plan(
    *,
    shared_root: Path,
    request: dict[str, Any],
    command_name: str,
    translation: dict[str, Any],
) -> dict[str, Any]:
    metadata = _json_dict(request.get("metadata_json"))
    replay_meta = _json_dict(metadata.get("macro_replay"))
    source_request_id = str(replay_meta.get("replay_of_request_id") or "").strip()
    available_phases = _macro_phase_names(translation)

    if not source_request_id:
        return {
            "requested": False,
            "valid": True,
            "source_request_id": None,
            "resume_from_phase": None,
            "carried_forward_subactions": [],
            "carried_phase_history": [],
            "remaining_phases": list(_json_list(translation.get("phases"))),
            "replay": _macro_replay_descriptor(
                request_id=str(request.get("request_id") or "").strip(),
                resume_from_phase=None,
                carried_forward_subactions=[],
                replayable_phases_remaining=[],
                requested=False,
                eligible=False,
                replay_reason="no_replay_requested",
            ),
        }

    state = load_execution_lane_state(shared_root)
    processed_requests = _json_dict(state.get("processed_requests"))
    previous = _json_dict(processed_requests.get(source_request_id))
    previous_result = _json_dict(previous.get("result"))
    previous_output = _json_dict(previous_result.get("output"))
    previous_command_name = str(previous.get("command_name") or "").strip()
    previous_reason = str(previous_result.get("reason") or "").strip()
    requested_phase = str(replay_meta.get("resume_from_phase") or previous_output.get("failed_subaction") or "").strip()

    if not previous:
        return {
            "requested": True,
            "valid": False,
            "error_reason": "macro_replay_source_request_unknown",
            "replay": _macro_replay_descriptor(
                request_id=source_request_id,
                resume_from_phase=requested_phase,
                carried_forward_subactions=[],
                replayable_phases_remaining=[],
                requested=True,
                eligible=False,
                replay_reason="macro_replay_source_request_unknown",
            ),
        }

    if previous_command_name != command_name:
        return {
            "requested": True,
            "valid": False,
            "error_reason": "macro_replay_command_mismatch",
            "replay": _macro_replay_descriptor(
                request_id=source_request_id,
                resume_from_phase=requested_phase,
                carried_forward_subactions=[],
                replayable_phases_remaining=[],
                requested=True,
                eligible=False,
                replay_reason="macro_replay_command_mismatch",
            ),
        }

    if previous_reason != "execution_interrupted_by_stop":
        return {
            "requested": True,
            "valid": False,
            "error_reason": "macro_replay_source_not_interrupted",
            "replay": _macro_replay_descriptor(
                request_id=source_request_id,
                resume_from_phase=requested_phase,
                carried_forward_subactions=[],
                replayable_phases_remaining=[],
                requested=True,
                eligible=False,
                replay_reason="macro_replay_source_not_interrupted",
            ),
        }

    if requested_phase not in available_phases:
        return {
            "requested": True,
            "valid": False,
            "error_reason": "macro_replay_phase_unknown",
            "replay": _macro_replay_descriptor(
                request_id=source_request_id,
                resume_from_phase=requested_phase,
                carried_forward_subactions=[],
                replayable_phases_remaining=[],
                requested=True,
                eligible=False,
                replay_reason="macro_replay_phase_unknown",
            ),
        }

    start_index = available_phases.index(requested_phase)
    carried_forward_subactions = [
        phase_name
        for phase_name in _json_list(previous_output.get("completed_subactions"))
        if str(phase_name).strip() in available_phases[:start_index]
    ]
    carried_phase_history = []
    for entry in _json_list(previous_output.get("phase_history")):
        phase_entry = _json_dict(entry)
        phase_name = str(phase_entry.get("phase") or "").strip()
        if phase_name in carried_forward_subactions:
            carried_phase_history.append({**phase_entry, "carried_forward": True, "replayed_from_request_id": source_request_id})

    return {
        "requested": True,
        "valid": True,
        "source_request_id": source_request_id,
        "resume_from_phase": requested_phase,
        "carried_forward_subactions": carried_forward_subactions,
        "carried_phase_history": carried_phase_history,
        "remaining_phases": _json_list(translation.get("phases"))[start_index:],
        "replay": _macro_replay_descriptor(
            request_id=source_request_id,
            resume_from_phase=requested_phase,
            carried_forward_subactions=carried_forward_subactions,
            replayable_phases_remaining=available_phases[start_index:],
            requested=True,
            eligible=True,
            replay_reason="bounded_macro_replay_requested",
        ),
    }


def _execute_arm_translation(
    *,
    base_url: str,
    translation: dict[str, Any],
    timeout_seconds: int,
    request_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    dispatches: list[dict[str, Any]] = []
    for step in _json_list(translation.get("steps")):
        dispatch = _dispatch_arm_step(
            base_url,
            servo=int(_json_dict(step).get("servo") or 0),
            angle=int(_json_dict(step).get("angle") or 0),
            timeout_seconds=timeout_seconds,
            request_context=request_context,
        )
        dispatches.append({**_json_dict(step), **dispatch})
        if not dispatch["ok"]:
            after_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
            result_status, reason, phase_status = _dispatch_failure_outcome(dispatch)
            return {
                "succeeded": False,
                "dispatches": dispatches,
                "after_state": after_state,
                "failed_dispatch": dispatches[-1],
                "result_status": result_status,
                "reason": reason,
                "phase_status": phase_status,
            }

    gripper_step = _json_dict(translation.get("gripper_step"))
    if gripper_step:
        dispatch = _dispatch_arm_step(
            base_url,
            servo=int(gripper_step.get("servo") or 0),
            angle=int(gripper_step.get("angle") or 0),
            timeout_seconds=timeout_seconds,
            request_context=request_context,
        )
        dispatches.append({**gripper_step, **dispatch})
        if not dispatch["ok"]:
            after_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
            result_status, reason, phase_status = _dispatch_failure_outcome(dispatch)
            return {
                "succeeded": False,
                "dispatches": dispatches,
                "after_state": after_state,
                "failed_dispatch": dispatches[-1],
                "result_status": result_status,
                "reason": reason,
                "phase_status": phase_status,
            }

    return {
        "succeeded": True,
        "dispatches": dispatches,
        "after_state": _fetch_arm_state(base_url, timeout_seconds=timeout_seconds),
        "failed_dispatch": None,
        "result_status": "succeeded",
        "reason": "hardware_transport_succeeded",
        "phase_status": "completed",
    }


def _arm_base_url(status_surface: dict[str, Any]) -> str:
    override = _env_text("MIM_ARM_HTTP_BASE_URL", "")
    if override:
        return override.rstrip("/")
    arm_state_url = str(_json_dict(status_surface.get("arm_state_probe")).get("url") or "").strip()
    if arm_state_url.endswith("/arm_state"):
        candidate = arm_state_url[: -len("/arm_state")]
        if candidate.startswith("http://127.0.0.1") or candidate.startswith("http://localhost"):
            host = _env_text("MIM_ARM_SSH_HOST", "192.168.1.90")
            port = _env_int("MIM_ARM_HTTP_PORT", 5000)
            return f"http://{host}:{port}"
        return candidate
    host = _env_text("MIM_ARM_SSH_HOST", "192.168.1.90")
    port = _env_int("MIM_ARM_HTTP_PORT", 5000)
    return f"http://{host}:{port}"


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if isinstance(headers, dict):
        request_headers.update({str(key): str(value) for key, value in headers.items() if str(key).strip()})
    request = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers=request_headers,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _post_no_body_json(
    url: str,
    timeout_seconds: int,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = {"Content-Type": "application/json"}
    if isinstance(headers, dict):
        request_headers.update({str(key): str(value) for key, value in headers.items() if str(key).strip()})
    request = urllib_request.Request(
        url,
        data=json.dumps(payload if isinstance(payload, dict) else {}).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _get_json(url: str, timeout_seconds: int) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _request_transport_context(request: dict[str, Any]) -> dict[str, str]:
    metadata = _json_dict(request.get("metadata_json"))
    request_id = str(request.get("request_id") or "").strip()
    task_id = str(metadata.get("task_id") or "").strip()
    correlation_id = str(metadata.get("correlation_id") or "").strip()
    context = {
        "request_id": request_id,
        "task_id": task_id,
        "correlation_id": correlation_id,
        "command_name": str(_json_dict(request.get("command")).get("name") or "").strip(),
        "producer": str(metadata.get("producer") or "mim").strip() or "mim",
        "lane": str(metadata.get("lane") or "mim_arm_execution").strip() or "mim_arm_execution",
    }
    return {key: value for key, value in context.items() if value}


def _transport_headers(request_context: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    if str(request_context.get("request_id") or "").strip():
        headers["X-MIM-Request-ID"] = str(request_context["request_id"])
    if str(request_context.get("task_id") or "").strip():
        headers["X-MIM-Task-ID"] = str(request_context["task_id"])
    if str(request_context.get("correlation_id") or "").strip():
        headers["X-MIM-Correlation-ID"] = str(request_context["correlation_id"])
    if str(request_context.get("lane") or "").strip():
        headers["X-MIM-Lane"] = str(request_context["lane"])
    return headers


def _live_transport_enabled() -> bool:
    value = _env_text("MIM_ARM_EXECUTION_ENABLE", "1").lower()
    return value not in {"0", "false", "no", "off"}


def _current_pose(status_surface: dict[str, Any]) -> list[int]:
    pose = status_surface.get("current_pose")
    if isinstance(pose, list) and len(pose) >= 6:
        try:
            return [int(round(float(item))) for item in pose[:6]]
        except Exception:
            return list(MIM_ARM_DEFAULT_POSE)
    return list(MIM_ARM_DEFAULT_POSE)


def _translate_mim_arm_steps(request: dict[str, Any], status_surface: dict[str, Any]) -> dict[str, Any]:
    command = _json_dict(request.get("command"))
    command_name = str(command.get("name") or "").strip()
    command_args = _json_dict(command.get("args"))
    pose = _current_pose(status_surface)

    if command_name == "move_to":
        return _project_direct_translation(
            pose,
            x=float(command_args.get("x", pose[0])),
            y=float(command_args.get("y", pose[1])),
            z=float(command_args.get("z", pose[2])),
        )

    if command_name == "move_relative":
        return _project_relative_translation(
            pose,
            dx=float(command_args.get("dx", 0)),
            dy=float(command_args.get("dy", 0)),
            dz=float(command_args.get("dz", 0)),
        )

    if command_name == "move_relative_then_set_gripper":
        return _project_relative_translation(
            pose,
            dx=float(command_args.get("dx", 0)),
            dy=float(command_args.get("dy", 0)),
            dz=float(command_args.get("dz", 0)),
            include_gripper_position=float(command_args.get("position", 0)),
        )

    if command_name == "pick_at":
        return _build_pick_at_translation(status_surface, command_args)

    if command_name == "place_at":
        return _build_place_at_translation(status_surface, command_args)

    if command_name == "pick_and_place":
        return _build_pick_and_place_translation(status_surface, command_args)

    if command_name == "move_home":
        return {
            "translation_strategy": "safe_home_route",
            "route": "go_safe",
            "steps": [],
            "projected_pose": list(MIM_ARM_HOME_POSE),
            "transport_supported": True,
        }

    if command_name == "set_speed":
        requested_level = str(command_args.get("level") or "").strip().lower()
        return {
            "translation_strategy": "speed_route",
            "steps": [],
            "transport_supported": True,
            "route": "set_speed",
            "requested_level": requested_level,
            "requested_speed_ms": int(MIM_ARM_SPEED_LEVELS[requested_level]),
        }

    if command_name == "stop":
        return {
            "translation_strategy": "stop_route",
            "steps": [],
            "transport_supported": True,
            "route": "stop",
        }

    if command_name == "set_gripper":
        target_angle = _gripper_angle_for_position(float(command_args.get("position", 0)))
        requested_position = float(command_args.get("position", 0))
    else:
        target_angle = MIM_ARM_CLAW_OPEN_ANGLE if command_name == "open_gripper" else MIM_ARM_CLAW_CLOSE_ANGLE
        requested_position = 100.0 if command_name == "open_gripper" else 0.0
    projected = list(pose)
    projected[5] = _clamp_servo(5, target_angle)
    return {
        "translation_strategy": "single_servo_gripper",
        "steps": [{"servo": 5, "angle": projected[5], "axis": "claw"}],
        "projected_pose": projected,
        "requested_position": requested_position,
        "transport_supported": True,
    }


def _dispatch_arm_step(
    base_url: str,
    *,
    servo: int,
    angle: int,
    timeout_seconds: int,
    request_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}/move"
    context = request_context if isinstance(request_context, dict) else {}
    request_payload = {"servo": servo, "angle": angle}
    if context:
        request_payload.update(context)
    try:
        status_code, payload = _post_json(
            url,
            request_payload,
            timeout_seconds=timeout_seconds,
            headers=_transport_headers(context),
        )
    except Exception as exc:
        return {
            "ok": False,
            "timed_out": False,
            "reason": "transport_unavailable",
            "status_code": None,
            "payload": {"message": str(exc)},
            "url": url,
            "servo": servo,
            "angle": angle,
            "request_context": context,
        }

    result = {
        "ok": status_code == 200 and str(payload.get("status") or "").strip().lower() == "ok",
        "timed_out": status_code == 504 or str(payload.get("status") or "").strip().lower() == "timeout",
        "reason": "",
        "status_code": status_code,
        "payload": payload,
        "url": url,
        "servo": servo,
        "angle": angle,
        "request_context": context,
    }
    if result["ok"]:
        result["reason"] = "transport_dispatch_succeeded"
    elif result["timed_out"]:
        result["reason"] = "execution_timeout"
    elif status_code >= 500 and "serial port unavailable" in str(payload.get("message") or "").lower():
        result["reason"] = "transport_unavailable"
    else:
        result["reason"] = "transport_dispatch_failed"
    return result


def _dispatch_arm_home(
    base_url: str,
    *,
    timeout_seconds: int,
    request_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}/go_safe"
    context = request_context if isinstance(request_context, dict) else {}
    try:
        status_code, payload = _post_no_body_json(
            url,
            timeout_seconds=timeout_seconds,
            payload=context,
            headers=_transport_headers(context),
        )
    except Exception as exc:
        return {
            "ok": False,
            "timed_out": False,
            "reason": "transport_unavailable",
            "status_code": None,
            "payload": {"message": str(exc)},
            "url": url,
            "request_context": context,
        }

    ok = status_code == 200 and str(payload.get("status") or "ok").strip().lower() in {"ok", "success", "done", "accepted"}
    timed_out = status_code == 504 or str(payload.get("status") or "").strip().lower() == "timeout"
    if ok:
        reason = "transport_dispatch_succeeded"
    elif timed_out:
        reason = "execution_timeout"
    elif status_code >= 500 and "serial port unavailable" in str(payload.get("message") or "").lower():
        reason = "transport_unavailable"
    else:
        reason = "transport_dispatch_failed"
    return {
        "ok": ok,
        "timed_out": timed_out,
        "reason": reason,
        "status_code": status_code,
        "payload": payload,
        "url": url,
        "route": "go_safe",
        "request_context": context,
    }


def _dispatch_arm_speed(
    base_url: str,
    *,
    speed_ms: int,
    timeout_seconds: int,
    request_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}/set_speed"
    context = request_context if isinstance(request_context, dict) else {}
    request_payload = {"speed": int(speed_ms)}
    if context:
        request_payload.update(context)
    try:
        status_code, payload = _post_json(
            url,
            request_payload,
            timeout_seconds=timeout_seconds,
            headers=_transport_headers(context),
        )
    except Exception as exc:
        return {
            "ok": False,
            "timed_out": False,
            "reason": "transport_unavailable",
            "status_code": None,
            "payload": {"message": str(exc)},
            "url": url,
            "route": "set_speed",
            "request_context": context,
        }

    ok = status_code == 200 and str(payload.get("status") or "ok").strip().lower() in {"ok", "success", "done", "accepted"}
    return {
        "ok": ok,
        "timed_out": False,
        "reason": "transport_dispatch_succeeded" if ok else "transport_dispatch_failed",
        "status_code": status_code,
        "payload": payload,
        "url": url,
        "route": "set_speed",
        "speed_ms": int(speed_ms),
        "request_context": context,
    }


def _dispatch_arm_stop(
    base_url: str,
    *,
    timeout_seconds: int,
    request_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}/stop"
    context = request_context if isinstance(request_context, dict) else {}
    try:
        status_code, payload = _post_no_body_json(
            url,
            timeout_seconds=timeout_seconds,
            payload=context,
            headers=_transport_headers(context),
        )
    except Exception as exc:
        return {
            "ok": False,
            "timed_out": False,
            "reason": "transport_unavailable",
            "status_code": None,
            "payload": {"message": str(exc)},
            "url": url,
            "route": "stop",
            "request_context": context,
        }

    ok = status_code == 200 and str(payload.get("status") or "").strip().lower() in {"ok", "success", "done", "accepted"}
    timed_out = status_code == 504 or str(payload.get("status") or "").strip().lower() == "timeout"
    if ok:
        reason = "transport_dispatch_succeeded"
    elif timed_out:
        reason = "execution_timeout"
    elif status_code >= 500 and "serial port unavailable" in str(payload.get("message") or "").lower():
        reason = "transport_unavailable"
    else:
        reason = "transport_dispatch_failed"
    return {
        "ok": ok,
        "timed_out": timed_out,
        "reason": reason,
        "status_code": status_code,
        "payload": payload,
        "url": url,
        "route": "stop",
        "request_context": context,
    }


def _fetch_arm_state(base_url: str, timeout_seconds: int) -> dict[str, Any]:
    try:
        status_code, payload = _get_json(f"{base_url}/arm_state", timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    if status_code != 200:
        return {"status": "error", "status_code": status_code, "payload": payload}
    return payload


def build_tod_execution_request(
    *,
    request_id: str,
    command_name: str,
    command_args: dict[str, Any] | None = None,
    target: str = TARGET_MIM_ARM,
    sequence: int = 1,
    supersedes_request_id: str = "",
    expires_at: str = "",
    metadata_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from core.tod_mim_contract import (
        CONTRACT_SCHEMA_VERSION,
        CONTRACT_VERSION,
        PRIMARY_TRANSPORT_ID,
        LOCAL_TRANSPORT_SURFACE,
        build_source_identity,
        build_transport,
    )

    metadata = metadata_json or {}
    task_id = str(metadata.get("task_id") or "").strip()
    correlation_id = str(metadata.get("correlation_id") or request_id).strip() or request_id
    objective_id = str(metadata.get("objective_id") or metadata.get("objective") or "objective-arm-execution").strip()
    payload = {
        "version": "1.0",
        "packet_type": "mim-tod-task-request-v1",
        "message_kind": "request",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "objective_id": objective_id,
        "target": target,
        "target_executor": target,
        "sequence": sequence,
        "issued_at": utc_now(),
        "generated_at": utc_now(),
        "expires_at": expires_at or (datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")),
        "supersedes_request_id": supersedes_request_id,
        "command": {"name": command_name, "args": command_args or {}},
        "task_classification": "governed_execution",
        "source_identity": build_source_identity(actor="TOD", service_name="execution_lane_service", instance_id=str(metadata.get("producer") or "TOD")),
        "transport": build_transport(transport_id=PRIMARY_TRANSPORT_ID, surface=LOCAL_TRANSPORT_SURFACE),
        "execution_policy": {"policy_outcome": "allow", "shadow_mode": True},
        "idempotency": {"key": request_id, "duplicate_execution_allowed": False},
        "fallback_policy": {"activation_rule": "primary_transport_unavailable_or_reconciliation_blocked", "allowed": True},
        "metadata_json": metadata,
    }
    if task_id:
        payload["task_id"] = task_id
    return payload


def load_execution_lane_state(shared_root: Path) -> dict[str, Any]:
    state = _read_json(_state_path(shared_root))
    if state:
        return state
    return {
        "generated_at": utc_now(),
        "processed_requests": {},
        "superseded_request_ids": [],
    }


def _save_execution_lane_state(shared_root: Path, state: dict[str, Any]) -> None:
    state["generated_at"] = utc_now()
    _write_json(_state_path(shared_root), state)


def read_execution_events(shared_root: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_event_log_path(shared_root))


def read_execution_requests(shared_root: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_request_log_path(shared_root))


def build_execution_target_profile(
    *,
    target: str,
    shared_root: Path,
    status_surface: dict[str, Any] | None = None,
    hardware_transport_enabled: bool = False,
) -> dict[str, Any]:
    status_surface = _json_dict(status_surface)
    real_target = str(target or "").strip() or TARGET_SYNTHETIC_ARM
    state_snapshot = load_execution_lane_state(shared_root)
    processed_requests = _json_dict(state_snapshot.get("processed_requests"))
    latest_request = list(processed_requests.values())[-1] if processed_requests else {}
    command_capabilities = {
        command_name: {
            "parameter_schema": _json_dict(spec.get("parameter_schema")),
            "result_schema": _json_dict(spec.get("result_schema")),
            "transport_mode": "synthetic" if real_target == TARGET_SYNTHETIC_ARM else "hardware_transport",
            "transport_support": _json_dict(spec.get("transport_support")),
            "safety_constraints": _json_dict(spec.get("safety_constraints")),
            "idempotency_behavior": COMMAND_BEHAVIOR["idempotency"],
            "timeout_behavior": COMMAND_BEHAVIOR["timeout_behavior"],
            "cancellation_behavior": COMMAND_BEHAVIOR["cancellation_behavior"],
            "macro_replay_behavior": COMMAND_BEHAVIOR["macro_replay_behavior"] if command_name in {"pick_at", "place_at", "pick_and_place"} else "not_applicable",
            "available": bool(
                _json_dict(spec.get("transport_support")).get(real_target, "unsupported") == "supported"
                and (real_target == TARGET_SYNTHETIC_ARM or hardware_transport_enabled)
            ),
        }
        for command_name, spec in COMMAND_SPECS.items()
    }
    return {
        "target": real_target,
        "contract_version": "1.0",
        "synthetic_only": real_target == TARGET_SYNTHETIC_ARM,
        "execution_mode": "synthetic" if real_target == TARGET_SYNTHETIC_ARM else "hardware_transport",
        "live_transport_available": bool(real_target == TARGET_SYNTHETIC_ARM or hardware_transport_enabled),
        "allowed_commands": sorted(COMMAND_SPECS.keys()),
        "command_capabilities": command_capabilities,
        "artifacts": {
            "request_log": str(_request_log_path(shared_root)),
            "event_log": str(_event_log_path(shared_root)),
            "state": str(_state_path(shared_root)),
            "latest_target_request": str(_arm_request_path(shared_root)) if real_target == TARGET_MIM_ARM else "",
            "live_transport_check": str(arm_live_check_path(shared_root)) if real_target == TARGET_MIM_ARM else "",
            "tod_request": str(tod_request_path(shared_root)) if real_target == TARGET_MIM_ARM else "",
        },
        "dispatch_ready": bool(
            real_target == TARGET_SYNTHETIC_ARM
            or (
                hardware_transport_enabled
                and bool(status_surface.get("arm_online", False))
                and bool(status_surface.get("serial_ready", False))
                and status_surface.get("estop_ok") is True
            )
        ),
        "current_execution_state": {
            "processed_request_count": len(processed_requests),
            "superseded_request_count": len(_json_list(state_snapshot.get("superseded_request_ids"))),
            "replayable_macro_count": len(
                [
                    summary
                    for summary in processed_requests.values()
                    if str(_json_dict(_json_dict(summary).get("result")).get("reason") or "") == "execution_interrupted_by_stop"
                    and str(_json_dict(summary).get("command_name") or "") in {"pick_at", "place_at", "pick_and_place"}
                ]
            ),
            "latest_request": latest_request,
        },
        "status_snapshot": {
            "arm_online": status_surface.get("arm_online"),
            "serial_ready": status_surface.get("serial_ready"),
            "estop_ok": status_surface.get("estop_ok"),
            "mode": status_surface.get("mode"),
            "current_pose": status_surface.get("current_pose"),
            "last_command_result": status_surface.get("last_command_result"),
        },
    }


def _command_validation_error(command: dict[str, Any]) -> str:
    command_name = str(command.get("name") or "").strip()
    if command_name not in COMMAND_SPECS:
        return f"unsupported_command:{command_name or 'unknown'}"
    command_args = _json_dict(command.get("args"))
    required_args = set(COMMAND_SPECS[command_name]["required_args"])
    missing_args = sorted(required_args.difference(command_args.keys()))
    if missing_args:
        return f"missing_command_args:{','.join(missing_args)}"
    if command_name == "set_gripper":
        try:
            position = float(command_args.get("position"))
        except Exception:
            return "invalid_command_args:position_type"
        if position < 0 or position > 100:
            return "invalid_command_args:position_out_of_range"
    if command_name == "move_relative_then_set_gripper":
        try:
            position = float(command_args.get("position"))
        except Exception:
            return "invalid_command_args:position_type"
        if position < 0 or position > 100:
            return "invalid_command_args:position_out_of_range"
    if command_name == "set_speed":
        level = str(command_args.get("level") or "").strip().lower()
        if level not in MIM_ARM_SPEED_LEVELS:
            return "invalid_command_args:level_unsupported"
    return ""


def _request_validation_errors(request: dict[str, Any], *, expected_target: str) -> list[str]:
    errors: list[str] = []
    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        errors.append("missing_request_id")

    target = str(request.get("target") or "").strip()
    if target != expected_target:
        errors.append("wrong_target")

    command = _json_dict(request.get("command"))
    command_error = _command_validation_error(command)
    if command_error:
        errors.append(command_error)

    expires_at = _parse_timestamp(request.get("expires_at"))
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        errors.append("stale_request")
    return errors


def _record_summary(request: dict[str, Any], ack: dict[str, Any] | None, result: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "request_id": str(request.get("request_id") or "").strip(),
        "target": str(request.get("target") or "").strip(),
        "sequence": int(request.get("sequence") or 0),
        "command_name": str(_json_dict(request.get("command")).get("name") or "").strip(),
        "ack": ack or {},
        "result": result or {},
    }


def _build_ack(
    request: dict[str, Any],
    *,
    ack_status: str,
    reason: str,
    expected_target: str,
) -> dict[str, Any]:
    return {
        "event_type": "ack",
        "request_id": str(request.get("request_id") or "").strip(),
        "target": expected_target,
        "ack_status": ack_status,
        "reason": reason,
        "emitted_at": utc_now(),
        "sequence": int(request.get("sequence") or 0),
        "command": _json_dict(request.get("command")),
    }


def _build_result(
    request: dict[str, Any],
    *,
    result_status: str,
    reason: str,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": "result",
        "request_id": str(request.get("request_id") or "").strip(),
        "target": str(request.get("target") or "").strip(),
        "result_status": result_status,
        "reason": reason,
        "emitted_at": utc_now(),
        "sequence": int(request.get("sequence") or 0),
        "command": _json_dict(request.get("command")),
        "output": output or {},
    }


def _execute_synthetic_request(request: dict[str, Any], *, shared_root: Path) -> dict[str, Any]:
    command = _json_dict(request.get("command"))
    command_name = str(command.get("name") or "").strip()
    command_args = _json_dict(command.get("args"))
    forced_outcome = str(request.get("simulation_outcome") or "succeeded").strip().lower() or "succeeded"

    if forced_outcome == "timed_out":
        return _build_result(
            request,
            result_status="timed_out",
            reason="execution_timeout",
            output={"command_name": command_name, "timed_out": True},
        )
    if forced_outcome == "failed":
        return _build_result(
            request,
            result_status="failed",
            reason="synthetic_execution_failure",
            output={"command_name": command_name, "failed": True},
        )

    if command_name == "move_to":
        pose = {
            "x": command_args.get("x"),
            "y": command_args.get("y"),
            "z": command_args.get("z"),
        }
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"pose": pose, "gripper_state": "unchanged"},
        )
    if command_name == "move_relative":
        relative_delta = {
            "dx": command_args.get("dx"),
            "dy": command_args.get("dy"),
            "dz": command_args.get("dz"),
        }
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"relative_delta": relative_delta, "motion_state": "relative_projected"},
        )
    if command_name == "move_relative_then_set_gripper":
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={
                "relative_delta": {
                    "dx": command_args.get("dx"),
                    "dy": command_args.get("dy"),
                    "dz": command_args.get("dz"),
                },
                "gripper_position": float(command_args.get("position", 0)),
                "motion_state": "relative_projection_then_gripper",
            },
        )
    if command_name == "pick_at":
        phase_history = [
            {"phase": "move_above_target", "status": "completed"},
            {"phase": "descend_to_target", "status": "completed"},
            {"phase": "close_gripper", "status": "completed"},
            {"phase": "lift_from_target", "status": "completed"},
        ]
        translation = _translate_mim_arm_steps(request, {"current_pose": list(MIM_ARM_DEFAULT_POSE)})
        replay_plan = _macro_replay_plan(
            shared_root=shared_root,
            request=request,
            command_name=command_name,
            translation=translation,
        )
        if bool(replay_plan.get("requested")) and not bool(replay_plan.get("valid")):
            return _build_result(
                request,
                result_status="failed",
                reason=str(replay_plan.get("error_reason") or "invalid_macro_replay_request"),
                output={
                    "translation": translation,
                    "replay": _json_dict(replay_plan.get("replay")),
                },
            )
        if bool(replay_plan.get("requested")):
            carried = list(replay_plan.get("carried_forward_subactions") or [])
            phase_history = list(replay_plan.get("carried_phase_history") or []) + [
                entry for entry in phase_history if str(entry.get("phase") or "") not in carried
            ]
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={
                **_macro_result_output(
                    command_name=command_name,
                    translation=translation,
                    phase="completed",
                    phase_history=phase_history,
                    completed_subactions=[entry["phase"] for entry in phase_history],
                    failed_subaction=None,
                    interruption_cause=None,
                    after_state={"current_pose": translation.get("projected_pose")},
                    replay=_json_dict(replay_plan.get("replay")) if isinstance(replay_plan, dict) else _macro_replay_descriptor(
                        request_id=str(request.get("request_id") or "").strip(),
                        resume_from_phase=None,
                        carried_forward_subactions=[],
                        replayable_phases_remaining=[],
                        requested=False,
                        eligible=False,
                        replay_reason="no_replay_requested",
                    ),
                ),
                "target_pose": {
                    "x": command_args.get("x"),
                    "y": command_args.get("y"),
                    "z": command_args.get("z"),
                },
                "motion_state": "pick_completed",
            },
        )
    if command_name == "place_at":
        phase_history = [
            {"phase": "move_above_target", "status": "completed"},
            {"phase": "descend_to_target", "status": "completed"},
            {"phase": "open_gripper", "status": "completed"},
            {"phase": "retract_or_lift", "status": "completed"},
        ]
        translation = _translate_mim_arm_steps(request, {"current_pose": list(MIM_ARM_DEFAULT_POSE)})
        replay_plan = _macro_replay_plan(
            shared_root=shared_root,
            request=request,
            command_name=command_name,
            translation=translation,
        )
        if bool(replay_plan.get("requested")) and not bool(replay_plan.get("valid")):
            return _build_result(
                request,
                result_status="failed",
                reason=str(replay_plan.get("error_reason") or "invalid_macro_replay_request"),
                output={
                    "translation": translation,
                    "replay": _json_dict(replay_plan.get("replay")),
                },
            )
        if bool(replay_plan.get("requested")):
            carried = list(replay_plan.get("carried_forward_subactions") or [])
            phase_history = list(replay_plan.get("carried_phase_history") or []) + [
                entry for entry in phase_history if str(entry.get("phase") or "") not in carried
            ]
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={
                **_macro_result_output(
                    command_name=command_name,
                    translation=translation,
                    phase="completed",
                    phase_history=phase_history,
                    completed_subactions=[entry["phase"] for entry in phase_history],
                    failed_subaction=None,
                    interruption_cause=None,
                    after_state={"current_pose": translation.get("projected_pose")},
                    replay=_json_dict(replay_plan.get("replay")) if isinstance(replay_plan, dict) else _macro_replay_descriptor(
                        request_id=str(request.get("request_id") or "").strip(),
                        resume_from_phase=None,
                        carried_forward_subactions=[],
                        replayable_phases_remaining=[],
                        requested=False,
                        eligible=False,
                        replay_reason="no_replay_requested",
                    ),
                ),
                "target_pose": {
                    "x": command_args.get("x"),
                    "y": command_args.get("y"),
                    "z": command_args.get("z"),
                },
                "motion_state": "place_completed",
            },
        )
    if command_name == "pick_and_place":
        phase_history = [
            {"phase": "move_above_pick_target", "status": "completed"},
            {"phase": "descend_to_pick_target", "status": "completed"},
            {"phase": "close_gripper", "status": "completed"},
            {"phase": "lift_from_pick_target", "status": "completed"},
            {"phase": "move_above_place_target", "status": "completed"},
            {"phase": "descend_to_place_target", "status": "completed"},
            {"phase": "open_gripper", "status": "completed"},
            {"phase": "lift_from_place_target", "status": "completed"},
        ]
        translation = _translate_mim_arm_steps(request, {"current_pose": list(MIM_ARM_DEFAULT_POSE)})
        replay_plan = _macro_replay_plan(
            shared_root=shared_root,
            request=request,
            command_name=command_name,
            translation=translation,
        )
        if bool(replay_plan.get("requested")) and not bool(replay_plan.get("valid")):
            return _build_result(
                request,
                result_status="failed",
                reason=str(replay_plan.get("error_reason") or "invalid_macro_replay_request"),
                output={
                    "translation": translation,
                    "replay": _json_dict(replay_plan.get("replay")),
                },
            )
        if bool(replay_plan.get("requested")):
            carried = list(replay_plan.get("carried_forward_subactions") or [])
            phase_history = list(replay_plan.get("carried_phase_history") or []) + [
                entry for entry in phase_history if str(entry.get("phase") or "") not in carried
            ]
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={
                **_macro_result_output(
                    command_name=command_name,
                    translation=translation,
                    phase="completed",
                    phase_history=phase_history,
                    completed_subactions=[entry["phase"] for entry in phase_history],
                    failed_subaction=None,
                    interruption_cause=None,
                    after_state={"current_pose": translation.get("projected_pose")},
                    replay=_json_dict(replay_plan.get("replay")) if isinstance(replay_plan, dict) else _macro_replay_descriptor(
                        request_id=str(request.get("request_id") or "").strip(),
                        resume_from_phase=None,
                        carried_forward_subactions=[],
                        replayable_phases_remaining=[],
                        requested=False,
                        eligible=False,
                        replay_reason="no_replay_requested",
                    ),
                ),
                "pick_target_pose": {
                    "x": command_args.get("pick_x"),
                    "y": command_args.get("pick_y"),
                    "z": command_args.get("pick_z"),
                },
                "place_target_pose": {
                    "x": command_args.get("place_x"),
                    "y": command_args.get("place_y"),
                    "z": command_args.get("place_z"),
                },
                "motion_state": "pick_and_place_completed",
            },
        )
    if command_name == "move_home":
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"pose": list(MIM_ARM_HOME_POSE), "motion_state": "home"},
        )
    if command_name == "open_gripper":
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"gripper_state": "open"},
        )
    if command_name == "set_gripper":
        position = float(command_args.get("position", 0))
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"gripper_state": "custom", "gripper_position": position},
        )
    if command_name == "set_speed":
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"speed_level": str(command_args.get("level") or "").strip().lower()},
        )
    if command_name == "stop":
        return _build_result(
            request,
            result_status="succeeded",
            reason="synthetic_execution_succeeded",
            output={"motion_state": "stopped"},
        )
    return _build_result(
        request,
        result_status="succeeded",
        reason="synthetic_execution_succeeded",
        output={"gripper_state": "closed"},
    )


def _execute_mim_arm_request(
    request: dict[str, Any],
    *,
    shared_root: Path,
    status_surface: dict[str, Any] | None,
    hardware_transport_enabled: bool,
) -> dict[str, Any]:
    status_surface = _json_dict(status_surface)
    if not bool(status_surface.get("arm_online", False)):
        return _build_result(
            request,
            result_status="failed",
            reason="arm_offline",
            output={"status_snapshot": status_surface},
        )
    if not bool(status_surface.get("serial_ready", False)):
        return _build_result(
            request,
            result_status="failed",
            reason="controller_not_ready",
            output={"status_snapshot": status_surface},
        )
    if status_surface.get("estop_ok") is not True:
        return _build_result(
            request,
            result_status="failed",
            reason="estop_not_clear",
            output={"status_snapshot": status_surface},
        )

    _write_json(
        _arm_request_path(shared_root),
        {
            "generated_at": utc_now(),
            "request": request,
            "status_snapshot": {
                "arm_online": status_surface.get("arm_online"),
                "serial_ready": status_surface.get("serial_ready"),
                "estop_ok": status_surface.get("estop_ok"),
                "mode": status_surface.get("mode"),
                "current_pose": status_surface.get("current_pose"),
            },
        },
    )

    if not hardware_transport_enabled:
        return _build_result(
            request,
            result_status="failed",
            reason="hardware_target_not_configured",
            output={
                "status_snapshot": status_surface,
                "latest_target_request": str(_arm_request_path(shared_root)),
            },
        )

    base_url = _arm_base_url(status_surface)
    timeout_seconds = _env_int("MIM_ARM_EXECUTION_TIMEOUT_SECONDS", 6)
    command_name = str(_json_dict(request.get("command")).get("name") or "").strip()
    request_context = _request_transport_context(request)
    translation_status_surface = status_surface
    if command_name in {"move_relative", "move_relative_then_set_gripper", "pick_at", "place_at", "pick_and_place"}:
        live_before_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
        live_pose = live_before_state.get("current_pose") if isinstance(live_before_state, dict) else None
        if isinstance(live_pose, list) and len(live_pose) >= 6:
            translation_status_surface = {**status_surface, "current_pose": live_pose}
    translation = _translate_mim_arm_steps(request, translation_status_surface)

    if command_name in {"pick_at", "place_at", "pick_and_place"}:
        replay_plan = _macro_replay_plan(
            shared_root=shared_root,
            request=request,
            command_name=command_name,
            translation=translation,
        )
        if bool(replay_plan.get("requested")) and not bool(replay_plan.get("valid")):
            return _build_result(
                request,
                result_status="failed",
                reason=str(replay_plan.get("error_reason") or "invalid_macro_replay_request"),
                output={
                    "translation": translation,
                    "latest_target_request": str(_arm_request_path(shared_root)),
                    "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                    "request_context": request_context,
                    "replay": _json_dict(replay_plan.get("replay")),
                },
            )
        phase_history: list[dict[str, Any]] = list(replay_plan.get("carried_phase_history") or [])
        completed_subactions: list[str] = list(replay_plan.get("carried_forward_subactions") or [])
        dispatches: list[dict[str, Any]] = []
        after_state: dict[str, Any] = {}
        for phase in list(replay_plan.get("remaining_phases") or _json_list(translation.get("phases"))):
            phase_payload = _json_dict(phase)
            phase_name = str(phase_payload.get("phase") or "").strip()
            phase_translation = _json_dict(phase_payload.get("translation"))
            execution = _execute_arm_translation(
                base_url=base_url,
                translation=phase_translation,
                timeout_seconds=timeout_seconds,
                request_context=request_context,
            )
            phase_dispatches = [{**dispatch, "phase": phase_name} for dispatch in _json_list(execution.get("dispatches"))]
            dispatches.extend(phase_dispatches)
            after_state = _json_dict(execution.get("after_state"))
            phase_status = str(execution.get("phase_status") or "completed")
            phase_record = {
                "phase": phase_name,
                "status": phase_status,
                "command": _json_dict(phase_payload.get("command")),
                "translation": phase_translation,
                "dispatches": phase_dispatches,
                "after_state": after_state,
            }
            failed_dispatch = _json_dict(execution.get("failed_dispatch"))
            if failed_dispatch:
                phase_record["failure"] = {
                    "reason": str(execution.get("reason") or "transport_dispatch_failed"),
                    "dispatch": failed_dispatch,
                }
            phase_history.append(phase_record)
            if bool(execution.get("succeeded")):
                completed_subactions.append(phase_name)
                continue
            return _build_result(
                request,
                result_status=str(execution.get("result_status") or "failed"),
                reason=str(execution.get("reason") or "transport_dispatch_failed"),
                output={
                    **_macro_result_output(
                        command_name=command_name,
                        translation=translation,
                        phase=phase_name,
                        phase_history=phase_history,
                        completed_subactions=completed_subactions,
                        failed_subaction=phase_name,
                        interruption_cause=(
                            str(execution.get("reason") or "")
                            if str(execution.get("phase_status") or "") == "interrupted"
                            else None
                        ),
                        after_state=after_state,
                        replay=(
                            _macro_replay_descriptor(
                                request_id=str(replay_plan.get("source_request_id") or request.get("request_id") or "").strip(),
                                resume_from_phase=str(replay_plan.get("resume_from_phase") or phase_name).strip() or phase_name,
                                carried_forward_subactions=list(replay_plan.get("carried_forward_subactions") or completed_subactions),
                                replayable_phases_remaining=_macro_phase_names(translation)[_macro_phase_names(translation).index(phase_name):] if phase_name in _macro_phase_names(translation) else [phase_name],
                                requested=bool(replay_plan.get("requested")),
                                eligible=str(execution.get("reason") or "") == "execution_interrupted_by_stop",
                                replay_reason=(
                                    "interrupted_macro_can_resume_from_failed_phase"
                                    if str(execution.get("reason") or "") == "execution_interrupted_by_stop"
                                    else str(execution.get("reason") or "transport_dispatch_failed")
                                ),
                            )
                        ),
                    ),
                    "translation": translation,
                    "dispatches": dispatches,
                    "after_state": after_state,
                    "latest_target_request": str(_arm_request_path(shared_root)),
                    "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                    "request_context": request_context,
                },
            )
        return _build_result(
            request,
            result_status="succeeded",
            reason="hardware_transport_succeeded",
            output={
                **_macro_result_output(
                    command_name=command_name,
                    translation=translation,
                    phase="completed",
                    phase_history=phase_history,
                    completed_subactions=completed_subactions,
                    failed_subaction=None,
                    interruption_cause=None,
                    after_state=after_state,
                    replay=(
                        _json_dict(replay_plan.get("replay"))
                        if bool(replay_plan.get("requested"))
                        else _macro_replay_descriptor(
                            request_id=str(request.get("request_id") or "").strip(),
                            resume_from_phase=None,
                            carried_forward_subactions=[],
                            replayable_phases_remaining=[],
                            requested=False,
                            eligible=False,
                            replay_reason="no_replay_requested",
                        )
                    ),
                ),
                "translation": translation,
                "dispatches": dispatches,
                "after_state": after_state,
                "latest_target_request": str(_arm_request_path(shared_root)),
                "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                "request_context": request_context,
            },
        )

    if command_name == "move_home":
        dispatch = _dispatch_arm_home(base_url, timeout_seconds=timeout_seconds, request_context=request_context)
        after_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
        if not dispatch["ok"]:
            result_status = "timed_out" if dispatch["timed_out"] else "failed"
            return _build_result(
                request,
                result_status=result_status,
                reason=str(dispatch["reason"] or "transport_dispatch_failed"),
                output={
                    "translation": translation,
                    "dispatches": [dispatch],
                    "after_state": after_state,
                    "latest_target_request": str(_arm_request_path(shared_root)),
                    "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                    "request_context": request_context,
                },
            )
        return _build_result(
            request,
            result_status="succeeded",
            reason="hardware_transport_succeeded",
            output={
                "translation": translation,
                "dispatches": [dispatch],
                "after_state": after_state,
                "latest_target_request": str(_arm_request_path(shared_root)),
                "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                "request_context": request_context,
            },
        )

    if command_name == "stop":
        dispatch = _dispatch_arm_stop(base_url, timeout_seconds=timeout_seconds, request_context=request_context)
        after_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
        if not dispatch["ok"]:
            result_status = "timed_out" if dispatch["timed_out"] else "failed"
            return _build_result(
                request,
                result_status=result_status,
                reason=str(dispatch["reason"] or "transport_dispatch_failed"),
                output={
                    "translation": translation,
                    "dispatches": [dispatch],
                    "after_state": after_state,
                    "latest_target_request": str(_arm_request_path(shared_root)),
                    "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                    "request_context": request_context,
                },
            )
        return _build_result(
            request,
            result_status="succeeded",
            reason="hardware_transport_succeeded",
            output={
                "translation": translation,
                "dispatches": [dispatch],
                "after_state": after_state,
                "latest_target_request": str(_arm_request_path(shared_root)),
                "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                "request_context": request_context,
            },
        )

    if command_name == "set_speed":
        dispatch = _dispatch_arm_speed(
            base_url,
            speed_ms=int(_json_dict(translation).get("requested_speed_ms") or 0),
            timeout_seconds=timeout_seconds,
            request_context=request_context,
        )
        after_state = _fetch_arm_state(base_url, timeout_seconds=timeout_seconds)
        if not dispatch["ok"]:
            return _build_result(
                request,
                result_status="failed",
                reason=str(dispatch["reason"] or "transport_dispatch_failed"),
                output={
                    "translation": translation,
                    "dispatches": [dispatch],
                    "after_state": after_state,
                    "latest_target_request": str(_arm_request_path(shared_root)),
                    "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                    "request_context": request_context,
                },
            )
        return _build_result(
            request,
            result_status="succeeded",
            reason="hardware_transport_succeeded",
            output={
                "translation": translation,
                "dispatches": [dispatch],
                "after_state": after_state,
                "latest_target_request": str(_arm_request_path(shared_root)),
                "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                "request_context": request_context,
            },
        )

    execution = _execute_arm_translation(
        base_url=base_url,
        translation=translation,
        timeout_seconds=timeout_seconds,
        request_context=request_context,
    )
    if not bool(execution.get("succeeded")):
        return _build_result(
            request,
            result_status=str(execution.get("result_status") or "failed"),
            reason=str(execution.get("reason") or "transport_dispatch_failed"),
            output={
                "translation": translation,
                "dispatches": _json_list(execution.get("dispatches")),
                "after_state": _json_dict(execution.get("after_state")),
                "latest_target_request": str(_arm_request_path(shared_root)),
                "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
                "request_context": request_context,
            },
        )
    return _build_result(
        request,
        result_status="succeeded",
        reason="hardware_transport_succeeded",
        output={
            "translation": translation,
            "dispatches": _json_list(execution.get("dispatches")),
            "after_state": _json_dict(execution.get("after_state")),
            "latest_target_request": str(_arm_request_path(shared_root)),
            "transport": {"base_url": base_url, "timeout_seconds": timeout_seconds},
            "request_context": request_context,
        },
    )


def submit_execution_request(
    request: dict[str, Any],
    *,
    shared_root: Path,
    expected_target: str,
    execution_mode: str,
    status_surface: dict[str, Any] | None = None,
    hardware_transport_enabled: bool = False,
) -> dict[str, Any]:
    shared_root = shared_root.expanduser().resolve()
    state = load_execution_lane_state(shared_root)
    processed_requests = _json_dict(state.get("processed_requests"))
    superseded_request_ids = set(str(item).strip() for item in _json_list(state.get("superseded_request_ids")) if str(item).strip())
    request_id = str(request.get("request_id") or "").strip()
    supersedes_request_id = str(request.get("supersedes_request_id") or "").strip()

    _append_jsonl(
        _request_log_path(shared_root),
        {
            "received_at": utc_now(),
            "request": request,
        },
    )

    if supersedes_request_id:
        superseded_request_ids.add(supersedes_request_id)

    if request_id in superseded_request_ids:
        state["superseded_request_ids"] = sorted(superseded_request_ids)
        _save_execution_lane_state(shared_root, state)
        return {
            "request_id": request_id,
            "disposition": "ignored_superseded",
            "accepted": False,
            "events_emitted": 0,
            "ack": None,
            "result": None,
        }

    if request_id and request_id in processed_requests:
        previous = _json_dict(processed_requests.get(request_id))
        state["superseded_request_ids"] = sorted(superseded_request_ids)
        _save_execution_lane_state(shared_root, state)
        return {
            "request_id": request_id,
            "disposition": "duplicate",
            "accepted": bool(_json_dict(previous.get("ack")).get("ack_status") == "accepted"),
            "events_emitted": 0,
            "ack": previous.get("ack"),
            "result": previous.get("result"),
        }

    validation_errors = _request_validation_errors(request, expected_target=expected_target)
    if validation_errors:
        reason = validation_errors[0]
        ack = _build_ack(request, ack_status="rejected", reason=reason, expected_target=expected_target)
        _append_jsonl(_event_log_path(shared_root), ack)
        processed_requests[request_id] = _record_summary(request, ack, None)
        state["processed_requests"] = processed_requests
        state["superseded_request_ids"] = sorted(superseded_request_ids)
        _save_execution_lane_state(shared_root, state)
        return {
            "request_id": request_id,
            "disposition": "rejected",
            "accepted": False,
            "events_emitted": 1,
            "ack": ack,
            "result": None,
        }

    ack = _build_ack(request, ack_status="accepted", reason="request_accepted", expected_target=expected_target)
    _append_jsonl(_event_log_path(shared_root), ack)

    if execution_mode == "synthetic":
        result = _execute_synthetic_request(request, shared_root=shared_root)
    else:
        result = _execute_mim_arm_request(
            request,
            shared_root=shared_root,
            status_surface=status_surface,
            hardware_transport_enabled=hardware_transport_enabled,
        )
    _append_jsonl(_event_log_path(shared_root), result)

    processed_requests[request_id] = _record_summary(request, ack, result)
    state["processed_requests"] = processed_requests
    state["superseded_request_ids"] = sorted(superseded_request_ids)
    _save_execution_lane_state(shared_root, state)
    return {
        "request_id": request_id,
        "disposition": "executed",
        "accepted": True,
        "events_emitted": 2,
        "ack": ack,
        "result": result,
    }