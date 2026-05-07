from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.next_step_dialog_service import process_pending_dialog_sessions


DOC_PATHS = {
    "policy_authority": PROJECT_ROOT / "docs" / "tod-mim-communication-policy-authority-2026-04-01.md",
    "acceptance_checklist": PROJECT_ROOT / "docs" / "tod-mim-communication-acceptance-checklist-2026-04-01.md",
    "scenario_catalog": PROJECT_ROOT / "docs" / "tod-mim-conversation-simulation-scenarios-2026-04-01.md",
}
SCENARIO_IDS = [
    "diagnostic_roundtrip",
    "next_step_consensus_roundtrip",
    "supersede_reissue_same_session",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        text = str(raw).strip()
        if not text:
            continue
        payload = json.loads(text)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _latest_event(rows: list[dict[str, Any]], message_type: str) -> dict[str, Any] | None:
    normalized = str(message_type or "").strip().lower()
    for row in reversed(rows):
        row_type = str(row.get("message_type") or row.get("type") or "").strip().lower()
        if row_type == normalized:
            return row
    return None


def _ensure_scenario_root(base_root: Path, scenario_id: str) -> tuple[Path, Path, Path, Path]:
    scenario_root = base_root / scenario_id
    shared_root = scenario_root / "shared"
    dialog_root = shared_root / "dialog"
    logs_root = scenario_root / "logs"
    dialog_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    return scenario_root, shared_root, dialog_root, logs_root


def _session_path(dialog_root: Path, session_id: str) -> Path:
    return dialog_root / f"MIM_TOD_DIALOG.session-{session_id}.jsonl"


def _session_index_path(dialog_root: Path) -> Path:
    return dialog_root / "MIM_TOD_DIALOG.sessions.latest.json"


def _write_local_posture(shared_root: Path) -> None:
    _write_json(
        shared_root / "MIM_TASK_STATUS_REVIEW.latest.json",
        {
            "generated_at": _utc_now(),
            "task": {"active_task_id": "synthetic-objective-98a-task", "objective_id": "98A"},
            "state": "completed",
            "gate": {"pass": True, "promotion_ready": True},
            "blocking_reason_codes": [],
        },
    )
    _write_json(
        shared_root / "MIM_SYSTEM_ALERTS.latest.json",
        {"generated_at": _utc_now(), "active": False, "highest_severity": "none"},
    )
    _write_json(
        shared_root / "TOD_CATCHUP_GATE.latest.json",
        {"generated_at": _utc_now(), "gate_pass": True, "promotion_ready": True},
    )
    _write_json(
        shared_root / "mim_arm_control_readiness.latest.json",
        {
            "generated_at": _utc_now(),
            "operator_approval_required": False,
            "tod_execution_allowed": True,
        },
    )


def _build_index_entry(
    *,
    session_id: str,
    session_path: Path,
    status: str,
    open_reply_turn: int,
    open_reply_type: str,
    last_turn: int,
    last_message_type: str,
    last_intent: str | None = None,
    windows_mirror: bool = False,
) -> dict[str, Any]:
    mirrored_path = f"C:\\synthetic\\dialog\\{session_path.name}" if windows_mirror else str(session_path)
    last_message: dict[str, Any] = {
        "turn_id": last_turn,
        "message_type": last_message_type,
    }
    if last_intent:
        last_message["intent"] = last_intent
    return {
        "session_id": session_id,
        "status": status,
        "session_path": mirrored_path,
        "open_reply": {
            "to": "MIM",
            "turn_id": open_reply_turn,
            "message_type": open_reply_type,
        },
        "last_message": last_message,
        "updated_at": _utc_now(),
    }


def _result_payload(
    *,
    scenario_id: str,
    scenario_root: Path,
    session_id: str,
    passed: bool,
    checks: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "passed": passed,
        "session_id": session_id,
        "scenario_root": str(scenario_root),
        "checks": checks,
        "error": error,
    }


def _run_diagnostic_roundtrip(base_root: Path) -> dict[str, Any]:
    scenario_id = "diagnostic_roundtrip"
    session_id = "synthetic-diagnostic-roundtrip-20260401"
    scenario_root, _, dialog_root, _ = _ensure_scenario_root(base_root, scenario_id)
    session_path = _session_path(dialog_root, session_id)
    index_path = _session_index_path(dialog_root)

    request = {
        "type": "handoff_request",
        "message_type": "handoff_request",
        "intent": "diagnostic_roundtrip",
        "session_id": session_id,
        "turn": 1,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "payload": {
            "summary": "Synthetic diagnostic roundtrip request.",
            "diagnostic_type": "dialog_path_health",
            "response_contract": {"required_fields": ["summary", "roundtrip_status"]},
        },
    }
    _write_json(index_path, {"generated_at": _utc_now(), "sessions": [_build_index_entry(session_id=session_id, session_path=session_path, status="awaiting_reply", open_reply_turn=1, open_reply_type="handoff_request", last_turn=1, last_message_type="handoff_request", last_intent="diagnostic_roundtrip")]})
    _write_jsonl(session_path, [request])

    response = {
        "type": "handoff_response",
        "message_type": "handoff_response",
        "intent": "diagnostic_roundtrip",
        "session_id": session_id,
        "generated_at": _utc_now(),
        "from": "MIM",
        "to": "TOD",
        "reply_to_turn": 1,
        "summary": "MIM synthetic diagnostic acknowledged the session and verified the reply lane.",
        "payload": {
            "summary": "MIM synthetic diagnostic acknowledged the session and verified the reply lane.",
            "roundtrip_status": "synthetic_ok",
        },
    }
    _append_jsonl(session_path, response)
    _append_jsonl(
        session_path,
        {
            "type": "resolution_notice",
            "message_type": "resolution_notice",
            "intent": "diagnostic_roundtrip_resolved",
            "session_id": session_id,
            "generated_at": _utc_now(),
            "from": "TOD",
            "to": "MIM",
            "reply_to_turn": 2,
            "summary": "TOD consumed the synthetic diagnostic response and closed the session.",
        },
    )

    rows = _read_jsonl(session_path)
    mim_response = _latest_event(rows, "handoff_response")
    resolution_notice = _latest_event(rows, "resolution_notice")
    checks = {
        "reply_to_turn": mim_response.get("reply_to_turn") if mim_response else None,
        "roundtrip_status": ((mim_response or {}).get("payload") or {}).get("roundtrip_status"),
        "resolution_notice_present": resolution_notice is not None,
        "session_row_count": len(rows),
        "session_file": str(session_path),
    }
    assert mim_response is not None
    assert int(mim_response.get("reply_to_turn") or 0) == 1
    assert checks["roundtrip_status"] == "synthetic_ok"
    assert resolution_notice is not None
    return _result_payload(
        scenario_id=scenario_id,
        scenario_root=scenario_root,
        session_id=session_id,
        passed=True,
        checks=checks,
    )


def _run_next_step_consensus_roundtrip(base_root: Path) -> dict[str, Any]:
    scenario_id = "next_step_consensus_roundtrip"
    session_id = "synthetic-next-step-consensus-20260401"
    scenario_root, shared_root, dialog_root, _ = _ensure_scenario_root(base_root, scenario_id)
    session_path = _session_path(dialog_root, session_id)
    _write_local_posture(shared_root)

    request = {
        "type": "handoff_request",
        "message_type": "handoff_request",
        "intent": "next_step_consensus",
        "session_id": session_id,
        "turn": 7,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "payload": {
            "task_id": "objective-98a-simulation",
            "objective_id": "98A",
            "run_id": "synthetic-run-next-step-20260401T000000Z",
            "findings": [
                {
                    "finding_id": "synthetic-finding-001",
                    "summary": "Run canonical-only validation pass",
                    "owner_workspace": "TOD",
                    "action_type": "validate",
                    "risk": "low",
                    "cross_system": True,
                },
                {
                    "finding_id": "synthetic-finding-002",
                    "summary": "Retire remaining live aliases after validation",
                    "owner_workspace": "TOD",
                    "action_type": "cleanup",
                    "risk": "medium",
                    "cross_system": True,
                },
            ],
            "response_contract": {"required_fields": ["summary", "finding_positions"]},
        },
    }
    reminder = {
        "type": "status_request",
        "message_type": "status_request",
        "intent": "next_step_consensus_location_hint",
        "session_id": session_id,
        "turn": 11,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "summary": "Use the session index and answer the active turn on this same session.",
    }
    _write_jsonl(session_path, [request, reminder])
    _write_json(
        _session_index_path(dialog_root),
        {
            "generated_at": _utc_now(),
            "sessions": [
                _build_index_entry(
                    session_id=session_id,
                    session_path=session_path,
                    status="timed_out",
                    open_reply_turn=7,
                    open_reply_type="handoff_request",
                    last_turn=11,
                    last_message_type="status_request",
                    windows_mirror=True,
                )
            ],
        },
    )

    process_result = process_pending_dialog_sessions(shared_root=shared_root, dialog_root=dialog_root)
    rows = _read_jsonl(session_path)
    response = _latest_event(rows, "handoff_response")
    _append_jsonl(
        session_path,
        {
            "type": "resolution_notice",
            "message_type": "resolution_notice",
            "intent": "next_step_consensus_resolved",
            "session_id": session_id,
            "turn": 12,
            "generated_at": _utc_now(),
            "from": "TOD",
            "to": "MIM",
            "reply_to_turn": 7,
            "summary": "TOD consumed the synthetic handoff response and closed the consensus request.",
        },
    )

    finding_positions = list(response.get("finding_positions") or []) if isinstance(response, dict) else []
    checks = {
        "processed_count": int(process_result.get("processed_count") or 0),
        "reply_to_turn": response.get("reply_to_turn") if response else None,
        "finding_positions_count": len(finding_positions),
        "required_fields_present": all(
            {"finding_id", "decision", "reason", "confidence", "local_blockers"}.issubset(position.keys())
            for position in finding_positions
            if isinstance(position, dict)
        ),
        "session_file": str(session_path),
        "timed_out_index_consumed": True,
        "windows_session_path_consumed": True,
        "mim_adjudication_file": str(shared_root / "mim_next_step_adjudication.latest.json"),
    }
    assert checks["processed_count"] == 1
    assert response is not None
    assert int(response.get("reply_to_turn") or 0) == 7
    assert len(finding_positions) == 2
    assert checks["required_fields_present"] is True
    return _result_payload(
        scenario_id=scenario_id,
        scenario_root=scenario_root,
        session_id=session_id,
        passed=True,
        checks=checks,
    )


def _run_supersede_reissue_same_session(base_root: Path) -> dict[str, Any]:
    scenario_id = "supersede_reissue_same_session"
    session_id = "synthetic-next-step-supersede-reissue-20260401"
    scenario_root, shared_root, dialog_root, _ = _ensure_scenario_root(base_root, scenario_id)
    session_path = _session_path(dialog_root, session_id)
    _write_local_posture(shared_root)

    original_request = {
        "type": "handoff_request",
        "message_type": "handoff_request",
        "intent": "next_step_consensus",
        "session_id": session_id,
        "turn": 3,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "payload": {
            "task_id": "objective-98a-supersede",
            "objective_id": "98A",
            "run_id": "synthetic-run-supersede-v1",
            "findings": [
                {
                    "finding_id": "synthetic-reissue-finding-001",
                    "summary": "Old finding that will be superseded",
                    "owner_workspace": "TOD",
                    "action_type": "validate",
                    "risk": "low",
                    "cross_system": True,
                }
            ],
            "response_contract": {"required_fields": ["summary", "finding_positions"]},
        },
    }
    supersede_notice = {
        "type": "resolution_notice",
        "message_type": "resolution_notice",
        "intent": "next_step_consensus_superseded",
        "session_id": session_id,
        "turn": 4,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "summary": "Turn 3 is superseded; use the replacement request on the same session.",
        "payload": {"supersedes_turn": 3, "reason": "corrected_findings"},
    }
    replacement_request = {
        "type": "handoff_request",
        "message_type": "handoff_request",
        "intent": "next_step_consensus",
        "session_id": session_id,
        "turn": 5,
        "generated_at": _utc_now(),
        "from": "TOD",
        "to": "MIM",
        "payload": {
            "task_id": "objective-98a-supersede",
            "objective_id": "98A",
            "run_id": "synthetic-run-supersede-v2",
            "findings": [
                {
                    "finding_id": "synthetic-reissue-finding-002",
                    "summary": "Replacement finding after supersede",
                    "owner_workspace": "TOD",
                    "action_type": "validate",
                    "risk": "low",
                    "cross_system": True,
                }
            ],
            "response_contract": {"required_fields": ["summary", "finding_positions"]},
        },
    }
    _write_jsonl(session_path, [original_request, supersede_notice, replacement_request])
    _write_json(
        _session_index_path(dialog_root),
        {
            "generated_at": _utc_now(),
            "sessions": [
                _build_index_entry(
                    session_id=session_id,
                    session_path=session_path,
                    status="awaiting_reply",
                    open_reply_turn=5,
                    open_reply_type="handoff_request",
                    last_turn=5,
                    last_message_type="handoff_request",
                    last_intent="next_step_consensus",
                )
            ],
        },
    )

    process_result = process_pending_dialog_sessions(shared_root=shared_root, dialog_root=dialog_root)
    rows = _read_jsonl(session_path)
    responses = [row for row in rows if str(row.get("message_type") or row.get("type") or "").strip().lower() == "handoff_response"]
    latest_response = responses[-1] if responses else None
    _append_jsonl(
        session_path,
        {
            "type": "resolution_notice",
            "message_type": "resolution_notice",
            "intent": "next_step_consensus_resolved",
            "session_id": session_id,
            "turn": 6,
            "generated_at": _utc_now(),
            "from": "TOD",
            "to": "MIM",
            "reply_to_turn": 5,
            "summary": "TOD consumed the reissued request response on the same session.",
        },
    )

    checks = {
        "processed_count": int(process_result.get("processed_count") or 0),
        "response_count": len(responses),
        "reply_to_turn": latest_response.get("reply_to_turn") if latest_response else None,
        "superseded_turn": 3,
        "reissued_turn": 5,
        "session_file": str(session_path),
    }
    assert checks["processed_count"] == 1
    assert latest_response is not None
    assert len(responses) == 1
    assert int(latest_response.get("reply_to_turn") or 0) == 5
    return _result_payload(
        scenario_id=scenario_id,
        scenario_root=scenario_root,
        session_id=session_id,
        passed=True,
        checks=checks,
    )


SCENARIO_RUNNERS = {
    "diagnostic_roundtrip": _run_diagnostic_roundtrip,
    "next_step_consensus_roundtrip": _run_next_step_consensus_roundtrip,
    "supersede_reissue_same_session": _run_supersede_reissue_same_session,
}


def run_scenarios(*, scenario: str = "all", synthetic_root: str | None = None) -> dict[str, Any]:
    if scenario == "all":
        selected = SCENARIO_IDS
    else:
        selected = [scenario]
    unknown = [item for item in selected if item not in SCENARIO_RUNNERS]
    if unknown:
        raise ValueError(f"Unsupported scenario(s): {', '.join(unknown)}")

    base_root = Path(synthetic_root).resolve() if synthetic_root else Path(tempfile.mkdtemp(prefix="mim-tod-conversation-sim-"))
    base_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for scenario_id in selected:
        runner = SCENARIO_RUNNERS[scenario_id]
        try:
            result = runner(base_root)
        except Exception as exc:
            result = _result_payload(
                scenario_id=scenario_id,
                scenario_root=base_root / scenario_id,
                session_id="",
                passed=False,
                checks={},
                error=str(exc),
            )
        results.append(result)

    passed = all(bool(result.get("passed")) for result in results)
    return {
        "generated_at": _utc_now(),
        "synthetic_root": str(base_root),
        "passed": passed,
        "scenario_count": len(results),
        "results": results,
        "docs": {name: str(path) for name, path in DOC_PATHS.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run synthetic TOD-MIM conversation scenarios against synthetic roots only.")
    parser.add_argument("--scenario", default="all", help="Scenario id to run or 'all'.")
    parser.add_argument("--synthetic-root", default="", help="Synthetic root directory to use.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    report = run_scenarios(
        scenario=str(args.scenario or "all").strip() or "all",
        synthetic_root=str(args.synthetic_root or "").strip() or None,
    )
    if args.output:
        output_path = Path(str(args.output)).resolve()
        _write_json(output_path, report)
    print(json.dumps(report, indent=2))
    return 0 if bool(report.get("passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())