from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.execution_lane_service import (
    TARGET_SYNTHETIC_ARM,
    build_execution_target_profile,
    read_execution_events,
    read_execution_requests,
    submit_execution_request,
    utc_now,
)


DOC_PATHS = {
    "scenario_catalog": PROJECT_ROOT / "docs" / "tod-mim-execution-lane-scenarios-2026-04-01.md",
}
SCENARIO_IDS = [
    "request_accepted_ack_result",
    "duplicate_request_idempotent",
    "superseded_request_ignored",
    "stale_or_wrong_target_rejected",
    "timeout_failure_surfaced",
    "expanded_command_vocabulary_supported",
    "invalid_command_args_rejected",
    "move_relative_lineage_supported",
]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _scenario_roots(base_root: Path, scenario_id: str) -> tuple[Path, Path]:
    scenario_root = base_root / scenario_id
    shared_root = scenario_root / "shared"
    shared_root.mkdir(parents=True, exist_ok=True)
    return scenario_root, shared_root


def _expires_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _request(
    *,
    request_id: str,
    target: str = TARGET_SYNTHETIC_ARM,
    sequence: int = 1,
    command_name: str = "move_to",
    command_args: dict[str, Any] | None = None,
    supersedes_request_id: str = "",
    expires_at: str | None = None,
    simulation_outcome: str = "succeeded",
) -> dict[str, Any]:
    return {
        "version": "1.0",
        "request_id": request_id,
        "target": target,
        "sequence": sequence,
        "issued_at": utc_now(),
        "expires_at": expires_at or _expires_in(300),
        "supersedes_request_id": supersedes_request_id,
        "command": {
            "name": command_name,
            "args": command_args or {"x": 0.1, "y": 0.2, "z": 0.3},
        },
        "simulation_outcome": simulation_outcome,
    }


def _result_payload(
    *,
    scenario_id: str,
    scenario_root: Path,
    passed: bool,
    checks: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "scenario_root": str(scenario_root),
        "passed": passed,
        "checks": checks,
        "error": error,
    }


