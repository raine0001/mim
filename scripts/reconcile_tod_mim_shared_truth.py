from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARED_ROOT = REPO_ROOT / "runtime" / "shared"
DEFAULT_OUTPUT_PATH = DEFAULT_SHARED_ROOT / "TOD_MIM_SHARED_TRUTH.latest.json"
DEFAULT_INTEGRATION_PATH = REPO_ROOT / "shared_state" / "integration_status.json"
DEFAULT_MIM_CONTEXT_EXPORT_PATH = REPO_ROOT / "tod" / "out" / "context-sync" / "MIM_CONTEXT_EXPORT.latest.json"
DEFAULT_MIM_CONTEXT_EXPORT_SSH_PATH = REPO_ROOT / "tod" / "out" / "context-sync" / "ssh-shared" / "MIM_CONTEXT_EXPORT.latest.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    current = now or _utc_now()
    return max(0, int(round((current - parsed).total_seconds())))


def _compact_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.exists() or not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _first_existing_payload(*paths: Path) -> tuple[dict[str, Any], str]:
    for path in paths:
        payload = _load_json_file(path)
        if payload:
            return payload, str(path)
    return {}, ""


def _normalize_string_list(values: Any, limit: int = 8) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for item in values[:limit]:
        text = str(item or "").strip()
        if text:
            items.append(text)
    return items


def _pick_first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _latest_timestamp(*values: Any) -> str:
    latest: datetime | None = None
    for value in values:
        if isinstance(value, (list, tuple)):
            candidate = _latest_timestamp(*value)
            parsed = _parse_timestamp(candidate)
        else:
            parsed = _parse_timestamp(value)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest.isoformat().replace("+00:00", "Z") if latest is not None else ""


def _is_newer_timestamp(candidate: Any, baseline: Any) -> bool:
    candidate_dt = _parse_timestamp(candidate)
    baseline_dt = _parse_timestamp(baseline)
    if candidate_dt is None:
        return False
    if baseline_dt is None:
        return True
    return candidate_dt > baseline_dt


def _normalize_objective_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("objective-"):
        return text.split("objective-", 1)[1]
    if text.lower().startswith("obj-"):
        return text.split("-", 1)[1]
    return text


def _same_normalized_id(left: Any, right: Any) -> bool:
    left_text = _normalize_objective_id(left)
    right_text = _normalize_objective_id(right)
    return bool(left_text and right_text and left_text == right_text)


def _compose_objective_task_id(objective_id: Any, task_id: Any) -> str:
    objective_text = _normalize_objective_id(objective_id)
    task_text = str(task_id or "").strip()
    if not objective_text or not task_text:
        return ""
    if task_text.upper().startswith("TSK-") or task_text.lower().startswith("objective-"):
        return task_text
    return f"objective-{objective_text}-task-{task_text}"


