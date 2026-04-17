#!/usr/bin/env python3
"""Generate real host-state truth for MIM_ARM on the arm host itself."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_local(command: list[str], timeout: int = 8) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _payload_timestamp(payload: dict[str, Any]) -> datetime:
    bridge_runtime = payload.get("bridge_runtime") if isinstance(payload.get("bridge_runtime"), dict) else {}
    current_processing = bridge_runtime.get("current_processing") if isinstance(bridge_runtime.get("current_processing"), dict) else {}
    last_command_result = payload.get("last_command_result") if isinstance(payload.get("last_command_result"), dict) else {}
    for candidate in (
        payload.get("generated_at"),
        payload.get("emitted_at"),
        payload.get("observed_at"),
        payload.get("host_timestamp"),
        current_processing.get("generated_at"),
        current_processing.get("emitted_at"),
        last_command_result.get("host_completed_timestamp"),
        last_command_result.get("host_received_timestamp"),
        last_command_result.get("last_command_sent_at"),
    ):
        parsed = _parse_timestamp(candidate)
        if parsed != datetime.min.replace(tzinfo=timezone.utc):
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _find_best_json(candidates: list[Path]) -> tuple[Path | None, dict[str, Any]]:
    best_path: Path | None = None
    best_payload: dict[str, Any] = {}
    best_rank = (datetime.min.replace(tzinfo=timezone.utc), -10**9)
    for index, path in enumerate(candidates):
        payload = _read_json(path)
        if not payload:
            continue
        rank = (_payload_timestamp(payload), -index)
        if rank > best_rank:
            best_rank = rank
            best_path = path
            best_payload = payload
    if best_path is not None:
        return best_path, best_payload
    for path in candidates:
        payload = _read_json(path)
        if payload:
            return path, payload
    return None, {}


def _find_first_json(candidates: list[Path]) -> tuple[Path | None, dict[str, Any]]:
    return _find_best_json(candidates)


def _unique_paths(candidates: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _build_source_candidates(shared_root: Path, extra_candidates: list[Path]) -> list[Path]:
    # Prefer live request/ack/result surfaces because they carry the active
    # task attribution even when generic host status files do not.
    preferred_candidates = [
        shared_root / "TOD_MIM_TASK_ACK.latest.json",
        shared_root / "MIM_TOD_TASK_REQUEST.latest.json",
        shared_root / "TOD_MIM_TASK_RESULT.latest.json",
        shared_root / "TOD_AUTHORITY_SUMMARY.latest.json",
        shared_root / "mim_arm_execution.latest.json",
        shared_root / "mim_arm_controller_state.latest.json",
        shared_root / "mim_arm_ui_state.latest.json",
        shared_root / "mim_arm_status.latest.json",
    ]
    return _unique_paths([*extra_candidates, *preferred_candidates])


def _to_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "online", "connected", "ok", "clear", "ready"}:
        return True
    if text in {"0", "false", "no", "off", "offline", "disconnected", "error", "pressed", "blocked"}:
        return False
    return None


def _uptime_seconds() -> float | None:
    try:
        return round(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]), 3)
    except Exception:
        return None


def _read_local_json_url(url: str, timeout: int = 3) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_pose(value: object) -> object:
    if isinstance(value, (list, dict)):
        return value
    if value in {None, ""}:
        return "unknown"
    return str(value).strip() or "unknown"


def _text(value: object) -> str:
    return str(value or "").strip()


def _nested_dict(payload: dict[str, Any], *path: str) -> dict[str, Any]:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_text(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _resolve_command_attribution(
    source_payload: dict[str, Any],
    arm_state_payload: dict[str, Any],
    *,
    last_command_result: dict[str, Any],
    serial_state: dict[str, Any],
) -> dict[str, Any]:
    bridge_processing = _nested_dict(source_payload, "bridge_runtime", "current_processing")
    source_request = _nested_dict(source_payload, "request")
    source_command_evidence = _nested_dict(source_payload, "command_evidence")

    request_id = _first_text(
        source_command_evidence.get("request_id"),
        source_payload.get("request_id"),
        source_request.get("request_id"),
        bridge_processing.get("request_id"),
        source_command_evidence.get("task_id"),
        source_payload.get("task_id"),
        source_request.get("task_id"),
        bridge_processing.get("task_id"),
        last_command_result.get("request_id"),
        serial_state.get("request_id"),
        serial_state.get("last_request_id"),
        arm_state_payload.get("last_request_id"),
    )
    task_id = _first_text(
        source_command_evidence.get("task_id"),
        source_payload.get("task_id"),
        source_payload.get("task"),
        source_request.get("task_id"),
        bridge_processing.get("task_id"),
        source_command_evidence.get("request_id"),
        source_payload.get("request_id"),
        source_request.get("request_id"),
        bridge_processing.get("request_id"),
        last_command_result.get("task_id"),
        serial_state.get("task_id"),
        serial_state.get("last_task_id"),
        arm_state_payload.get("last_task_id"),
        request_id,
    )
    correlation_id = _first_text(
        source_command_evidence.get("correlation_id"),
        source_payload.get("correlation_id"),
        source_request.get("correlation_id"),
        bridge_processing.get("correlation_id"),
        last_command_result.get("correlation_id"),
        serial_state.get("correlation_id"),
        serial_state.get("last_correlation_id"),
        arm_state_payload.get("last_correlation_id"),
    )
    lane = _first_text(
        source_command_evidence.get("lane"),
        source_payload.get("lane"),
        source_payload.get("active_lane"),
        last_command_result.get("lane"),
        serial_state.get("lane"),
        serial_state.get("last_command_lane"),
        arm_state_payload.get("last_command_lane"),
    )

    evidence = {
        "request_id": request_id,
        "task_id": task_id,
        "correlation_id": correlation_id,
        "lane": lane,
    }
    source = ""
    if request_id or task_id or correlation_id:
        if any(_text(source_command_evidence.get(key)) for key in ("request_id", "task_id", "correlation_id")):
            source = "source_payload.command_evidence"
        elif any(_text(source_payload.get(key)) for key in ("request_id", "task_id", "task", "correlation_id")):
            source = "source_payload"
        elif any(_text(source_request.get(key)) for key in ("request_id", "task_id", "correlation_id")):
            source = "source_payload.request"
        elif any(_text(bridge_processing.get(key)) for key in ("request_id", "task_id", "correlation_id")):
            source = "bridge_runtime.current_processing"
        elif any(_text(last_command_result.get(key)) for key in ("request_id", "task_id", "correlation_id")):
            source = "last_command_result"
        elif any(_text(serial_state.get(key)) for key in ("request_id", "task_id", "correlation_id", "last_request_id", "last_task_id", "last_correlation_id")):
            source = "serial"
        elif any(_text(arm_state_payload.get(key)) for key in ("last_request_id", "last_task_id", "last_correlation_id")):
            source = "arm_state"
    if source:
        evidence["attribution_source"] = source
    return {key: value for key, value in evidence.items() if value not in {None, ""}}


def _command_evidence(source_payload: dict[str, Any], arm_state_payload: dict[str, Any]) -> dict[str, Any]:
    last_command_result = source_payload.get("last_command_result") if isinstance(source_payload.get("last_command_result"), dict) else {}
    if not last_command_result and isinstance(arm_state_payload.get("last_command_result"), dict):
        last_command_result = arm_state_payload.get("last_command_result")
    serial_state = arm_state_payload.get("serial") if isinstance(arm_state_payload.get("serial"), dict) else {}
    attribution = _resolve_command_attribution(
        source_payload,
        arm_state_payload,
        last_command_result=last_command_result,
        serial_state=serial_state,
    )
    evidence = {
        "commands_total": last_command_result.get("commands_total"),
        "acks_total": last_command_result.get("acks_total"),
        "last_command_sent": last_command_result.get("last_command_sent") or serial_state.get("last_command_sent"),
        "last_command_sent_at": last_command_result.get("last_command_sent_at") or arm_state_payload.get("last_command_sent_at"),
        "last_serial_event": serial_state.get("last_serial_event"),
        "serial_command_count": serial_state.get("serial_command_count"),
        "serial_ack_count": serial_state.get("serial_ack_count"),
        "request_id": attribution.get("request_id"),
        "task_id": attribution.get("task_id"),
        "correlation_id": attribution.get("correlation_id"),
        "lane": attribution.get("lane"),
        "attribution_source": attribution.get("attribution_source"),
    }
    return {key: value for key, value in evidence.items() if value not in {None, ""}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", default="runtime/shared")
    parser.add_argument("--output", default="mim_arm_host_state.latest.json")
    parser.add_argument("--process-match", default="mim_arm|arm_ui|uvicorn|python.*app")
    parser.add_argument(
        "--controller-glob",
        action="append",
        default=["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"],
    )
    parser.add_argument(
        "--camera-glob",
        action="append",
        default=["/dev/video*"],
    )
    parser.add_argument(
        "--input-json",
        action="append",
        default=[],
        help="Candidate JSON files that may already contain pose/servo/error state.",
    )
    parser.add_argument(
        "--arm-url",
        default="",
        help="Override the arm_state URL (e.g. http://192.168.1.90:5000/arm_state). "
        "When empty, defaults to http://127.0.0.1:5000/arm_state (on-device execution).",
    )
    parser.add_argument(
        "--sim-estop-ok",
        action="store_true",
        default=False,
        help="When the arm runtime is sim and estop is unsupported, treat estop as "
        "implicitly clear (no physical motion occurs in sim mode).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shared_root = Path(args.shared_root).expanduser().resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = shared_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    process_info = run_local(["sh", "-lc", f"ps -ef | grep -E '{args.process_match}' | grep -v grep || true"])
    controller_info = run_local(["sh", "-lc", f"ls -1 {' '.join(args.controller_glob)} 2>/dev/null || true"])
    camera_info = run_local(["sh", "-lc", f"ls -1 {' '.join(args.camera_glob)} 2>/dev/null || true"])

    extra_candidates = [Path(item).expanduser() for item in args.input_json]
    source_path, source_payload = _find_first_json(_build_source_candidates(shared_root, extra_candidates))
    arm_state_url = args.arm_url.strip() if args.arm_url else "http://127.0.0.1:5000/arm_state"
    arm_state_payload = _read_local_json_url(arm_state_url)
    arm_state_estop = arm_state_payload.get("estop") if isinstance(arm_state_payload.get("estop"), dict) else {}

    pose = _normalize_pose(
        source_payload.get("current_pose")
        or source_payload.get("pose")
        or source_payload.get("reported_pose")
        or arm_state_payload.get("current_pose")
        or "unknown"
    )
    servo_states = source_payload.get("servo_states") if isinstance(source_payload.get("servo_states"), dict) else {}
    if not servo_states and isinstance(arm_state_payload.get("servo_states"), dict):
        servo_states = arm_state_payload.get("servo_states")
    last_command_result = source_payload.get("last_command_result") if isinstance(source_payload.get("last_command_result"), dict) else {}
    if not last_command_result and isinstance(arm_state_payload.get("last_command_result"), dict):
        last_command_result = arm_state_payload.get("last_command_result")
    serial_state = arm_state_payload.get("serial") if isinstance(arm_state_payload.get("serial"), dict) else {}
    command_attribution = _resolve_command_attribution(
        source_payload,
        arm_state_payload,
        last_command_result=last_command_result,
        serial_state=serial_state,
    )
    if command_attribution:
        enriched_last_command_result = dict(last_command_result)
        for field_name in ("request_id", "task_id", "correlation_id", "lane"):
            if not _text(enriched_last_command_result.get(field_name)) and _text(command_attribution.get(field_name)):
                enriched_last_command_result[field_name] = command_attribution[field_name]
        last_command_result = enriched_last_command_result
    last_command_status = str(
        source_payload.get("last_command_status")
        or last_command_result.get("status")
        or "unknown"
    ).strip() or "unknown"
    last_error = source_payload.get("last_error")
    if last_error in {None, ""}:
        last_error = arm_state_payload.get("last_error")
    mode = str(
        source_payload.get("mode")
        or source_payload.get("current_mode")
        or source_payload.get("active_mode")
        or arm_state_payload.get("mode")
        or arm_state_payload.get("runtime")
        or "unknown"
    ).strip() or "unknown"
    estop_ok = _to_bool(source_payload.get("estop_ok"))
    estop_status = str(source_payload.get("estop_status") or "unknown").strip() or "unknown"
    estop_supported = _to_bool(arm_state_estop.get("supported"))
    estop_active = _to_bool(arm_state_estop.get("active"))
    if estop_supported is True and estop_active is not None:
        estop_ok = not estop_active
        estop_status = "clear" if estop_ok else "engaged"
    elif estop_supported is False:
        estop_ok = None
        estop_status = "unsupported"
        # In sim mode, no physical movement occurs so a software e-stop gate is not
        # required for safe dispatch.  Honour the --sim-estop-ok flag to acknowledge
        # this explicitly rather than leaving the gate permanently blocked.
        arm_runtime = str(arm_state_payload.get("runtime") or "").strip().lower()
        if args.sim_estop_ok and arm_runtime == "sim":
            estop_ok = True
            estop_status = "sim_clear"

    ui_process_alive = _to_bool(arm_state_payload.get("app_alive"))
    if ui_process_alive is None:
        ui_process_alive = bool(process_info.get("stdout"))

    serial_state = arm_state_payload.get("serial") if isinstance(arm_state_payload.get("serial"), dict) else {}
    serial_ready_from_arm = _to_bool(serial_state.get("serial_ready"))
    if serial_ready_from_arm is None and str(serial_state.get("status") or "").strip().lower() == "ok":
        serial_ready_from_arm = True
    controller_connected = serial_ready_from_arm if serial_ready_from_arm is not None else bool(controller_info.get("stdout"))

    camera_state = arm_state_payload.get("camera") if isinstance(arm_state_payload.get("camera"), dict) else {}
    camera_online_from_arm = None
    if camera_state:
        camera_online_from_arm = str(camera_state.get("status") or "").strip().lower() == "ok"
    camera_online = camera_online_from_arm if camera_online_from_arm is not None else bool(camera_info.get("stdout"))

    arm_status_text = str(arm_state_payload.get("status") or "").strip().lower()
    arm_online = arm_status_text == "ok" or ui_process_alive or controller_connected

    payload = {
        "host_timestamp": utc_now(),
        "source_host": socket.gethostname(),
        "uptime": {"seconds": _uptime_seconds()},
        "ui_process_alive": ui_process_alive,
        "controller_connected": controller_connected,
        "arm_online": arm_online,
        "app_alive": ui_process_alive,
        "current_pose": pose,
        "servo_states": servo_states,
        "camera_online": camera_online,
        "camera_status": "online" if camera_online else "offline",
        "estop_ok": estop_ok,
        "estop_supported": estop_supported,
        "estop_state_explicit": estop_ok is not None or estop_supported is False,
        "estop_status": estop_status,
        "mode": mode,
        "serial_ready": controller_connected,
        "last_command_status": last_command_status,
        "last_command_result": last_command_result,
        "command_evidence": _command_evidence(source_payload, arm_state_payload),
        "last_request_id": command_attribution.get("request_id"),
        "last_task_id": command_attribution.get("task_id"),
        "last_correlation_id": command_attribution.get("correlation_id"),
        "last_command_lane": command_attribution.get("lane"),
        "last_error": last_error,
        "arm_status": "online" if arm_online else "offline",
        "source_payload_path": str(source_path) if source_path else "",
        "arm_state_probe": {
            "url": arm_state_url,
            "available": bool(arm_state_payload),
            "estop": arm_state_estop,
        },
        "process_probe": process_info,
        "controller_probe": controller_info,
        "camera_probe": camera_info,
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())