def _run_request_accepted_ack_result(base_root: Path) -> dict[str, Any]:
    scenario_id = "request_accepted_ack_result"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    request = _request(request_id="synthetic-request-001")
    submission = submit_execution_request(
        request,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    events = read_execution_events(shared_root)
    checks = {
        "accepted": submission["accepted"],
        "ack_status": submission["ack"]["ack_status"],
        "result_status": submission["result"]["result_status"],
        "event_count": len(events),
        "target_profile": build_execution_target_profile(target=TARGET_SYNTHETIC_ARM, shared_root=shared_root),
    }
    assert checks["accepted"] is True
    assert checks["ack_status"] == "accepted"
    assert checks["result_status"] == "succeeded"
    assert checks["event_count"] == 2
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_duplicate_request_idempotent(base_root: Path) -> dict[str, Any]:
    scenario_id = "duplicate_request_idempotent"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    request = _request(request_id="synthetic-request-duplicate")
    first = submit_execution_request(
        request,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    second = submit_execution_request(
        request,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    events = [item for item in read_execution_events(shared_root) if item.get("request_id") == request["request_id"]]
    checks = {
        "first_disposition": first["disposition"],
        "second_disposition": second["disposition"],
        "ack_count": sum(1 for item in events if item.get("event_type") == "ack"),
        "result_count": sum(1 for item in events if item.get("event_type") == "result"),
        "request_receipts": len(read_execution_requests(shared_root)),
    }
    assert checks["first_disposition"] == "executed"
    assert checks["second_disposition"] == "duplicate"
    assert checks["ack_count"] == 1
    assert checks["result_count"] == 1
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_superseded_request_ignored(base_root: Path) -> dict[str, Any]:
    scenario_id = "superseded_request_ignored"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    replacement = _request(
        request_id="synthetic-request-reissue",
        sequence=2,
        supersedes_request_id="synthetic-request-original",
    )
    original = _request(request_id="synthetic-request-original", sequence=1)
    replacement_submission = submit_execution_request(
        replacement,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    original_submission = submit_execution_request(
        original,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    events = read_execution_events(shared_root)
    checks = {
        "replacement_disposition": replacement_submission["disposition"],
        "original_disposition": original_submission["disposition"],
        "replacement_event_count": sum(1 for item in events if item.get("request_id") == replacement["request_id"]),
        "original_event_count": sum(1 for item in events if item.get("request_id") == original["request_id"]),
    }
    assert checks["replacement_disposition"] == "executed"
    assert checks["original_disposition"] == "ignored_superseded"
    assert checks["replacement_event_count"] == 2
    assert checks["original_event_count"] == 0
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_stale_or_wrong_target_rejected(base_root: Path) -> dict[str, Any]:
    scenario_id = "stale_or_wrong_target_rejected"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    stale = _request(request_id="synthetic-request-stale", expires_at=_expires_in(-60))
    wrong_target = _request(request_id="synthetic-request-wrong-target", target="mim_arm")
    stale_submission = submit_execution_request(
        stale,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    wrong_target_submission = submit_execution_request(
        wrong_target,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    events = read_execution_events(shared_root)
    checks = {
        "stale_disposition": stale_submission["disposition"],
        "stale_reason": stale_submission["ack"]["reason"],
        "wrong_target_disposition": wrong_target_submission["disposition"],
        "wrong_target_reason": wrong_target_submission["ack"]["reason"],
        "rejected_ack_count": sum(1 for item in events if item.get("ack_status") == "rejected"),
    }
    assert checks["stale_disposition"] == "rejected"
    assert checks["stale_reason"] == "stale_request"
    assert checks["wrong_target_disposition"] == "rejected"
    assert checks["wrong_target_reason"] == "wrong_target"
    assert checks["rejected_ack_count"] == 2
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_timeout_failure_surfaced(base_root: Path) -> dict[str, Any]:
    scenario_id = "timeout_failure_surfaced"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    timed_out = _request(request_id="synthetic-request-timeout", simulation_outcome="timed_out")
    failed = _request(request_id="synthetic-request-failed", simulation_outcome="failed", command_name="close_gripper", command_args={})
    timeout_submission = submit_execution_request(
        timed_out,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    failed_submission = submit_execution_request(
        failed,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    checks = {
        "timeout_ack_status": timeout_submission["ack"]["ack_status"],
        "timeout_result_status": timeout_submission["result"]["result_status"],
        "failure_ack_status": failed_submission["ack"]["ack_status"],
        "failure_result_status": failed_submission["result"]["result_status"],
        "failure_reason": failed_submission["result"]["reason"],
    }
    assert checks["timeout_ack_status"] == "accepted"
    assert checks["timeout_result_status"] == "timed_out"
    assert checks["failure_ack_status"] == "accepted"
    assert checks["failure_result_status"] == "failed"
    assert checks["failure_reason"] == "synthetic_execution_failure"
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_expanded_command_vocabulary_supported(base_root: Path) -> dict[str, Any]:
    scenario_id = "expanded_command_vocabulary_supported"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    requests = [
        _request(request_id="synthetic-request-home", command_name="move_home", command_args={}),
        _request(
            request_id="synthetic-request-relative",
            command_name="move_relative",
            command_args={"dx": 5, "dy": -10, "dz": 15},
        ),
        _request(
            request_id="synthetic-request-compound",
            command_name="move_relative_then_set_gripper",
            command_args={"dx": 5, "dy": -10, "dz": 15, "position": 40},
        ),
        _request(
            request_id="synthetic-request-pick",
            command_name="pick_at",
            command_args={"x": 110, "y": 55, "z": 45},
        ),
        _request(
            request_id="synthetic-request-pick-and-place",
            command_name="pick_and_place",
            command_args={"pick_x": 110, "pick_y": 55, "pick_z": 45, "place_x": 130, "place_y": 60, "place_z": 50},
        ),
        _request(
            request_id="synthetic-request-place",
            command_name="place_at",
            command_args={"x": 110, "y": 55, "z": 45},
        ),
        _request(request_id="synthetic-request-gripper", command_name="set_gripper", command_args={"position": 40}),
        _request(request_id="synthetic-request-speed", command_name="set_speed", command_args={"level": "slow"}),
        _request(request_id="synthetic-request-stop", command_name="stop", command_args={}),
    ]
    submissions = [
        submit_execution_request(
            request,
            shared_root=shared_root,
            expected_target=TARGET_SYNTHETIC_ARM,
            execution_mode="synthetic",
        )
        for request in requests
    ]
    profile = build_execution_target_profile(target=TARGET_SYNTHETIC_ARM, shared_root=shared_root)
    checks = {
        "result_statuses": [submission["result"]["result_status"] for submission in submissions],
        "allowed_commands": profile["allowed_commands"],
        "move_relative_schema": profile["command_capabilities"]["move_relative"]["parameter_schema"],
        "compound_schema": profile["command_capabilities"]["move_relative_then_set_gripper"]["parameter_schema"],
        "pick_and_place_schema": profile["command_capabilities"]["pick_and_place"]["parameter_schema"],
        "pick_at_schema": profile["command_capabilities"]["pick_at"]["parameter_schema"],
        "place_at_schema": profile["command_capabilities"]["place_at"]["parameter_schema"],
        "set_gripper_schema": profile["command_capabilities"]["set_gripper"]["parameter_schema"],
        "set_speed_available": profile["command_capabilities"]["set_speed"]["available"],
        "current_execution_state": profile["current_execution_state"],
    }
    assert checks["result_statuses"] == ["succeeded", "succeeded", "succeeded", "succeeded", "succeeded", "succeeded", "succeeded", "succeeded", "succeeded"]
    assert "move_home" in checks["allowed_commands"]
    assert "move_relative" in checks["allowed_commands"]
    assert "move_relative_then_set_gripper" in checks["allowed_commands"]
    assert "pick_and_place" in checks["allowed_commands"]
    assert "pick_at" in checks["allowed_commands"]
    assert "place_at" in checks["allowed_commands"]
    assert "set_gripper" in checks["allowed_commands"]
    assert "set_speed" in checks["allowed_commands"]
    assert "stop" in checks["allowed_commands"]
    assert checks["move_relative_schema"]["required"] == ["dx", "dy", "dz"]
    assert checks["compound_schema"]["required"] == ["dx", "dy", "dz", "position"]
    assert checks["pick_and_place_schema"]["required"] == ["pick_x", "pick_y", "pick_z", "place_x", "place_y", "place_z"]
    assert checks["pick_at_schema"]["required"] == ["x", "y", "z"]
    assert checks["place_at_schema"]["required"] == ["x", "y", "z"]
    assert checks["set_gripper_schema"]["properties"]["position"]["maximum"] == 100
    assert checks["set_speed_available"] is True
    assert checks["current_execution_state"]["processed_request_count"] == 9
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_invalid_command_args_rejected(base_root: Path) -> dict[str, Any]:
    scenario_id = "invalid_command_args_rejected"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    invalid_gripper = _request(
        request_id="synthetic-request-invalid-gripper",
        command_name="set_gripper",
        command_args={"position": 140},
    )
    invalid_speed = _request(
        request_id="synthetic-request-invalid-speed",
        command_name="set_speed",
        command_args={"level": "warp"},
    )
    invalid_gripper_submission = submit_execution_request(
        invalid_gripper,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    invalid_speed_submission = submit_execution_request(
        invalid_speed,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    checks = {
        "gripper_disposition": invalid_gripper_submission["disposition"],
        "gripper_reason": invalid_gripper_submission["ack"]["reason"],
        "speed_disposition": invalid_speed_submission["disposition"],
        "speed_reason": invalid_speed_submission["ack"]["reason"],
    }
    assert checks["gripper_disposition"] == "rejected"
    assert checks["gripper_reason"] == "invalid_command_args:position_out_of_range"
    assert checks["speed_disposition"] == "rejected"
    assert checks["speed_reason"] == "invalid_command_args:level_unsupported"
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


def _run_move_relative_lineage_supported(base_root: Path) -> dict[str, Any]:
    scenario_id = "move_relative_lineage_supported"
    scenario_root, shared_root = _scenario_roots(base_root, scenario_id)
    request = _request(
        request_id="synthetic-relative-lineage",
        command_name="move_relative",
        command_args={"dx": 180, "dy": -180, "dz": 500},
    )
    first = submit_execution_request(
        request,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    duplicate = submit_execution_request(
        request,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    replacement = _request(
        request_id="synthetic-relative-lineage-reissue",
        sequence=2,
        supersedes_request_id="synthetic-relative-lineage-superseded",
        command_name="move_relative",
        command_args={"dx": 1, "dy": 1, "dz": 1},
    )
    superseded = _request(
        request_id="synthetic-relative-lineage-superseded",
        sequence=1,
        command_name="move_relative",
        command_args={"dx": 2, "dy": 2, "dz": 2},
    )
    replacement_submission = submit_execution_request(
        replacement,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    superseded_submission = submit_execution_request(
        superseded,
        shared_root=shared_root,
        expected_target=TARGET_SYNTHETIC_ARM,
        execution_mode="synthetic",
    )
    checks = {
        "first_disposition": first["disposition"],
        "duplicate_disposition": duplicate["disposition"],
        "replacement_disposition": replacement_submission["disposition"],
        "superseded_disposition": superseded_submission["disposition"],
        "duplicate_result_status": duplicate["result"]["result_status"],
    }
    assert checks["first_disposition"] == "executed"
    assert checks["duplicate_disposition"] == "duplicate"
    assert checks["replacement_disposition"] == "executed"
    assert checks["superseded_disposition"] == "ignored_superseded"
    assert checks["duplicate_result_status"] == "succeeded"
    return _result_payload(scenario_id=scenario_id, scenario_root=scenario_root, passed=True, checks=checks)


SCENARIO_RUNNERS = {
    "request_accepted_ack_result": _run_request_accepted_ack_result,
    "duplicate_request_idempotent": _run_duplicate_request_idempotent,
    "superseded_request_ignored": _run_superseded_request_ignored,
    "stale_or_wrong_target_rejected": _run_stale_or_wrong_target_rejected,
    "timeout_failure_surfaced": _run_timeout_failure_surfaced,
    "expanded_command_vocabulary_supported": _run_expanded_command_vocabulary_supported,
    "invalid_command_args_rejected": _run_invalid_command_args_rejected,
    "move_relative_lineage_supported": _run_move_relative_lineage_supported,
}


def run_scenarios(*, scenario: str = "all", synthetic_root: str | None = None) -> dict[str, Any]:
    selected = SCENARIO_IDS if scenario == "all" else [scenario]
    unknown = [item for item in selected if item not in SCENARIO_RUNNERS]
    if unknown:
        raise ValueError(f"Unsupported scenario(s): {', '.join(unknown)}")

    base_root = Path(synthetic_root).resolve() if synthetic_root else Path(tempfile.mkdtemp(prefix="mim-execution-lane-sim-"))
    base_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for scenario_id in selected:
        runner = SCENARIO_RUNNERS[scenario_id]
        try:
            results.append(runner(base_root))
        except Exception as exc:
            results.append(
                _result_payload(
                    scenario_id=scenario_id,
                    scenario_root=base_root / scenario_id,
                    passed=False,
                    checks={},
                    error=str(exc),
                )
            )

    return {
        "generated_at": utc_now(),
        "synthetic_root": str(base_root),
        "passed": all(bool(item.get("passed")) for item in results),
        "scenario_count": len(results),
        "results": results,
        "docs": {name: str(path) for name, path in DOC_PATHS.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run synthetic execution-lane scenarios for TOD-MIM Objective 99.")
    parser.add_argument("--scenario", default="all", help="Scenario id to run or 'all'.")
    parser.add_argument("--synthetic-root", default="", help="Synthetic root directory to use.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    report = run_scenarios(
        scenario=str(args.scenario or "all").strip() or "all",
        synthetic_root=str(args.synthetic_root or "").strip() or None,
    )
    if args.output:
        _write_json(Path(str(args.output)).resolve(), report)
    print(json.dumps(report, indent=2))
    return 0 if bool(report.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())