def _derive_execution_lock(lock_payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(lock_payload, dict):
        return {}

    current_processing = lock_payload.get("current_processing") if isinstance(lock_payload.get("current_processing"), dict) else {}
    writer = _pick_first(lock_payload.get("writer"), lock_payload.get("owner"), lock_payload.get("authority_owner"))
    source = _pick_first(lock_payload.get("source"), lock_payload.get("surface"))
    task_id = _pick_first(lock_payload.get("task_id"), current_processing.get("task_id"))
    objective_id = _normalize_objective_id(_pick_first(lock_payload.get("objective_id"), current_processing.get("objective_id")))
    request_id = _pick_first(lock_payload.get("request_id"), current_processing.get("request_id"), task_id)
    correlation_id = _pick_first(lock_payload.get("correlation_id"), current_processing.get("correlation_id"), request_id)
    if not task_id:
        return {}

    return {
        "writer": writer,
        "source": source,
        "objective_id": objective_id,
        "task_id": task_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "updated_at": _pick_first(lock_payload.get("generated_at"), lock_payload.get("updated_at")),
        "active": True,
    }


def _has_meaningful_evidence(execution_result: dict[str, Any], truth_row: dict[str, Any], next_task_selection: dict[str, Any]) -> tuple[bool, list[str]]:
    execution_evidence = execution_result.get("execution_evidence") if isinstance(execution_result.get("execution_evidence"), dict) else {}
    truth_evidence = truth_row.get("execution_evidence") if isinstance(truth_row.get("execution_evidence"), dict) else {}
    lists = [
        execution_evidence.get("meaningful_evidence"),
        truth_evidence.get("meaningful_evidence"),
    ]
    collected: list[str] = []
    for values in lists:
        collected.extend(_normalize_string_list(values, limit=12))
    unique: list[str] = []
    seen: set[str] = set()
    for item in collected:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    if unique:
        return True, unique

    next_outcome = next_task_selection.get("last_terminal_outcome") if isinstance(next_task_selection.get("last_terminal_outcome"), dict) else {}
    if next_outcome.get("meaningful_evidence") is False:
        return False, []
    return False, []


def _derive_tod_view(artifacts: dict[str, tuple[dict[str, Any], str]], *, now: datetime) -> dict[str, Any]:
    execution_result, execution_result_path = artifacts["execution_result"]
    truth_payload, truth_path = artifacts["execution_truth"]
    next_task_selection, next_task_selection_path = artifacts["next_task_selection"]
    activity_payload, activity_path = artifacts["activity_stream"]
    active_task, active_task_path = artifacts["active_task"]
    validation_payload, validation_path = artifacts["validation_result"]
    integration_payload, _ = artifacts["integration_status"]

    truth_row = {}
    recent_truth = truth_payload.get("recent_execution_truth") if isinstance(truth_payload.get("recent_execution_truth"), list) else []
    if recent_truth and isinstance(recent_truth[0], dict):
        truth_row = recent_truth[0]
    truth_summary = truth_payload.get("summary") if isinstance(truth_payload.get("summary"), dict) else {}
    execution_evidence = execution_result.get("execution_evidence") if isinstance(execution_result.get("execution_evidence"), dict) else {}

    objective_id = _normalize_objective_id(_pick_first(
        execution_result.get("normalized_objective_id"),
        execution_result.get("objective_id"),
        active_task.get("normalized_objective_id"),
        active_task.get("objective_id"),
        truth_summary.get("objective_id"),
        next_task_selection.get("source_objective"),
        ((integration_payload.get("live_task_request") or {}).get("normalized_objective_id") if isinstance(integration_payload.get("live_task_request"), dict) else ""),
    ))
    task_id = _pick_first(
        execution_result.get("task_id"),
        active_task.get("task_id"),
        truth_summary.get("task_id"),
        next_task_selection.get("selected_task_id"),
        ((integration_payload.get("live_task_request") or {}).get("task_id") if isinstance(integration_payload.get("live_task_request"), dict) else ""),
    )
    request_id = _pick_first(
        execution_result.get("request_id"),
        truth_summary.get("request_id"),
        next_task_selection.get("request_id"),
        ((integration_payload.get("live_task_request") or {}).get("request_id") if isinstance(integration_payload.get("live_task_request"), dict) else ""),
        task_id,
    )
    correlation_id = _pick_first(
        execution_result.get("correlation_id"),
        (next_task_selection.get("dispatch_result") or {}).get("correlation_id") if isinstance(next_task_selection.get("dispatch_result"), dict) else "",
        ((integration_payload.get("live_task_request") or {}).get("correlation_id") if isinstance(integration_payload.get("live_task_request"), dict) else ""),
        request_id,
    )
    task_title = _pick_first(
        execution_result.get("title"),
        active_task.get("title"),
        next_task_selection.get("selected_task_title"),
    )
    phase = _pick_first(
        execution_result.get("phase"),
        activity_payload.get("phase"),
        validation_payload.get("phase"),
        active_task.get("phase"),
    )
    execution_state = _pick_first(
        execution_result.get("execution_state"),
        truth_row.get("execution_state"),
        next_task_selection.get("dispatch_status"),
        active_task.get("execution_state"),
        activity_payload.get("execution_state"),
    ).lower()
    validation_status = _pick_first(
        validation_payload.get("status"),
        execution_result.get("validation_status"),
        "passed" if execution_evidence.get("validation_passed") is True else "",
        "failed" if execution_evidence.get("validation_passed") is False else "",
        "passed" if truth_summary.get("validation_passed") is True else "",
        "failed" if truth_summary.get("validation_passed") is False else "",
    ).lower()
    files_changed = _normalize_string_list(
        execution_result.get("files_changed") if isinstance(execution_result.get("files_changed"), list) else execution_evidence.get("files_changed"),
        limit=16,
    )
    commands_run = _normalize_string_list(
        execution_result.get("commands_run") if isinstance(execution_result.get("commands_run"), list) else execution_evidence.get("commands_run"),
        limit=24,
    )
    meaningful_evidence_present, meaningful_evidence = _has_meaningful_evidence(execution_result, truth_row, next_task_selection)
    blocker_code = _pick_first(
        execution_result.get("reason_code"),
        execution_evidence.get("reason_code"),
        (next_task_selection.get("last_terminal_outcome") or {}).get("reason_code") if isinstance(next_task_selection.get("last_terminal_outcome"), dict) else "",
    )
    blocker_detail = _compact_text(_pick_first(
        execution_result.get("wait_reason"),
        execution_result.get("summary"),
        (next_task_selection.get("dispatch_result") or {}).get("review_response", {}).get("rationale") if isinstance((next_task_selection.get("dispatch_result") or {}).get("review_response"), dict) else "",
        next_task_selection.get("reason_selected"),
        next_task_selection.get("selected_task_scope"),
    ))
    updated_at = _latest_timestamp(
        execution_result.get("updated_at"),
        execution_result.get("generated_at"),
        truth_row.get("generated_at"),
        truth_payload.get("generated_at"),
        next_task_selection.get("generated_at"),
        activity_payload.get("updated_at"),
        activity_payload.get("generated_at"),
        active_task.get("updated_at"),
        active_task.get("generated_at"),
        validation_payload.get("updated_at"),
        validation_payload.get("generated_at"),
    )
    freshness_seconds = _age_seconds(updated_at, now=now)
    recent_activity = freshness_seconds is not None and freshness_seconds <= 1200

    next_outcome = next_task_selection.get("last_terminal_outcome") if isinstance(next_task_selection.get("last_terminal_outcome"), dict) else {}
    next_dispatch_status = str(next_task_selection.get("dispatch_status") or "").strip().lower()
    state = "stale"
    state_reason = "No recent TOD execution evidence is currently available."
    execution_evidence_state = "missing"
    if meaningful_evidence_present:
        execution_evidence_state = "meaningful_evidence_present"
    elif execution_state == "no_op_rejected" or str(next_outcome.get("classification") or "").strip().lower() == "no_op_rejected":
        execution_evidence_state = "no_meaningful_execution_evidence"
    elif execution_result or truth_row or next_task_selection:
        execution_evidence_state = "present_without_meaningful_evidence"

    if execution_state in {"completed", "complete", "success", "succeeded"} and meaningful_evidence_present:
        state = "completed_with_evidence"
        state_reason = _compact_text(_pick_first(execution_result.get("summary"), truth_summary.get("summary"), "TOD completed the current execution slice with meaningful evidence."))
    elif execution_state == "no_op_rejected" or str(next_outcome.get("classification") or "").strip().lower() == "no_op_rejected" or blocker_code == "no_meaningful_execution_evidence":
        state = "no_op_rejected"
        state_reason = _compact_text(_pick_first(
            next_outcome.get("summary"),
            blocker_detail,
            "TOD rejected the last execution because it did not publish meaningful evidence.",
        ))
    elif execution_state == "blocked_with_reason" or next_dispatch_status == "blocked_with_reason":
        state = "blocked_with_reason"
        state_reason = blocker_detail or "TOD published an explicit blocker for the active work."
    elif recent_activity and (execution_result or activity_payload or active_task or next_task_selection):
        state = "active"
        state_reason = _compact_text(_pick_first(
            execution_result.get("current_action"),
            execution_result.get("summary"),
            activity_payload.get("current_action"),
            next_task_selection.get("reason_selected"),
            "TOD has recent activity on the current objective.",
        ))

    return {
        "state": state,
        "reason": state_reason,
        "objective_id": objective_id,
        "task_id": task_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "task_title": task_title,
        "phase": phase,
        "execution_state": execution_state,
        "execution_evidence_state": execution_evidence_state,
        "meaningful_evidence_present": meaningful_evidence_present,
        "meaningful_evidence": meaningful_evidence,
        "files_changed": files_changed,
        "commands_run_count": len(commands_run),
        "validation_status": validation_status,
        "blocker_code": blocker_code,
        "blocker_detail": blocker_detail,
        "updated_at": updated_at,
        "freshness_seconds": freshness_seconds,
        "source_paths": [path for path in (execution_result_path, truth_path, next_task_selection_path, activity_path, active_task_path, validation_path) if path],
    }


def _derive_mim_view(artifacts: dict[str, tuple[dict[str, Any], str]], *, now: datetime) -> dict[str, Any]:
    integration_payload, integration_path = artifacts["integration_status"]
    task_status_review, task_status_review_path = artifacts["task_status_review"]
    decision_task, decision_task_path = artifacts["decision_task"]
    mim_context_export, mim_context_export_path = artifacts["mim_context_export"]

    mim_status = integration_payload.get("mim_status") if isinstance(integration_payload.get("mim_status"), dict) else {}
    handshake = integration_payload.get("mim_handshake") if isinstance(integration_payload.get("mim_handshake"), dict) else {}
    source_of_truth = mim_context_export.get("source_of_truth") if isinstance(mim_context_export.get("source_of_truth"), dict) else {}
    formal_program_truth = source_of_truth.get("formal_program_truth") if isinstance(source_of_truth.get("formal_program_truth"), dict) else {}
    objective_target = source_of_truth.get("objective_target") if isinstance(source_of_truth.get("objective_target"), dict) else {}
    live_task_request_signal = source_of_truth.get("live_task_request_signal") if isinstance(source_of_truth.get("live_task_request_signal"), dict) else {}
    blockers = _normalize_string_list(task_status_review.get("blocking_reason_codes"), limit=8)
    blockers_text = _pick_first(
        mim_status.get("blockers"),
        "; ".join(blockers),
        (decision_task.get("communication_escalation") or {}).get("detail") if isinstance(decision_task.get("communication_escalation"), dict) else "",
    )
    authority_source = _pick_first(
        source_of_truth.get("objective_active_source"),
        "task_status_review" if task_status_review else "",
        "decision_task" if decision_task else "",
        "mim_status" if mim_status else "",
    )
    authoritative_objective_id = _normalize_objective_id(_pick_first(
        objective_target.get("objective"),
        formal_program_truth.get("objective"),
        mim_status.get("objective_active"),
        handshake.get("current_next_objective"),
        handshake.get("objective_active"),
    ))
    authoritative_task_id = _pick_first(
        task_status_review.get("task_id"),
        decision_task.get("active_task_id"),
        _compose_objective_task_id(authoritative_objective_id, formal_program_truth.get("task_id")),
        _compose_objective_task_id(authoritative_objective_id, live_task_request_signal.get("task_id") if live_task_request_signal.get("objective_authority_eligible") else ""),
    )
    authoritative_request_id = _pick_first(
        task_status_review.get("request_id"),
        decision_task.get("request_id"),
        authoritative_task_id,
    )
    updated_at = _latest_timestamp(
        task_status_review.get("generated_at"),
        task_status_review.get("updated_at"),
        decision_task.get("generated_at"),
        mim_status.get("generated_at"),
        handshake.get("generated_at"),
        mim_context_export.get("exported_at"),
        formal_program_truth.get("generated_at"),
        integration_payload.get("generated_at"),
    )
    freshness_seconds = _age_seconds(updated_at, now=now)

    state = "unknown"
    reason = "MIM has not published a focused task review state yet."
    if bool(mim_status.get("is_stale")):
        state = "stale"
        reason = _compact_text(blockers_text or "MIM still reports a stale or lagging view of the current work.")
    elif blockers_text:
        state = "blocked_with_reason"
        reason = _compact_text(blockers_text)
    elif str(mim_status.get("phase") or "").strip().lower() == "execution" or str(formal_program_truth.get("execution_state") or "").strip().lower() == "executing":
        state = "active"
        reason = "MIM reports execution-phase visibility for the active objective."

    return {
        "state": state,
        "reason": reason,
        "objective_id": authoritative_objective_id,
        "task_id": authoritative_task_id,
        "request_id": authoritative_request_id,
        "task_title": _pick_first(formal_program_truth.get("task_title"), decision_task.get("task_title")),
        "phase": _pick_first(mim_status.get("phase"), decision_task.get("state"), formal_program_truth.get("execution_state")),
        "updated_at": updated_at,
        "freshness_seconds": freshness_seconds,
        "authority_source": authority_source,
        "authority_reason": _pick_first(source_of_truth.get("manifest_source_selection_reason"), source_of_truth.get("manifest_source_used")),
        "authoritative": authority_source in {"formal_program_truth", "task_status_review", "decision_task"},
        "source_paths": [path for path in (task_status_review_path, decision_task_path, mim_context_export_path, integration_path) if path],
    }


def reconcile_shared_truth_payload(artifacts: dict[str, tuple[dict[str, Any], str]], *, now: datetime | None = None) -> dict[str, Any]:
    current = now or _utc_now()
    tod_view = _derive_tod_view(artifacts, now=current)
    mim_view = _derive_mim_view(artifacts, now=current)
    integration_payload, integration_path = artifacts["integration_status"]
    execution_lock_payload, execution_lock_path = artifacts.get("execution_lock", ({}, ""))
    execution_lock = _derive_execution_lock(execution_lock_payload)
    execution_truth_present = bool(execution_lock.get("task_id") or tod_view.get("task_id"))
    same_objective = _same_normalized_id(tod_view.get("objective_id"), mim_view.get("objective_id"))
    same_task = bool(tod_view.get("task_id") and mim_view.get("task_id") and str(tod_view.get("task_id")) == str(mim_view.get("task_id")))
    tod_newer_than_mim = _is_newer_timestamp(tod_view.get("updated_at"), mim_view.get("updated_at"))
    mim_newer_than_tod = _is_newer_timestamp(mim_view.get("updated_at"), tod_view.get("updated_at"))
    prefer_mim_canonical_lane = (not execution_truth_present) and bool(mim_view.get("objective_id")) and (
        not tod_view.get("objective_id")
        or (mim_view.get("authoritative") and not same_objective and mim_newer_than_tod)
        or (same_objective and mim_view.get("state") in {"blocked_with_reason", "active"} and not tod_newer_than_mim and tod_view.get("state") != "completed_with_evidence")
    )

    disagreement_reasons: list[str] = []
    if tod_view["objective_id"] and mim_view["objective_id"] and not same_objective:
        disagreement_reasons.append("objective_mismatch")
    if tod_view["state"] != mim_view["state"] and mim_view["state"] not in {"", "unknown"}:
        disagreement_reasons.append(f"state_mismatch:{tod_view['state']}!={mim_view['state']}")
    if tod_view["task_id"] and mim_view["task_id"] and not same_task:
        disagreement_reasons.append("task_mismatch")
    if execution_lock.get("task_id") and mim_view.get("task_id") and str(execution_lock.get("task_id")) != str(mim_view.get("task_id")):
        disagreement_reasons.append("mim_execution_truth_override_rejected")
    if execution_lock.get("task_id") and tod_view.get("task_id") and str(execution_lock.get("task_id")) != str(tod_view.get("task_id")):
        disagreement_reasons.append("tod_execution_truth_lock_mismatch")

    disagreement_detected = bool(disagreement_reasons)
    disagreement_reason = "; ".join(disagreement_reasons)

    state = "STALE"
    state_reason = "No recent evidence exists on either the TOD or MIM truth surfaces."
    authoritative_next_action = "self-driving task selection loop"

    if not same_objective and tod_view.get("objective_id") and mim_view.get("objective_id"):
        state = "DISAGREEMENT"
        state_reason = _compact_text(disagreement_reason or "TOD and MIM disagree on the current authoritative state.")
        authoritative_next_action = "preserve canonical MIM lane and clear stale non-matching TOD artifacts" if prefer_mim_canonical_lane else "reconcile authoritative artifact sources"
    elif same_objective and tod_view["state"] == "completed_with_evidence" and mim_view["state"] == "stale":
        state = "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH"
        state_reason = "TOD completed with meaningful evidence, but MIM still reports a stale view and needs a refresh."
        authoritative_next_action = "refresh MIM consumer / republish shared truth"
    elif same_objective and tod_view["state"] == "completed_with_evidence" and tod_newer_than_mim:
        state = "ACCEPTED_COMPLETE"
        state_reason = tod_view["reason"] or "TOD completed the current objective with meaningful evidence."
        authoritative_next_action = "refresh MIM consumer / republish shared truth"
    elif same_objective and tod_view["state"] == "completed_with_evidence" and mim_view["state"] == "blocked_with_reason" and not bool(mim_view.get("authoritative")):
        state = "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH"
        state_reason = "TOD completed with meaningful evidence, but MIM still reports a non-authoritative blocker and needs a refresh."
        authoritative_next_action = "refresh MIM consumer / republish shared truth"
    elif same_objective and mim_view["state"] == "blocked_with_reason" and (mim_newer_than_tod or tod_view["state"] == "stale"):
        state = "BLOCKED_WITH_REASON"
        state_reason = mim_view["reason"] or "MIM published a fresher blocker on the current objective."
        authoritative_next_action = "address blocker"
    elif same_objective and mim_view["state"] == "active" and mim_view.get("authoritative") and (mim_newer_than_tod or tod_view["state"] == "stale"):
        state = "ACTIVE"
        state_reason = mim_view["reason"] or "MIM has the freshest active view for the canonical objective."
        authoritative_next_action = "wait/check next activity deadline"
    elif tod_view["state"] == "blocked_with_reason":
        state = "BLOCKED_WITH_REASON"
        state_reason = tod_view["reason"] or "TOD published an explicit blocker."
        authoritative_next_action = "address blocker"
    elif tod_view["state"] == "no_op_rejected":
        state = "REPLAY_OR_REPLAN_REQUIRED"
        state_reason = tod_view["reason"] or "TOD rejected the last run because it did not produce meaningful evidence."
        authoritative_next_action = "forced replay or replan"
    elif tod_view["state"] == "stale":
        state = "STALE"
        state_reason = tod_view["reason"] or "No recent evidence exists on either the TOD or MIM truth surfaces."
        authoritative_next_action = "self-driving task selection loop"
    elif disagreement_detected:
        state = "DISAGREEMENT"
        state_reason = _compact_text(disagreement_reason or "TOD and MIM disagree on the current authoritative state.")
        authoritative_next_action = "reconcile authoritative artifact sources"
    elif tod_view["state"] == "active":
        state = "ACTIVE"
        state_reason = tod_view["reason"] or "TOD has recent activity on the current objective."
        authoritative_next_action = "wait/check next activity deadline"
    elif tod_view["state"] == "completed_with_evidence":
        state = "ACCEPTED_COMPLETE"
        state_reason = tod_view["reason"] or "TOD completed the current objective with meaningful evidence."
        authoritative_next_action = "monitor MIM refresh and continue with the next bounded objective"

    latest_source_timestamp = _latest_timestamp(
        tod_view.get("updated_at"),
        mim_view.get("updated_at"),
        integration_payload.get("generated_at"),
    )
    freshness_seconds = _age_seconds(latest_source_timestamp, now=current)
    confidence = "low"
    if state in {"BLOCKED_WITH_REASON", "ACTIVE", "REPLAY_OR_REPLAN_REQUIRED"}:
        confidence = "medium"
    if state in {"ACCEPTED_COMPLETE_PENDING_MIM_REFRESH", "ACCEPTED_COMPLETE"} and tod_view["meaningful_evidence_present"]:
        confidence = "high"
    if state == "DISAGREEMENT":
        confidence = "medium"

    objective_id = _pick_first(
        execution_lock.get("objective_id"),
        mim_view.get("objective_id") if prefer_mim_canonical_lane else "",
        tod_view.get("objective_id"),
        mim_view.get("objective_id"),
    )
    task_id = _pick_first(
        execution_lock.get("task_id"),
        mim_view.get("task_id") if prefer_mim_canonical_lane else "",
        tod_view.get("task_id"),
        mim_view.get("task_id"),
    )
    request_id = _pick_first(
        execution_lock.get("request_id"),
        mim_view.get("request_id") if prefer_mim_canonical_lane else "",
        tod_view.get("request_id"),
        mim_view.get("request_id"),
        task_id,
    )
    correlation_id = _pick_first(
        execution_lock.get("correlation_id"),
        tod_view.get("correlation_id") if not prefer_mim_canonical_lane else "",
        request_id,
        task_id,
        tod_view.get("correlation_id"),
    )

    return {
        "generated_at": _utc_now_iso(),
        "source": "tod-mim-shared-truth-reconciler-v1",
        "objective_id": objective_id,
        "task_id": task_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "task_title": _pick_first(mim_view.get("task_title") if prefer_mim_canonical_lane else "", tod_view.get("task_title"), mim_view.get("task_title")),
        "phase": _pick_first(mim_view.get("phase") if prefer_mim_canonical_lane else "", tod_view.get("phase"), mim_view.get("phase")),
        "state": state,
        "state_reason": state_reason,
        "execution_state": tod_view.get("execution_state") or "",
        "execution_evidence_state": tod_view.get("execution_evidence_state") or "missing",
        "meaningful_evidence_present": bool(tod_view.get("meaningful_evidence_present")),
        "files_changed": tod_view.get("files_changed") if isinstance(tod_view.get("files_changed"), list) else [],
        "commands_run_count": int(tod_view.get("commands_run_count") or 0),
        "validation_status": tod_view.get("validation_status") or "",
        "blocker_code": tod_view.get("blocker_code") or "",
        "blocker_detail": tod_view.get("blocker_detail") or mim_view.get("reason") or "",
        "tod_view": tod_view,
        "mim_view": mim_view,
        "disagreement_detected": disagreement_detected,
        "disagreement_reason": disagreement_reason,
        "authoritative_next_action": authoritative_next_action,
        "canonical_lane_source": "execution_lock" if execution_lock.get("task_id") else (mim_view.get("authority_source") if prefer_mim_canonical_lane else "tod_execution_artifacts"),
        "execution_lock": {
            "active": bool(execution_lock.get("task_id")),
            "writer": execution_lock.get("writer") or "",
            "source": execution_lock.get("source") or "",
            "objective_id": execution_lock.get("objective_id") or "",
            "task_id": execution_lock.get("task_id") or "",
            "request_id": execution_lock.get("request_id") or "",
            "correlation_id": execution_lock.get("correlation_id") or "",
            "updated_at": execution_lock.get("updated_at") or "",
        },
        "confidence": confidence,
        "freshness_seconds": freshness_seconds if freshness_seconds is not None else -1,
        "source_paths": {
            "integration_status": integration_path,
            "mim_context_export": artifacts["mim_context_export"][1],
            "execution_result": artifacts["execution_result"][1],
            "execution_truth": artifacts["execution_truth"][1],
            "next_task_selection": artifacts["next_task_selection"][1],
            "activity_stream": artifacts["activity_stream"][1],
            "active_task": artifacts["active_task"][1],
            "validation_result": artifacts["validation_result"][1],
            "task_status_review": artifacts["task_status_review"][1],
            "decision_task": artifacts["decision_task"][1],
            "execution_lock": execution_lock_path,
        },
    }


def load_runtime_artifacts(*, shared_root: Path = DEFAULT_SHARED_ROOT, integration_path: Path = DEFAULT_INTEGRATION_PATH) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        "execution_result": _first_existing_payload(shared_root / "TOD_EXECUTION_RESULT.latest.json"),
        "execution_truth": _first_existing_payload(shared_root / "TOD_EXECUTION_TRUTH.latest.json"),
        "execution_lock": _first_existing_payload(shared_root / "TOD_EXECUTION_LOCK.latest.json"),
        "next_task_selection": _first_existing_payload(shared_root / "TOD_NEXT_TASK_SELECTION.latest.json"),
        "activity_stream": _first_existing_payload(shared_root / "TOD_ACTIVITY_STREAM.latest.json"),
        "active_task": _first_existing_payload(shared_root / "TOD_ACTIVE_TASK.latest.json"),
        "validation_result": _first_existing_payload(shared_root / "TOD_VALIDATION_RESULT.latest.json"),
        "task_status_review": _first_existing_payload(shared_root / "MIM_TASK_STATUS_REVIEW.latest.json"),
        "decision_task": _first_existing_payload(shared_root / "MIM_DECISION_TASK.latest.json"),
        "mim_context_export": _first_existing_payload(DEFAULT_MIM_CONTEXT_EXPORT_PATH, DEFAULT_MIM_CONTEXT_EXPORT_SSH_PATH),
        "integration_status": _first_existing_payload(shared_root / "TOD_INTEGRATION_STATUS.latest.json", shared_root / "TOD_integration_status.latest.json", integration_path),
    }


def write_shared_truth(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile TOD and MIM runtime artifacts into one shared truth artifact.")
    parser.add_argument("--shared-root", default=str(DEFAULT_SHARED_ROOT), help="Directory containing runtime/shared artifacts.")
    parser.add_argument("--integration-path", default=str(DEFAULT_INTEGRATION_PATH), help="Fallback path for integration_status.json.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output path for TOD_MIM_SHARED_TRUTH.latest.json.")
    args = parser.parse_args()

    shared_root = Path(args.shared_root)
    integration_path = Path(args.integration_path)
    output_path = Path(args.output)

    payload = reconcile_shared_truth_payload(load_runtime_artifacts(shared_root=shared_root, integration_path=integration_path))
    write_shared_truth(output_path, payload)
    print(json.dumps({
        "output": str(output_path),
        "state": payload.get("state"),
        "objective_id": payload.get("objective_id"),
        "task_id": payload.get("task_id"),
        "authoritative_next_action": payload.get("authoritative_next_action"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())