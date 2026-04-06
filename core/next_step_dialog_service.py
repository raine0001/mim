from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from core.next_step_adjudication_service import DEFAULT_SHARED_ROOT, build_mim_adjudication


DEFAULT_DIALOG_ROOT = DEFAULT_SHARED_ROOT / "dialog"
DIALOG_GLOB = "MIM_TOD_DIALOG.session-*.jsonl"
DIALOG_INDEX_NAME = "MIM_TOD_DIALOG.sessions.latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for raw in lines:
        text = str(raw).strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _extract_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else event


def _event_type(event: dict[str, Any]) -> str:
    payload = _extract_event_payload(event)
    return _normalize_text(
        event.get("message_type")
        or payload.get("message_type")
        or event.get("type")
        or payload.get("type")
    )


def _event_intent(event: dict[str, Any]) -> str:
    payload = _extract_event_payload(event)
    return _normalize_text(event.get("intent") or payload.get("intent"))


def _extract_session_id(event: dict[str, Any], path: Path) -> str:
    payload = _extract_event_payload(event)
    session_id = str(
        payload.get("session_id")
        or event.get("session_id")
        or payload.get("dialog_session_id")
        or event.get("dialog_session_id")
        or path.stem
    ).strip()
    return session_id or path.stem


def _extract_turn_id(event: dict[str, Any]) -> int | None:
    payload = _extract_event_payload(event)
    for key in ("turn", "turn_id", "message_turn", "sequence"):
        value = payload.get(key, event.get(key))
        try:
            turn_id = int(value)
        except Exception:
            continue
        if turn_id > 0:
            return turn_id
    return None


def _extract_execution_identity(event: dict[str, Any]) -> dict[str, str]:
    payload = _extract_event_payload(event)
    task_id = str(payload.get("task_id") or event.get("task_id") or "").strip()
    request_id = str(payload.get("request_id") or event.get("request_id") or "").strip()
    execution_id = str(payload.get("execution_id") or event.get("execution_id") or "").strip()
    id_kind = str(payload.get("id_kind") or event.get("id_kind") or "").strip()
    if not execution_id:
        if id_kind == "bridge_request_id":
            execution_id = request_id or task_id
        elif id_kind == "mim_task_registry_id":
            execution_id = task_id or request_id
        else:
            execution_id = request_id or task_id
    if not id_kind:
        if request_id and execution_id == request_id:
            id_kind = "bridge_request_id"
        elif task_id and execution_id == task_id:
            id_kind = "mim_task_registry_id"
    execution_lane = str(payload.get("execution_lane") or event.get("execution_lane") or "").strip()
    if not execution_lane:
        execution_lane = (
            "tod_bridge_request"
            if id_kind == "bridge_request_id"
            else ("mim_task_registry" if id_kind == "mim_task_registry_id" else "")
        )
    return {
        "task_id": task_id,
        "request_id": request_id,
        "execution_id": execution_id,
        "id_kind": id_kind,
        "execution_lane": execution_lane,
    }


def _extract_raw_finding_positions(event: dict[str, Any], items: list[dict[str, Any]]) -> list[Any]:
    payload = _extract_event_payload(event)
    direct_positions = payload.get("finding_positions")
    if isinstance(direct_positions, list) and direct_positions:
        return direct_positions

    findings = payload.get("candidate_findings") or payload.get("findings") or payload.get("items")
    if isinstance(findings, list):
        positions: list[Any] = []
        for index, finding in enumerate(findings, start=1):
            if isinstance(finding, dict) and finding.get("finding_positions"):
                positions.extend(finding.get("finding_positions") if isinstance(finding.get("finding_positions"), list) else [])
                continue
            if isinstance(finding, dict) and finding.get("position") is not None:
                positions.append(finding.get("position"))
                continue
            positions.append({
                "index": index,
                "step_id": str((finding or {}).get("step_id") or (finding or {}).get("finding_id") or f"finding_{index:03d}"),
            })
        if positions:
            return positions

    return [
        {
            "index": index,
            "step_id": str(item.get("step_id") or f"finding_{index:03d}"),
        }
        for index, item in enumerate(items, start=1)
    ]


def _decision_confidence(item: dict[str, Any]) -> float:
    blockers = item.get("local_blockers") if isinstance(item.get("local_blockers"), list) else []
    if blockers:
        return 0.58
    classification = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    risk = _normalize_text(classification.get("risk") or "medium")
    posture = _normalize_text(item.get("posture") or "proposal_only")
    if posture == "approval_required":
        return 0.74
    if posture == "blocked":
        return 0.52
    if risk == "low":
        return 0.93
    if risk == "medium":
        return 0.84
    return 0.71


def _build_response_finding_positions(event: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = _extract_event_payload(event)
    raw_findings = payload.get("candidate_findings") or payload.get("findings") or payload.get("items")
    normalized_items = {
        str(item.get("step_id") or "").strip(): item
        for item in items
        if isinstance(item, dict) and str(item.get("step_id") or "").strip()
    }

    positions: list[dict[str, Any]] = []
    if isinstance(raw_findings, list) and raw_findings:
        for index, finding in enumerate(raw_findings, start=1):
            if not isinstance(finding, dict):
                continue
            finding_id = str(
                finding.get("finding_id")
                or finding.get("step_id")
                or f"finding_{index:03d}"
            ).strip()
            item = normalized_items.get(finding_id, {})
            positions.append(
                {
                    "finding_id": finding_id,
                    "decision": str(item.get("mim_decision") or "approve").strip() or "approve",
                    "reason": str(item.get("reason") or "MIM reviewed the finding.").strip(),
                    "confidence": _decision_confidence(item) if item else 0.5,
                    "local_blockers": item.get("local_blockers") if isinstance(item.get("local_blockers"), list) else [],
                }
            )
        if positions:
            return positions

    raw_positions = _extract_raw_finding_positions(event, items)
    for index, raw_position in enumerate(raw_positions, start=1):
        if isinstance(raw_position, dict):
            finding_id = str(
                raw_position.get("finding_id")
                or raw_position.get("step_id")
                or f"finding_{index:03d}"
            ).strip()
        else:
            finding_id = str(raw_position or f"finding_{index:03d}").strip()
        item = normalized_items.get(finding_id, {})
        positions.append(
            {
                "finding_id": finding_id,
                "decision": str(item.get("mim_decision") or "approve").strip() or "approve",
                "reason": str(item.get("reason") or "MIM reviewed the finding.").strip(),
                "confidence": _decision_confidence(item) if item else 0.5,
                "local_blockers": item.get("local_blockers") if isinstance(item.get("local_blockers"), list) else [],
            }
        )
    return positions


def _dialog_index_path(dialog_root: Path) -> Path:
    return dialog_root / DIALOG_INDEX_NAME


def _resolve_session_path(dialog_root: Path, session_id: str, session_entry: dict[str, Any] | None = None) -> Path:
    if isinstance(session_entry, dict):
        for key in ("session_path", "log_path", "path", "session_log"):
            raw_value = str(session_entry.get(key) or "").strip()
            if not raw_value:
                continue
            windows_name = PureWindowsPath(raw_value).name
            if windows_name.startswith("MIM_TOD_DIALOG."):
                return dialog_root / windows_name
            candidate = Path(raw_value)
            return candidate if candidate.is_absolute() else dialog_root / candidate
    normalized_session_id = str(session_id or "").strip()
    if normalized_session_id.startswith("session-"):
        filename = f"MIM_TOD_DIALOG.{normalized_session_id}.jsonl"
    else:
        filename = f"MIM_TOD_DIALOG.session-{normalized_session_id}.jsonl"
    return dialog_root / filename


def _iter_index_sessions(dialog_root: Path) -> list[dict[str, Any]]:
    payload = _read_json(_dialog_index_path(dialog_root))
    raw_sessions = payload.get("sessions") or payload.get("items") or payload.get("open_sessions")
    if not isinstance(raw_sessions, list):
        return []
    return [item for item in raw_sessions if isinstance(item, dict)]


def _is_actionable_session(session_entry: dict[str, Any]) -> bool:
    status = _normalize_text(session_entry.get("status"))
    open_reply = session_entry.get("open_reply") if isinstance(session_entry.get("open_reply"), dict) else {}
    recipient = _normalize_text(
        open_reply.get("to")
        or session_entry.get("awaiting_reply_to")
        or session_entry.get("reply_to")
    )
    if recipient != "mim":
        return False

    if status == "awaiting_reply":
        return True

    if status == "timed_out":
        last_message = session_entry.get("last_message") if isinstance(session_entry.get("last_message"), dict) else {}
        last_intent = _normalize_text(last_message.get("intent"))
        last_message_type = _normalize_text(last_message.get("message_type") or last_message.get("type"))
        open_reply_type = _normalize_text(open_reply.get("message_type"))
        if open_reply_type == "handoff_request" and (
            last_intent in {"next_step_consensus_reminder", "next_step_consensus_location_hint"}
            or last_message_type == "status_request"
        ):
            return True

    return False


def list_dialog_sessions(
    shared_root: Path = DEFAULT_SHARED_ROOT,
    *,
    dialog_root: Path | None = None,
    pattern: str = DIALOG_GLOB,
) -> list[Path]:
    active_dialog_root = dialog_root if dialog_root is not None else shared_root / "dialog"
    indexed_sessions = _iter_index_sessions(active_dialog_root)
    if indexed_sessions:
        resolved: list[Path] = []
        seen: set[str] = set()
        for session_entry in indexed_sessions:
            if not _is_actionable_session(session_entry):
                continue
            session_id = str(session_entry.get("session_id") or "").strip()
            if not session_id:
                continue
            session_path = _resolve_session_path(active_dialog_root, session_id, session_entry)
            key = str(session_path)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(session_path)
        return resolved

    return sorted(
        [path for path in active_dialog_root.glob(pattern) if path.is_file()],
        key=lambda path: (path.stat().st_mtime, path.name),
    )


def find_pending_handoff_request(session_path: Path) -> dict[str, Any] | None:
    events = _read_jsonl(session_path)
    last_request_index: int | None = None
    session_id = ""
    for index, event in enumerate(events):
        event_type = _event_type(event)
        intent = _event_intent(event)
        if event_type != "handoff_request":
            continue
        if intent and intent != "next_step_consensus":
            continue
        last_request_index = index
        session_id = _extract_session_id(event, session_path)

    if last_request_index is None:
        return None

    for event in events[last_request_index + 1 :]:
        if _event_type(event) != "handoff_response":
            continue
        if _extract_session_id(event, session_path) == session_id:
            return None

    return events[last_request_index]


def build_next_steps_payload_from_handoff_request(
    request_event: dict[str, Any],
    *,
    session_path: Path,
) -> dict[str, Any] | None:
    payload = _extract_event_payload(request_event)
    raw_items = payload.get("candidate_findings") or payload.get("findings") or payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return None

    session_id = _extract_session_id(request_event, session_path)
    identity = _extract_execution_identity(request_event)
    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        description = str(
            raw_item.get("description")
            or raw_item.get("summary")
            or raw_item.get("title")
            or ""
        ).strip()
        if not description:
            continue
        items.append(
            {
                "step_id": str(
                    raw_item.get("step_id")
                    or raw_item.get("finding_id")
                    or f"finding_{index:03d}"
                ).strip(),
                "description": description,
                "owner_workspace": str(raw_item.get("owner_workspace") or payload.get("owner_workspace") or "TOD").strip(),
                "action_type": str(raw_item.get("action_type") or payload.get("action_type") or "inquire").strip(),
                "risk": str(raw_item.get("risk") or payload.get("risk") or "low").strip(),
                "cross_system": bool(raw_item.get("cross_system", payload.get("cross_system", True))),
                "approval_required": bool(raw_item.get("approval_required", payload.get("approval_required", False))),
                "metadata_json": raw_item.get("metadata_json") if isinstance(raw_item.get("metadata_json"), dict) else {},
            }
        )

    if not items:
        return None

    return {
        "source_workspace": "TOD",
        "run_id": str(payload.get("run_id") or session_id).strip(),
        "task_id": identity["task_id"],
        "request_id": identity["request_id"],
        "execution_id": identity["execution_id"],
        "id_kind": identity["id_kind"],
        "execution_lane": identity["execution_lane"],
        "objective_id": str(payload.get("objective_id") or request_event.get("objective_id") or "").strip(),
        "session_id": session_id,
        "response_contract": payload.get("response_contract") if isinstance(payload.get("response_contract"), dict) else {},
        "items": items,
    }


def publish_mim_adjudication(
    *,
    next_steps_payload: dict[str, Any],
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, Any]:
    mim_adjudication = build_mim_adjudication(next_steps_payload)
    next_steps_path = shared_root / "mim_codex_next_steps.latest.json"
    mim_path = shared_root / "mim_next_step_adjudication.latest.json"
    _write_json(next_steps_path, next_steps_payload)
    _write_json(mim_path, mim_adjudication)
    return {
        "next_steps_path": str(next_steps_path),
        "mim_adjudication_path": str(mim_path),
        "mim_adjudication": mim_adjudication,
    }


def _summarize_adjudication(items: list[dict[str, Any]]) -> str:
    total = len(items)
    posture_counts = {
        "auto_execute_candidate": 0,
        "proposal_only": 0,
        "approval_required": 0,
        "blocked": 0,
    }
    for item in items:
        posture = str(item.get("posture") or "proposal_only").strip()
        if posture in posture_counts:
            posture_counts[posture] += 1
    return (
        f"MIM reviewed {total} finding(s): "
        f"auto_execute_candidate={posture_counts['auto_execute_candidate']}, "
        f"proposal_only={posture_counts['proposal_only']}, "
        f"approval_required={posture_counts['approval_required']}, "
        f"blocked={posture_counts['blocked']}."
    )


def build_handoff_response(
    request_event: dict[str, Any],
    *,
    session_path: Path,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = _extract_event_payload(request_event)
    identity = _extract_execution_identity(request_event)
    session_id = _extract_session_id(request_event, session_path)
    turn_id = _extract_turn_id(request_event)
    next_steps_payload = build_next_steps_payload_from_handoff_request(request_event, session_path=session_path)
    adjudication_result: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    if next_steps_payload is not None:
        adjudication_result = publish_mim_adjudication(next_steps_payload=next_steps_payload, shared_root=shared_root)
        items = adjudication_result.get("mim_adjudication", {}).get("items", [])

    summary = str(
        payload.get("summary")
        or payload.get("issue_summary")
        or payload.get("request_summary")
        or ""
    ).strip()
    if items:
        summary = _summarize_adjudication(items)
    elif not summary:
        summary = "MIM reviewed the active handoff request and acknowledged the live decision session."

    finding_positions = _build_response_finding_positions(request_event, items)
    response_payload = {
        "summary": summary,
        "finding_positions": finding_positions,
    }

    response = {
        "type": "handoff_response",
        "message_type": "handoff_response",
        "intent": "next_step_consensus",
        "session_id": session_id,
        "generated_at": _utc_now(),
        "from": "MIM",
        "to": "TOD",
        "source": "MIM",
        "actor": "mim",
        "task_id": identity["task_id"],
        "request_id": identity["request_id"],
        "execution_id": identity["execution_id"],
        "id_kind": identity["id_kind"],
        "execution_lane": identity["execution_lane"],
        "run_id": str(payload.get("run_id") or request_event.get("run_id") or "").strip(),
        "objective_id": str(payload.get("objective_id") or request_event.get("objective_id") or "").strip(),
        "summary": summary,
        "finding_positions": finding_positions,
        "payload": response_payload,
    }
    if turn_id is not None:
        response["reply_to_turn"] = turn_id
    if adjudication_result is not None:
        response["mim_adjudication_path"] = adjudication_result["mim_adjudication_path"]
        response["next_steps_path"] = adjudication_result["next_steps_path"]
    return response, adjudication_result


def process_pending_dialog_sessions(
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
    dialog_root: Path | None = None,
    pattern: str = DIALOG_GLOB,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    active_dialog_root = dialog_root if dialog_root is not None else shared_root / "dialog"
    for session_path in list_dialog_sessions(shared_root=shared_root, dialog_root=active_dialog_root, pattern=pattern):
        request_event = find_pending_handoff_request(session_path)
        if request_event is None:
            continue
        response, adjudication_result = build_handoff_response(
            request_event,
            session_path=session_path,
            shared_root=shared_root,
        )
        _append_jsonl(session_path, response)
        processed.append(
            {
                "session_file": str(session_path),
                "session_id": response.get("session_id"),
                "summary": response.get("summary"),
                "finding_positions_count": len(response.get("finding_positions", [])),
                "mim_adjudication_path": (
                    adjudication_result.get("mim_adjudication_path") if adjudication_result else ""
                ),
            }
        )
    return {
        "generated_at": _utc_now(),
        "processed_count": len(processed),
        "processed_sessions": processed,
    }