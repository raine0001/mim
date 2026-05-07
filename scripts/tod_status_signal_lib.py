#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path


TERMINAL_SUCCESS_STATUSES = {"completed", "succeeded", "approved", "done"}
TERMINAL_FAILURE_STATUSES = {"failed", "blocked", "rejected", "cancelled", "canceled"}
TERMINAL_RESULT_STATUSES = TERMINAL_SUCCESS_STATUSES | TERMINAL_FAILURE_STATUSES
FORMAL_PROGRAM_ACTIVE_STATES = {"active", "created", "executing", "in_progress", "queued", "running", "working"}
ACTIVE_OPERATOR_INCIDENT_PRECEDENCE = "prefer_incident_over_latest"
NON_TASK_STREAM_TRIGGERS = {"", "liveness_ping", "coordination_ack_posted"}
DEFAULT_TOD_SILENCE_EMERGENCY_SECONDS = 60
DEFAULT_TOD_DIRECT_EXECUTION_SECONDS = 120


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def incident_artifact_key(*, objective_id: object, subtype: str) -> str:
    objective_text = normalize_objective(objective_id) or "unknown"
    subtype_text = re.sub(r"[^a-z0-9]+", "_", str(subtype or "incident").strip().lower()).strip("_")
    return f"objective-{objective_text}-{subtype_text}"


def incident_artifact_paths(*, shared_dir: Path, objective_id: object, subtype: str) -> dict[str, Path]:
    key = incident_artifact_key(objective_id=objective_id, subtype=subtype)
    incident_dir = shared_dir / "incidents"
    return {
        "summary": incident_dir / f"{key}.active.json",
        "review": incident_dir / f"{key}.review.json",
        "next_action": incident_dir / f"{key}.next_action.json",
        "decision_task": incident_dir / f"{key}.decision_task.json",
    }


def read_active_operator_incident(shared_dir: Path) -> dict:
    payload = read_json(shared_dir / "MIM_OPERATOR_INCIDENT.latest.json")
    return payload if isinstance(payload, dict) else {}


def active_operator_incident_review(shared_dir: Path) -> dict:
    incident = read_active_operator_incident(shared_dir)
    if incident.get("active") is not True:
        return {}
    review_path = str(incident.get("review_path") or "").strip()
    if not review_path:
        return {}
    payload = read_json(Path(review_path))
    return payload if isinstance(payload, dict) else {}


def build_operator_incident(
    *,
    review: dict | None,
    next_action: dict | None,
    decision_task: dict | None,
    existing_incident: dict | None = None,
    shared_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    reference = now or datetime.now(timezone.utc)
    review = _as_dict(review)
    next_action = _as_dict(next_action)
    decision_task = _as_dict(decision_task)
    existing_incident = _as_dict(existing_incident)
    task = _as_dict(review.get("task"))

    active_task_id = _first_text(task, "active_task_id")
    objective_id = normalize_objective(task.get("objective_id") or active_task_id)
    state = _first_text(review, "state")
    state_reason = _first_text(review, "state_reason")
    blocking_reason_codes = [
        str(item).strip() for item in review.get("blocking_reason_codes", []) if str(item).strip()
    ]

    subtype = ""
    if state == "failed" and state_reason == "executor_failed":
        if "executor_memory_pressure" in blocking_reason_codes:
            subtype = "executor_memory_pressure"

    if not subtype:
        if existing_incident.get("active") is True:
            return existing_incident
        return {}

    shared_dir = shared_dir if isinstance(shared_dir, Path) else Path(".")
    paths = incident_artifact_paths(shared_dir=shared_dir, objective_id=objective_id, subtype=subtype)

    selected_action = _as_dict(next_action.get("selected_action"))
    decision = _as_dict(decision_task.get("decision"))
    return {
        "generated_at": reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "mim_operator_incident_v1",
        "active": True,
        "incident_key": incident_artifact_key(objective_id=objective_id, subtype=subtype),
        "objective_id": objective_id,
        "active_task_id": active_task_id,
        "communication": {
            "state": "healthy",
        },
        "execution": {
            "state": "failed",
            "failure": state_reason,
            "subtype": subtype,
        },
        "review_state": state,
        "review_state_reason": state_reason,
        "blocking_reason_codes": blocking_reason_codes,
        "selected_action": {
            "code": _first_text(selected_action, "code") or _first_text(decision, "code") or "remediate_tod_executor_failure",
            "detail": _first_text(selected_action, "detail") or _first_text(decision, "detail"),
        },
        "precedence": ACTIVE_OPERATOR_INCIDENT_PRECEDENCE,
        "review_path": str(paths["review"]),
        "next_action_path": str(paths["next_action"]),
        "decision_task_path": str(paths["decision_task"]),
    }


def normalize_objective(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return match.group(1)
    return text


def _objective_sort_key(value: object) -> tuple[int, int]:
    normalized = normalize_objective(value)
    if not normalized:
        return (0, 0)
    major_text, _, minor_text = normalized.partition(".")
    try:
        major = int(major_text)
    except ValueError:
        major = 0
    try:
        minor = int(minor_text) if minor_text else 0
    except ValueError:
        minor = 0
    return (major, minor)


def parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_dict(payload: dict | None) -> dict:
    return payload if isinstance(payload, dict) else {}


def _first_text(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _matches_current_request_identity(
    value: str,
    *,
    active_task_id: str,
    request_request_id: str = "",
) -> bool:
    current_value = str(value or "").strip()
    if not current_value:
        return False
    if current_value == str(active_task_id or "").strip():
        return True
    request_value = str(request_request_id or "").strip()
    return bool(request_value and current_value == request_value)


def _is_task_stream_trigger(trigger: dict) -> bool:
    trigger_name = _first_text(trigger, "trigger").lower()
    return trigger_name not in NON_TASK_STREAM_TRIGGERS


def _trigger_ack_task_identity(trigger_ack: dict) -> str:
    trigger_ack = _as_dict(trigger_ack)
    if not trigger_ack:
        return ""

    trigger_context = _as_dict(trigger_ack.get("trigger_context"))
    bridge_runtime = _as_dict(trigger_ack.get("bridge_runtime"))
    current_processing = _as_dict(bridge_runtime.get("current_processing"))
    return _first_text(
        trigger_ack,
        "task_id",
        "current_task_id",
    ) or _first_text(
        trigger_context,
        "task_id",
    ) or _first_text(
        current_processing,
        "task_id",
    ) or _first_text(
        trigger_ack,
        "request_id",
        "acknowledges",
    ) or _first_text(
        trigger_context,
        "request_id",
    ) or _first_text(
        current_processing,
        "request_id",
    )


def _result_review_is_current(*, task_result: dict, active_task_id: str) -> bool:
    task_result = _as_dict(task_result)
    if not task_result:
        return False
    active_task_text = str(active_task_id or "").strip()
    result_task_id = _first_text(task_result, "task_id")
    if not active_task_text or not result_task_id or result_task_id != active_task_text:
        return False

    reconciliation = _as_dict(task_result.get("reconciliation"))
    if reconciliation:
        review_passed = reconciliation.get("review_passed")
        if review_passed is False:
            return False
        review_current = reconciliation.get("review_decision_current")
        if review_current is False:
            return False
        existing_task_id = _first_text(reconciliation, "existing_task_id")
        if existing_task_id and existing_task_id != active_task_text:
            return False

    current_processing_task_id = _bridge_current_processing_task_id(task_result)
    if current_processing_task_id and current_processing_task_id != active_task_text:
        return False

    return True


def _bridge_current_processing_task_id(task_result: dict) -> str:
    bridge_runtime = _as_dict(task_result.get("bridge_runtime"))
    current_processing = _as_dict(bridge_runtime.get("current_processing"))
    return _first_text(current_processing, "task_id", "request_id")


def _formal_program_active_lane(formal_program_response: dict | None) -> dict:
    payload = _as_dict(formal_program_response)
    objective = _as_dict(payload.get("objective"))
    continuation = _as_dict(payload.get("continuation"))
    continuation_status = _as_dict(continuation.get("status"))
    active_task = _as_dict(continuation_status.get("active_task"))
    active_project = _as_dict(continuation_status.get("active_project"))

    objective_id = normalize_objective(
        objective.get("objective_id")
        or active_task.get("objective_id")
        or active_project.get("objective_id")
    )
    objective_status = _first_text(objective, "status").lower()
    execution_state = (
        _first_text(payload, "execution_state").lower()
        or _first_text(objective, "execution_state").lower()
        or _first_text(continuation_status, "execution_state").lower()
        or _first_text(active_task, "execution_state").lower()
        or _first_text(active_project, "status").lower()
    )
    active = bool(
        objective_id
        and (
            objective_status in FORMAL_PROGRAM_ACTIVE_STATES
            or execution_state in FORMAL_PROGRAM_ACTIVE_STATES
        )
    )
    return {
        "active": active,
        "objective_id": objective_id,
        "objective_status": objective_status,
        "execution_state": execution_state,
        "task_id": _first_text(active_task, "task_id"),
        "task_title": _first_text(active_task, "display_title", "title"),
        "project_id": _first_text(active_project, "project_id"),
        "project_status": _first_text(active_project, "status"),
        "generated_at": _first_text(payload, "generated_at"),
    }


def _sanitize_persistent_task(
    *,
    persistent_task: dict,
    task_request: dict,
    trigger: dict,
    task_result: dict,
) -> dict:
    sanitized = dict(persistent_task)
    if not sanitized:
        return sanitized

    persistent_status = _first_text(sanitized, "status").lower()
    persistent_objective = normalize_objective(
        sanitized.get("objective_id") or sanitized.get("task_id")
    )
    request_task_id = _first_text(task_request, "task_id", "request_id")
    request_objective = normalize_objective(
        task_request.get("objective_id")
        or task_request.get("objective")
        or request_task_id
    )
    trigger_name = _first_text(trigger, "trigger")
    actionable_trigger = _is_task_stream_trigger(trigger)
    trigger_objective = normalize_objective(
        trigger.get("objective_id")
        or (_first_text(trigger, "task_id", "request_id") if actionable_trigger else "")
    )
    result_request_id = _first_text(task_result, "request_id", "task_id")
    result_objective = normalize_objective(
        task_result.get("objective_id") or result_request_id
    )

    live_objective = request_objective or trigger_objective or result_objective
    live_task_present = bool(request_task_id or actionable_trigger or result_request_id)
    persistent_terminal = persistent_status in TERMINAL_RESULT_STATUSES | {"completed"}

    if live_task_present and persistent_terminal:
        return {}

    if live_objective and persistent_objective and live_objective != persistent_objective:
        return {}

    return sanitized


def _authoritative_task_override(
    *,
    request_task_id: str,
    trigger_task_id: str,
    task_ack_request_id: str,
    result_request_id: str,
    task_result: dict,
) -> tuple[str, str]:
    if not result_request_id:
        return "", ""

    stale_request = _as_dict(task_result.get("stale_request"))
    stale_request_id = _first_text(stale_request, "request_id", "task_id")
    stale_reason = _first_text(stale_request, "reason")
    request_action_raw = _first_text(task_result, "request_action_raw")
    current_processing_task_id = _bridge_current_processing_task_id(task_result)
    result_status = _first_text(task_result, "status").lower()

    stale_markers = {"lower_ordinal_backfill_ignored", "stale_backfill_ignored"}
    stale_request_matches = request_task_id and stale_request_id == request_task_id
    stale_trigger_matches = trigger_task_id and stale_request_id == trigger_task_id
    stale_marker_present = stale_reason in stale_markers or request_action_raw in stale_markers
    authoritative_processing = (
        current_processing_task_id == result_request_id if current_processing_task_id else True
    )
    active_processing_matches = bool(
        current_processing_task_id and current_processing_task_id == result_request_id
    )
    ack_and_result_agree = bool(
        task_ack_request_id and task_ack_request_id == result_request_id
    )
    request_objective = normalize_objective(request_task_id)
    result_objective = normalize_objective(result_request_id)
    same_objective_task_conflict = bool(
        request_task_id
        and result_request_id
        and request_task_id != result_request_id
        and request_objective
        and result_objective
        and request_objective == result_objective
    )

    if authoritative_processing and stale_marker_present and (stale_request_matches or stale_trigger_matches):
        return result_request_id, "task_result_marked_prior_request_stale"

    if (
        result_status in TERMINAL_RESULT_STATUSES
        and ack_and_result_agree
        and active_processing_matches
        and not same_objective_task_conflict
    ):
        return result_request_id, "task_ack_and_terminal_result_agree_on_authoritative_task"

    if (
        authoritative_processing
        and task_ack_request_id
        and task_ack_request_id == result_request_id
        and request_task_id
        and request_task_id != result_request_id
        and stale_request_matches
    ):
        return result_request_id, "task_ack_and_result_agree_on_authoritative_task"

    return "", ""


def _same_objective_task_mismatch(
    *,
    request_task_id: str,
    task_request: dict,
    trigger_task_id: str,
    trigger: dict,
    task_ack_request_id: str,
    task_ack: dict,
    result_task_id: str,
    task_result: dict,
) -> bool:
    request_task_text = str(request_task_id or "").strip()
    if not request_task_text:
        return False

    request_objective = normalize_objective(
        task_request.get("objective_id") or task_request.get("objective") or request_task_text
    )

    def _same_objective(candidate_task_id: str, candidate_objective: object) -> bool:
        candidate_task_text = str(candidate_task_id or "").strip()
        if not candidate_task_text or candidate_task_text == request_task_text:
            return False
        candidate_objective_text = normalize_objective(candidate_objective or candidate_task_text)
        return bool(request_objective and candidate_objective_text and candidate_objective_text == request_objective)

    return any(
        (
            _same_objective(trigger_task_id, trigger.get("objective_id")),
            _same_objective(task_ack_request_id, task_ack.get("objective_id")),
            _same_objective(result_task_id, task_result.get("objective_id")),
        )
    )


def detect_completed_stream_supersession(
    *,
    task_request: dict | None,
    trigger: dict | None,
    task_ack: dict | None,
    task_result: dict | None,
) -> dict:
    task_request = _as_dict(task_request)
    trigger = _as_dict(trigger)
    task_ack = _as_dict(task_ack)
    task_result = _as_dict(task_result)

    trigger_name = _first_text(trigger, "trigger")
    actionable_trigger = _is_task_stream_trigger(trigger)

    request_task_id = _first_text(task_request, "task_id", "request_id")
    raw_trigger_task_id = _first_text(trigger, "task_id", "request_id")
    trigger_task_id = raw_trigger_task_id if actionable_trigger else ""
    task_ack_request_id = _first_text(task_ack, "request_id", "task_id")
    result_request_id = _first_text(task_result, "request_id", "task_id")
    result_status = _first_text(task_result, "status").lower()

    authoritative_task_id, authoritative_task_reason = _authoritative_task_override(
        request_task_id=request_task_id,
        trigger_task_id=trigger_task_id,
        task_ack_request_id=task_ack_request_id,
        result_request_id=result_request_id,
        task_result=task_result,
    )
    if not authoritative_task_id or result_request_id != authoritative_task_id:
        return {"active": False}

    if result_status not in {"completed", "succeeded", "approved", "done"}:
        return {"active": False}

    stale_request = _as_dict(task_result.get("stale_request"))
    stale_request_task_id = _first_text(stale_request, "request_id", "task_id")
    request_superseded = bool(request_task_id and request_task_id != authoritative_task_id)
    trigger_superseded = bool(raw_trigger_task_id and raw_trigger_task_id != authoritative_task_id)
    stale_request_matches = stale_request_task_id in {request_task_id, raw_trigger_task_id}
    active = bool((request_superseded or trigger_superseded) and stale_request_matches)

    return {
        "active": active,
        "authoritative_task_id": authoritative_task_id,
        "authoritative_task_reason": authoritative_task_reason,
        "request_task_id": request_task_id,
        "trigger_task_id": raw_trigger_task_id,
        "stale_request_task_id": stale_request_task_id,
        "result_status": result_status,
        "reason": "completed_authoritative_task_supersedes_current_request"
        if active
        else "",
    }


def artifact_age_seconds(
    *, payload: dict | None, path: Path | None = None, now: datetime | None = None
) -> int | None:
    reference = now or datetime.now(timezone.utc)
    data = _as_dict(payload)
    for key in ("generated_at", "emitted_at", "updated_at", "created_at"):
        parsed = parse_timestamp(data.get(key))
        if parsed is not None:
            return max(0, int((reference - parsed).total_seconds()))
    if path is not None and path.exists():
        return max(0, int(reference.timestamp() - path.stat().st_mtime))
    return None


def _read_jsonl_tail(path: Path | None, limit: int = 200) -> list[dict]:
    if path is None or not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return []
    rows: list[dict] = []
    for raw in lines[-limit:]:
        text = str(raw).strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_system_alert_summary(
    *,
    stale_ack_watchdog: dict | None,
    catchup_status: dict | None,
    liveness_events: list[dict] | None,
    now: datetime | None = None,
) -> dict:
    reference = now or datetime.now(timezone.utc)
    liveness_warning_max_age_seconds = 90
    stale_ack_watchdog_max_age_seconds = 900
    stale_ack_watchdog = _as_dict(stale_ack_watchdog)
    catchup_status = _as_dict(catchup_status)
    liveness_events = liveness_events if isinstance(liveness_events, list) else []

    alerts: list[dict[str, object]] = []

    stale_ack_generated_at = parse_timestamp(stale_ack_watchdog.get("generated_at"))
    stale_ack_age_seconds = (
        max(0, int((reference - stale_ack_generated_at).total_seconds()))
        if stale_ack_generated_at is not None
        else None
    )
    stale_ack_is_fresh = (
        stale_ack_age_seconds is None
        or stale_ack_age_seconds <= stale_ack_watchdog_max_age_seconds
    )

    if (
        str(stale_ack_watchdog.get("status") or "").strip().lower() == "alert"
        and stale_ack_is_fresh
    ):
        alerts.append(
            {
                "code": "stale_trigger_ack_failures",
                "severity": "critical",
                "source": "objective75_stale_ack_watchdog",
                "detail": str(
                    stale_ack_watchdog.get("reason")
                    or "consecutive stale trigger ACK failures detected"
                ),
                "context": {
                    "task_num": str(stale_ack_watchdog.get("task_num") or ""),
                    "consecutive_failures": int(
                        stale_ack_watchdog.get("consecutive_stale_failures", 0) or 0
                    ),
                    "age_seconds": stale_ack_age_seconds,
                },
            }
        )

    if catchup_status:
        if catchup_status.get("catchup_gate_pass") is False:
            alerts.append(
                {
                    "code": "catchup_gate_blocked",
                    "severity": "critical",
                    "source": "tod_catchup_status",
                    "detail": "TOD catchup gate is failing and dispatch-ready precondition is not met.",
                    "context": {
                        "streak": _as_dict(catchup_status.get("streak")),
                        "confidence": str(catchup_status.get("confidence") or ""),
                    },
                }
            )

        freshness = _as_dict(catchup_status.get("freshness"))
        if freshness and freshness.get("fresh") is False:
            alerts.append(
                {
                    "code": "integration_status_stale",
                    "severity": "warning",
                    "source": "tod_catchup_status",
                    "detail": "TOD integration freshness is stale.",
                    "context": {
                        "age_seconds": freshness.get("age_seconds"),
                        "max_age_seconds": freshness.get("max_age_seconds"),
                    },
                }
            )

        publisher_warning = _as_dict(catchup_status.get("publisher_warning"))
        if publisher_warning.get("active") is True:
            alerts.append(
                {
                    "code": str(publisher_warning.get("code") or "publisher_warning"),
                    "severity": "warning",
                    "source": "tod_catchup_status",
                    "detail": str(publisher_warning.get("message") or "publisher warning active"),
                    "context": {
                        "canonical_objective": publisher_warning.get(
                            "canonical_objective_active"
                        ),
                        "live_task_objective": publisher_warning.get(
                            "live_task_objective"
                        ),
                    },
                }
            )

    freeze_events = [
        item
        for item in liveness_events
        if str(item.get("event") or "").strip().lower() == "freeze_suspected"
    ]
    recent_freeze_events: list[tuple[dict, int | None]] = []
    for item in freeze_events:
        freeze_ts = parse_timestamp(item.get("generated_at"))
        freeze_age = (
            max(0, int((reference - freeze_ts).total_seconds()))
            if freeze_ts is not None
            else None
        )
        if freeze_age is None or freeze_age <= liveness_warning_max_age_seconds:
            recent_freeze_events.append((item, freeze_age))

    if recent_freeze_events:
        latest_freeze, latest_freeze_age = recent_freeze_events[-1]
        latest_freeze_ts = parse_timestamp(latest_freeze.get("generated_at"))
        alerts.append(
            {
                "code": "tod_freeze_suspected",
                "severity": "warning",
                "source": "tod_liveness_events",
                "detail": "Repeated TOD freeze_suspected liveness events are present.",
                "context": {
                    "recent_count": len(recent_freeze_events),
                    "latest_age_seconds": latest_freeze_age,
                    "latest_stale_seconds": latest_freeze.get("stale_seconds"),
                },
            }
        )

    severity_rank = {"none": 0, "info": 1, "warning": 2, "critical": 3}
    highest = "none"
    for alert in alerts:
        sev = str(alert.get("severity") or "none").strip().lower()
        if severity_rank.get(sev, 0) > severity_rank.get(highest, 0):
            highest = sev

    primary_alert = alerts[0] if alerts else {}
    return {
        "generated_at": reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "mim_system_alerts_v1",
        "active": bool(alerts),
        "highest_severity": highest,
        "primary_alert": primary_alert,
        "alerts": alerts,
    }


def reconcile_system_alert_summary_for_review(
    *,
    system_alert_summary: dict | None,
    review: dict | None,
) -> dict:
    system_alert_summary = _as_dict(system_alert_summary)
    review = _as_dict(review)
    task = _as_dict(review.get("task"))

    active_task_id = _first_text(task, "active_task_id")
    trigger_ack_task_id = _first_text(task, "trigger_ack_task_id")
    request_request_id = _first_text(task, "request_request_id")
    task_ack_request_id = _first_text(task, "task_ack_request_id")
    result_request_id = _first_text(task, "result_request_id")
    result_task_id = _first_text(task, "result_task_id")
    blocking_reason_codes = {
        str(item).strip()
        for item in review.get("blocking_reason_codes", [])
        if str(item).strip()
    }

    alerts = [
        dict(item)
        for item in system_alert_summary.get("alerts", [])
        if isinstance(item, dict)
    ]
    if not alerts:
        return {
            "generated_at": str(system_alert_summary.get("generated_at") or ""),
            "type": str(system_alert_summary.get("type") or "mim_system_alerts_v1"),
            "active": False,
            "highest_severity": "none",
            "primary_alert": {},
            "alerts": [],
        }

    stale_trigger_alert_cleared = bool(
        active_task_id
        and (
            trigger_ack_task_id == active_task_id
            or _matches_current_request_identity(
                task_ack_request_id,
                active_task_id=active_task_id,
                request_request_id=request_request_id,
            )
            or result_task_id == active_task_id
            or _matches_current_request_identity(
                result_request_id,
                active_task_id=active_task_id,
                request_request_id=request_request_id,
            )
        )
        and "trigger_ack_not_current" not in blocking_reason_codes
    )
    if stale_trigger_alert_cleared:
        alerts = [
            item
            for item in alerts
            if _first_text(item, "code") != "stale_trigger_ack_failures"
        ]

    severity_rank = {"none": 0, "info": 1, "warning": 2, "critical": 3}
    highest = "none"
    for alert in alerts:
        sev = str(alert.get("severity") or "none").strip().lower()
        if severity_rank.get(sev, 0) > severity_rank.get(highest, 0):
            highest = sev

    return {
        "generated_at": str(system_alert_summary.get("generated_at") or ""),
        "type": str(system_alert_summary.get("type") or "mim_system_alerts_v1"),
        "active": bool(alerts),
        "highest_severity": highest,
        "primary_alert": alerts[0] if alerts else {},
        "alerts": alerts,
    }


def build_task_status_review(
    *,
    task_request: dict | None,
    trigger: dict | None,
    trigger_ack: dict | None,
    task_ack: dict | None,
    task_result: dict | None,
    catchup_gate: dict | None,
    troubleshooting_authority: dict | None,
    persistent_task: dict | None,
    formal_program_response: dict | None = None,
    system_alert_summary: dict | None = None,
    idle_seconds: int = 120,
    emergency_timeout_seconds: int = DEFAULT_TOD_SILENCE_EMERGENCY_SECONDS,
    direct_execution_timeout_seconds: int = DEFAULT_TOD_DIRECT_EXECUTION_SECONDS,
    now: datetime | None = None,
) -> dict:
    reference = now or datetime.now(timezone.utc)
    task_request = _as_dict(task_request)
    trigger = _as_dict(trigger)
    trigger_ack = _as_dict(trigger_ack)
    task_ack = _as_dict(task_ack)
    task_result = _as_dict(task_result)
    catchup_gate = _as_dict(catchup_gate)
    troubleshooting_authority = _as_dict(troubleshooting_authority)
    persistent_task = _as_dict(persistent_task)
    formal_program_response = _as_dict(formal_program_response)
    system_alert_summary = _as_dict(system_alert_summary)
    persistent_task = _sanitize_persistent_task(
        persistent_task=persistent_task,
        task_request=task_request,
        trigger=trigger,
        task_result=task_result,
    )

    trigger_name = _first_text(trigger, "trigger")
    actionable_trigger = _is_task_stream_trigger(trigger)

    request_task_id = _first_text(task_request, "task_id", "request_id")
    request_request_id = _first_text(task_request, "request_id")
    trigger_task_id = _first_text(trigger, "task_id", "request_id") if actionable_trigger else ""
    trigger_ack_task_id = _trigger_ack_task_identity(trigger_ack)
    task_ack_request_id = _first_text(task_ack, "request_id", "task_id")
    result_request_id = _first_text(task_result, "request_id")
    result_task_id = _first_text(task_result, "task_id", "request_id")
    persistent_task_id = _first_text(persistent_task, "task_id", "request_id")
    supersession = detect_completed_stream_supersession(
        task_request=task_request,
        trigger=trigger,
        task_ack=task_ack,
        task_result=task_result,
    )
    authoritative_task_id = str(supersession.get("authoritative_task_id") or "").strip()
    authoritative_task_reason = str(supersession.get("authoritative_task_reason") or "").strip()
    if not authoritative_task_id:
        authoritative_task_id, authoritative_task_reason = _authoritative_task_override(
            request_task_id=request_task_id,
            trigger_task_id=trigger_task_id,
            task_ack_request_id=task_ack_request_id,
            result_request_id=result_task_id,
            task_result=task_result,
        )
    same_objective_task_mismatch = _same_objective_task_mismatch(
        request_task_id=request_task_id,
        task_request=task_request,
        trigger_task_id=trigger_task_id,
        trigger=trigger,
        task_ack_request_id=task_ack_request_id,
        task_ack=task_ack,
        result_task_id=result_task_id,
        task_result=task_result,
    )
    active_task_id = authoritative_task_id or (
        request_task_id if same_objective_task_mismatch else (trigger_task_id or request_task_id)
    ) or persistent_task_id
    formal_program_lane = _formal_program_active_lane(formal_program_response)

    persistent_status = _first_text(persistent_task, "status").lower()
    result_status = _first_text(task_result, "status").lower()
    current_processing_task_id = _bridge_current_processing_task_id(task_result)
    result_review_current = _result_review_is_current(
        task_result=task_result,
        active_task_id=active_task_id,
    )
    terminal_authoritative_result = bool(
        result_review_current and result_status in TERMINAL_RESULT_STATUSES
    )
    if terminal_authoritative_result and task_ack_request_id and task_ack_request_id != active_task_id:
        task_ack_request_id = ""
    execution_transport_healthy = bool(
        terminal_authoritative_result
        and (
            task_ack_request_id == active_task_id
            or current_processing_task_id == active_task_id
        )
    )
    terminal_execution_failure = bool(
        execution_transport_healthy and result_status in TERMINAL_FAILURE_STATUSES
    )
    execution_failure_reason = (
        _first_text(task_result, "result_reason_code") or "task_result_failed"
    ) if terminal_execution_failure else ""
    execution_failure_error = _first_text(task_result, "error")
    execution_failure_mode = _first_text(task_result, "execution_mode")
    task_objective = normalize_objective(
        active_task_id
        if authoritative_task_id or terminal_authoritative_result
        else (
            task_request.get("objective_id")
            or trigger.get("objective_id")
            or persistent_task.get("objective_id")
            or active_task_id
        )
    )
    formal_program_objective = str(formal_program_lane.get("objective_id") or "").strip()
    formal_program_override = bool(
        formal_program_lane.get("active") is True
        and formal_program_objective
        and (
            not task_objective
            or _objective_sort_key(formal_program_objective)
            > _objective_sort_key(task_objective)
        )
    )
    if formal_program_override:
        active_task_id = str(formal_program_lane.get("task_id") or "").strip() or f"objective-{formal_program_lane.get('objective_id')}-formal-program-active"
        authoritative_task_id = active_task_id
        authoritative_task_reason = "formal_program_active_lane"
        request_task_id = ""
        trigger_task_id = ""
        trigger_ack_task_id = ""
        task_ack_request_id = ""
        result_request_id = ""
        result_status = ""
        persistent_task_id = ""
        persistent_status = ""
        trigger_name = ""
        actionable_trigger = False
        terminal_authoritative_result = False
        execution_transport_healthy = False
        terminal_execution_failure = False
        execution_failure_reason = ""
        execution_failure_error = ""
        execution_failure_mode = ""
        task_objective = str(formal_program_lane.get("objective_id") or "").strip()

    gate_pass = bool(
        catchup_gate.get("promotion_ready") is True
        or catchup_gate.get("gate_pass") is True
    )

    authority = troubleshooting_authority.get("authority")
    authority = authority if isinstance(authority, dict) else {}
    mim_permissions = authority.get("mim") if isinstance(authority.get("mim"), dict) else {}
    tod_permissions = authority.get("tod") if isinstance(authority.get("tod"), dict) else {}
    mim_permission_set = {str(item).strip().lower() for item in mim_permissions.get("permissions", []) if str(item).strip()}
    tod_permission_set = {str(item).strip().lower() for item in tod_permissions.get("permissions", []) if str(item).strip()}
    enforcement = troubleshooting_authority.get("enforcement")
    enforcement = enforcement if isinstance(enforcement, dict) else {}
    authority_ok = {"read", "write"}.issubset(mim_permission_set) and {"read", "write"}.issubset(tod_permission_set)
    access_failure_action = _first_text(enforcement, "access_failure_action").lower()
    authority_reason_code = _first_text(enforcement, "reason_code") or "troubleshooting_access_denied"

    request_age = artifact_age_seconds(payload=task_request, now=reference)
    trigger_age = artifact_age_seconds(payload=trigger, now=reference)
    trigger_ack_age = artifact_age_seconds(payload=trigger_ack, now=reference)
    task_ack_age = artifact_age_seconds(payload=task_ack, now=reference)
    result_age = artifact_age_seconds(payload=task_result, now=reference)

    blocking_reason_codes: list[str] = []
    pending_actions: list[dict[str, str]] = []

    def add_reason(code: str) -> None:
        if code and code not in blocking_reason_codes:
            blocking_reason_codes.append(code)

    def add_action(code: str, detail: str) -> None:
        if code and all(existing.get("code") != code for existing in pending_actions):
            pending_actions.append({"code": code, "detail": detail})

    completed_stream_superseded = bool(supersession.get("active") is True)

    if terminal_execution_failure:
        add_reason(execution_failure_reason)
        if "outofmemoryexception" in execution_failure_error.lower():
            add_reason("executor_memory_pressure")
        detail = (
            "Treat the communication lane as healthy and remediate TOD executor stability before publishing more work."
        )
        if execution_failure_mode or execution_failure_error:
            mode_detail = execution_failure_mode or "terminal_failure"
            error_detail = execution_failure_error or "executor failure"
            detail = (
                f"Treat the communication lane as healthy and remediate TOD executor stability before publishing more work. "
                f"TOD reported {mode_detail}: {error_detail}"
            )
        add_action("remediate_tod_executor_failure", detail)

    if (
        not terminal_execution_failure
        and request_task_id
        and trigger_task_id
        and request_task_id != trigger_task_id
    ):
        if completed_stream_superseded:
            add_action(
                "stabilize_task_stream",
                "Stop rotating publishers from overwriting the active task packet and reissue request and trigger with one authoritative task_id.",
            )
        else:
            add_reason("task_stream_drift")
            add_action(
                "stabilize_task_stream",
                "Stop rotating publishers from overwriting the active task packet and reissue request and trigger with one authoritative task_id.",
            )

    if completed_stream_superseded:
        add_action(
            "stabilize_publisher_after_completion",
            "MIM request/trigger artifacts were overwritten after TOD completed the authoritative task; stabilize local publishers and do not restart the TOD trigger ACK bridge for this completed lane.",
        )

    if (
        completed_stream_superseded
        and request_task_id
        and _first_text(trigger, "trigger").lower() == "coordination_ack_posted"
        and _first_text(trigger, "task_id", "request_id")
        and _first_text(trigger, "task_id", "request_id") != request_task_id
    ):
        add_action(
            "stabilize_task_stream",
            "Stop rotating publishers from overwriting the active task packet and reissue request and trigger with one authoritative task_id.",
        )

    if not terminal_execution_failure and not authority_ok and access_failure_action == "no_go":
        add_reason(authority_reason_code)
        add_action(
            "restore_troubleshooting_authority",
            "Restore read and write access for both MIM and TOD on the shared troubleshooting artifacts before continuing dispatch.",
        )

    if not terminal_execution_failure and not gate_pass:
        add_reason("catchup_gate_blocked")
        add_action(
            "pass_dispatch_readiness_gate",
            "Recover the TOD bridge and satisfy the two-cycle ACK mutation gate before reissuing critical work.",
        )

    highest_alert_severity = str(
        system_alert_summary.get("highest_severity") or "none"
    ).strip().lower()
    primary_alert = _as_dict(system_alert_summary.get("primary_alert"))
    primary_alert_code = _first_text(primary_alert, "code")
    stale_trigger_alert_cleared = bool(
        primary_alert_code == "stale_trigger_ack_failures"
        and active_task_id
        and (
            trigger_ack_task_id == active_task_id
            or task_ack_request_id == active_task_id
            or result_request_id == active_task_id
            or result_task_id == active_task_id
        )
    )
    if (
        highest_alert_severity == "critical"
        and not terminal_execution_failure
        and not stale_trigger_alert_cleared
    ):
        add_reason("system_alert_critical")
        primary_code = primary_alert_code or "critical_alert"
        primary_detail = _first_text(primary_alert, "detail") or "Critical system alert active"
        add_action(
            "acknowledge_and_remediate_system_alerts",
            f"Resolve critical system alert '{primary_code}' before continuing dispatch. Detail: {primary_detail}",
        )

    if (
        primary_alert_code == "publisher_objective_mismatch"
        and not terminal_execution_failure
    ):
        add_reason("publisher_objective_mismatch")
        canonical_objective = str(primary_alert.get("context", {}).get("canonical_objective") or "").strip()
        live_task_objective = str(primary_alert.get("context", {}).get("live_task_objective") or "").strip()
        detail = (
            "Rebuild TOD integration status from canonical MIM export and republish the active task lane only if it still matches canonical objective truth."
        )
        if canonical_objective and live_task_objective:
            detail = (
                f"Canonical MIM truth is objective {canonical_objective}, but the live task lane still references objective {live_task_objective}. "
                "Rebuild TOD integration status from canonical MIM export, then reissue the active task lane only if it matches canonical objective truth."
            )
        add_action("recouple_publisher_objective", detail)

    current_task_ack = _matches_current_request_identity(
        task_ack_request_id,
        active_task_id=active_task_id,
        request_request_id=request_request_id,
    )
    current_result = bool(result_task_id and result_task_id == active_task_id)

    if (
        active_task_id
        and actionable_trigger
        and trigger_ack_task_id != active_task_id
        and not terminal_authoritative_result
        and not completed_stream_superseded
    ):
        add_reason("trigger_ack_not_current")
        add_action(
            "recover_trigger_ack_bridge",
            "Restart or recover the TOD listener bridge so TOD_TO_MIM_TRIGGER_ACK.latest.json mutates for the current task_id across two consecutive cycles.",
        )

    if (
        active_task_id
        and task_ack_request_id
        and not current_task_ack
        and trigger_ack_task_id != active_task_id
        and not terminal_authoritative_result
        and not completed_stream_superseded
    ):
        add_reason("task_ack_request_mismatch")
        add_action(
            "reissue_task_with_matching_ack",
            "Reissue the active task and require TOD_MIM_TASK_ACK.latest.json request_id to exactly match the current task_id.",
        )

    if active_task_id and result_task_id and result_task_id != active_task_id:
        add_reason("task_result_request_mismatch")
        add_action(
            "reissue_task_with_matching_result",
            "Reissue the active task and require TOD_MIM_TASK_RESULT.latest.json task_id to exactly match the current task_id.",
        )

    if active_task_id and trigger_ack_task_id == active_task_id and not task_ack_request_id:
        add_action(
            "wait_for_task_ack",
            "TOD has acknowledged the trigger but has not yet published TOD_MIM_TASK_ACK.latest.json for the active task.",
        )

    if active_task_id and current_task_ack and not result_task_id:
        add_action(
            "wait_for_task_result",
            "TOD has accepted the task but has not yet published TOD_MIM_TASK_RESULT.latest.json for the active task.",
        )

    review_state = "no_active_task"
    if active_task_id:
        review_state = "queued"
    if formal_program_override:
        review_state = "working"
    if result_review_current and result_status in TERMINAL_SUCCESS_STATUSES:
        review_state = "completed"
    elif result_review_current and result_status in TERMINAL_FAILURE_STATUSES:
        review_state = "failed"
    elif current_task_ack:
        review_state = "awaiting_result"
    elif trigger_ack_task_id == active_task_id:
        review_state = "awaiting_task_ack"
    elif actionable_trigger and active_task_id:
        review_state = "awaiting_trigger_ack"

    latest_progress_age_candidates = [
        age
        for age in (
            result_age if result_review_current else None,
            task_ack_age if current_task_ack else None,
            trigger_ack_age if trigger_ack_task_id == active_task_id else None,
            trigger_age if trigger_task_id == active_task_id else None,
            request_age if request_task_id == active_task_id else None,
        )
        if isinstance(age, int)
    ]
    latest_progress_age = min(latest_progress_age_candidates) if latest_progress_age_candidates else None
    idle_active = bool(
        review_state in {"queued", "awaiting_trigger_ack", "awaiting_task_ack", "awaiting_result"}
        and isinstance(latest_progress_age, int)
        and latest_progress_age >= idle_seconds
    )
    silence_emergency_active = bool(
        review_state in {"queued", "awaiting_trigger_ack", "awaiting_task_ack", "awaiting_result"}
        and isinstance(latest_progress_age, int)
        and latest_progress_age >= max(1, int(emergency_timeout_seconds))
        and not terminal_authoritative_result
        and primary_alert_code != "publisher_objective_mismatch"
    )
    direct_execution_ready = bool(
        silence_emergency_active
        and isinstance(latest_progress_age, int)
        and latest_progress_age >= max(
            int(emergency_timeout_seconds), int(direct_execution_timeout_seconds)
        )
    )

    if persistent_status == "queued" and not actionable_trigger and not request_task_id:
        add_reason("queued_not_dispatched")
        add_action(
            "dispatch_queued_task",
            "Publish the queued persistent TOD task into the shared request and trigger artifacts so execution can begin.",
        )

    if silence_emergency_active:
        add_reason("tod_silence_emergency")
        emergency_detail = (
            f"TOD has been silent on the active task for at least {int(emergency_timeout_seconds)} seconds. "
            "Escalate the lane as an emergency, stop passive waiting, and force bounded recovery or fallback execution."
        )
        if active_task_id and isinstance(latest_progress_age, int):
            emergency_detail = (
                f"TOD has been silent on {active_task_id} for {latest_progress_age} seconds. "
                "Escalate the lane as an emergency, stop passive waiting, and force bounded recovery or fallback execution."
            )
        add_action("declare_tod_emergency", emergency_detail)

    if direct_execution_ready:
        add_reason("tod_silence_direct_execution_ready")
        direct_execution_detail = (
            f"TOD silence has persisted beyond {int(direct_execution_timeout_seconds)} seconds. "
            "Submit the bounded task to the local Codex/OpenAI handoff path, complete the work directly if possible, and publish MIM-side status and error artifacts."
        )
        if active_task_id and isinstance(latest_progress_age, int):
            direct_execution_detail = (
                f"TOD silence on {active_task_id} has persisted for {latest_progress_age} seconds. "
                "Submit the bounded task to the local Codex/OpenAI handoff path, complete the work directly if possible, and publish MIM-side status and error artifacts."
            )
        add_action("fallback_to_codex_direct_execution", direct_execution_detail)

    lineage_mismatch_reason_codes = {
        "task_stream_drift",
        "task_ack_request_mismatch",
        "task_result_request_mismatch",
        "trigger_ack_not_current",
    }
    silence_reason_codes = {
        "tod_silence_emergency",
        "tod_silence_direct_execution_ready",
    }
    lineage_mismatch_dispatch_blocked = bool(
        (
            same_objective_task_mismatch
            or bool(blocking_reason_codes)
            and set(blocking_reason_codes).issubset(
                lineage_mismatch_reason_codes | silence_reason_codes
            )
        )
        and review_state in {"queued", "awaiting_trigger_ack", "awaiting_task_ack", "awaiting_result"}
        and any(code in lineage_mismatch_reason_codes for code in blocking_reason_codes)
    )

    if idle_active and blocking_reason_codes and not lineage_mismatch_dispatch_blocked:
        review_state = "idle_blocked"
    elif blocking_reason_codes and review_state in {"queued", "awaiting_trigger_ack", "awaiting_task_ack", "awaiting_result"}:
        review_state = "dispatch_blocked"

    primary_reason = blocking_reason_codes[0] if blocking_reason_codes else ""
    state_reason = {
        "completed": "task_result_current",
        "failed": execution_failure_reason or primary_reason or "task_result_failed",
        "awaiting_result": primary_reason or "tod_task_ack_current",
        "awaiting_task_ack": primary_reason or "trigger_ack_current",
        "awaiting_trigger_ack": primary_reason or "trigger_emitted_waiting_for_ack",
        "queued": primary_reason or ("persistent_task_queued" if persistent_status == "queued" else "task_created"),
        "working": primary_reason or "formal_program_active_lane",
        "dispatch_blocked": primary_reason or "dispatch_blocked",
        "idle_blocked": primary_reason or "idle_without_progress",
        "no_active_task": primary_reason or "no_task_detected",
    }.get(review_state, primary_reason)

    return {
        "generated_at": reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "mim_task_status_review_v1",
        "task": {
            "active_task_id": active_task_id,
            "objective_id": task_objective,
            "request_task_id": request_task_id,
            "request_request_id": request_request_id,
            "trigger_task_id": trigger_task_id,
            "trigger_ack_task_id": trigger_ack_task_id,
            "task_ack_request_id": task_ack_request_id,
            "result_request_id": result_request_id,
            "result_task_id": result_task_id,
            "result_review_current": result_review_current,
            "authoritative_task_id": authoritative_task_id,
            "authoritative_task_reason": authoritative_task_reason,
            "persistent_task_id": persistent_task_id,
            "persistent_status": persistent_status,
            "result_status": result_status,
            "trigger_name": trigger_name,
            "same_objective_task_mismatch": same_objective_task_mismatch,
        },
        "state": review_state,
        "state_reason": state_reason,
        "blocking_reason_codes": blocking_reason_codes,
        "idle": {
            "active": idle_active,
            "threshold_seconds": int(idle_seconds),
            "latest_progress_age_seconds": latest_progress_age,
            "emergency_active": silence_emergency_active,
            "emergency_threshold_seconds": int(emergency_timeout_seconds),
            "direct_execution_ready": direct_execution_ready,
            "direct_execution_threshold_seconds": int(direct_execution_timeout_seconds),
        },
        "gate": {
            "pass": gate_pass,
            "promotion_ready": bool(catchup_gate.get("promotion_ready") is True),
        },
        "authority": {
            "ok": authority_ok,
            "access_failure_action": access_failure_action,
            "reason_code": authority_reason_code,
        },
        "artifacts": {
            "request_age_seconds": request_age,
            "trigger_age_seconds": trigger_age,
            "trigger_ack_age_seconds": trigger_ack_age,
            "task_ack_age_seconds": task_ack_age,
            "result_age_seconds": result_age,
        },
        "pending_actions": pending_actions,
        "formal_program": {
            "active": bool(formal_program_lane.get("active") is True),
            "override_applied": formal_program_override,
            "objective_id": str(formal_program_lane.get("objective_id") or "").strip(),
            "task_id": str(formal_program_lane.get("task_id") or "").strip(),
            "task_title": str(formal_program_lane.get("task_title") or "").strip(),
            "project_id": str(formal_program_lane.get("project_id") or "").strip(),
            "execution_state": str(formal_program_lane.get("execution_state") or "").strip(),
            "generated_at": str(formal_program_lane.get("generated_at") or "").strip(),
        },
        "system_alerts": {
            "active": bool(system_alert_summary.get("active") is True),
            "highest_severity": highest_alert_severity,
            "primary_alert_code": _first_text(primary_alert, "code"),
        },
    }


def build_mim_tod_decision_snapshot(
    *,
    review: dict | None,
    next_action: dict | None,
    system_alert_summary: dict | None,
    coordination_request: dict | None,
    coordination_ack: dict | None,
    ping_response: dict | None,
    console_probe: dict | None,
    now: datetime | None = None,
    tod_console_url: str = "http://192.168.1.161:8844",
) -> dict:
    reference = now or datetime.now(timezone.utc)
    review = _as_dict(review)
    next_action = _as_dict(next_action)
    system_alert_summary = _as_dict(system_alert_summary)
    coordination_request = _as_dict(coordination_request)
    coordination_ack = _as_dict(coordination_ack)
    ping_response = _as_dict(ping_response)
    console_probe = _as_dict(console_probe)

    task = _as_dict(review.get("task"))
    idle = _as_dict(review.get("idle"))
    selected_action = _as_dict(next_action.get("selected_action"))
    primary_alert = _as_dict(system_alert_summary.get("primary_alert"))
    blocking_reason_codes = [
        str(item).strip()
        for item in review.get("blocking_reason_codes", [])
        if str(item).strip()
    ]

    active_task_id = _first_text(task, "active_task_id")
    objective_id = normalize_objective(task.get("objective_id") or active_task_id)
    state = _first_text(review, "state")
    state_reason = _first_text(review, "state_reason")
    trigger_name = _first_text(task, "trigger_name")
    trigger_ack_task_id = _first_text(task, "trigger_ack_task_id")
    request_request_id = _first_text(task, "request_request_id")
    task_ack_request_id = _first_text(task, "task_ack_request_id")
    result_request_id = _first_text(task, "result_request_id")
    result_task_id = _first_text(task, "result_task_id")
    result_review_current = bool(task.get("result_review_current") is True)
    coordination_request_id = _first_text(coordination_request, "request_id", "task_id")
    coordination_ack_id = _first_text(coordination_ack, "request_id", "task_id")
    ping_status = _first_text(ping_response, "heartbeat_status", "status").lower()
    highest_severity = _first_text(system_alert_summary, "highest_severity").lower() or "none"
    latest_progress_age_seconds = idle.get("latest_progress_age_seconds")
    ping_age_seconds = artifact_age_seconds(payload=ping_response, now=reference)
    console_probe_age_seconds = artifact_age_seconds(payload=console_probe, now=reference)
    console_probe_status = _first_text(console_probe, "status").lower()
    console_probe_http_status = console_probe.get("http_status")

    tod_has_current_task_evidence = []
    if active_task_id and trigger_ack_task_id == active_task_id:
        tod_has_current_task_evidence.append("trigger_ack_current")
    if _matches_current_request_identity(
        task_ack_request_id,
        active_task_id=active_task_id,
        request_request_id=request_request_id,
    ):
        tod_has_current_task_evidence.append("task_ack_current")
    if result_review_current and active_task_id and result_task_id == active_task_id:
        tod_has_current_task_evidence.append("task_result_current")
    if active_task_id and coordination_ack_id == active_task_id:
        tod_has_current_task_evidence.append("coordination_ack_current")

    mim_has_tod_activity_evidence = []
    if _matches_current_request_identity(
        task_ack_request_id,
        active_task_id=active_task_id,
        request_request_id=request_request_id,
    ):
        mim_has_tod_activity_evidence.append("tod_task_ack_current")
    if result_review_current and active_task_id and result_task_id == active_task_id:
        mim_has_tod_activity_evidence.append("tod_task_result_current")
    if coordination_request_id:
        mim_has_tod_activity_evidence.append("tod_coordination_request_seen")
    if ping_age_seconds is not None and ping_age_seconds <= 90 and ping_status in {"alive", "degraded", "ok"}:
        mim_has_tod_activity_evidence.append("tod_ping_response_recent")
    if (
        console_probe_age_seconds is not None
        and console_probe_age_seconds <= 180
        and console_probe_status == "reachable"
    ):
        mim_has_tod_activity_evidence.append("tod_console_probe_recent")

    tod_knows_mim_did = bool(tod_has_current_task_evidence)
    mim_knows_tod_did = bool(mim_has_tod_activity_evidence)

    tod_work_phase = {
        "awaiting_trigger_ack": "tod_has_not_confirmed_observation",
        "awaiting_task_ack": "tod_has_seen_request_waiting_acceptance",
        "awaiting_result": "tod_is_working_or_finishing",
        "working": "formal_program_active_lane",
        "completed": "tod_published_terminal_result",
        "failed": "tod_published_terminal_failure",
        "idle_blocked": "tod_progress_uncertain_blocked",
        "dispatch_blocked": "dispatch_blocked_before_safe_work_start",
        "queued": "mim_has_work_queued",
        "no_active_task": "no_active_tod_task_detected",
    }.get(state, "unknown")

    tod_work_known = bool(active_task_id or coordination_request_id or coordination_ack_id)
    tod_work_detail = ""
    if active_task_id:
        tod_work_detail = f"review_state={state or 'unknown'} trigger={trigger_name or 'none'}"
    elif coordination_request_id:
        tod_work_detail = "TOD coordination request observed without active task review"
    elif coordination_ack_id:
        tod_work_detail = "TOD coordination ack observed without active task review"

    silence_reasons = {
        "trigger_ack_not_current",
        "consume_watch_timeout",
        "catchup_gate_blocked",
        "system_alert_critical",
    }
    silence_detected = bool(
        state in {"idle_blocked", "dispatch_blocked"}
        and (
            any(code in silence_reasons for code in blocking_reason_codes)
            or highest_severity in {"critical", "warning"}
            or (isinstance(latest_progress_age_seconds, int) and latest_progress_age_seconds >= 120)
        )
        and not mim_knows_tod_did
    )
    degraded_detected = bool(
        not silence_detected
        and state in {"idle_blocked", "dispatch_blocked", "awaiting_trigger_ack", "awaiting_task_ack"}
        and (
            any(code in silence_reasons for code in blocking_reason_codes)
            or highest_severity == "warning"
            or (isinstance(latest_progress_age_seconds, int) and latest_progress_age_seconds >= 60)
        )
    )
    emergency_required = bool(idle.get("emergency_active") is True)
    direct_execution_ready = bool(idle.get("direct_execution_ready") is True)

    liveness_status = "alive"
    if direct_execution_ready:
        liveness_status = "direct_execution_ready"
    elif emergency_required:
        liveness_status = "emergency"
    elif silence_detected:
        liveness_status = "silent"
    elif degraded_detected:
        liveness_status = "degraded"
    elif state in {"completed", "failed"}:
        liveness_status = "terminal"

    escalation_required = liveness_status in {"silent", "degraded"} and state not in {"completed", "failed"}
    if liveness_status in {"emergency", "direct_execution_ready"} and state not in {"completed", "failed"}:
        escalation_required = True
    escalation_code = "monitor_only"
    escalation_detail = "Keep observing the current TOD lane."
    if liveness_status == "direct_execution_ready":
        escalation_code = "fallback_to_codex_direct_execution"
        escalation_detail = (
            "TOD has remained silent past the direct-execution threshold. "
            "Stop waiting on TOD, submit the bounded task to the local Codex/OpenAI handoff path, and publish MIM-side status and error artifacts."
        )
    elif liveness_status == "emergency":
        escalation_code = "declare_tod_emergency"
        escalation_detail = (
            "TOD has exceeded the emergency silence threshold. "
            "Treat the lane as an emergency, recover the bridge immediately, and prepare direct execution fallback if silence continues."
        )
    elif liveness_status == "silent":
        escalation_code = "ask_tod_status_loudly"
        escalation_detail = (
            "TOD is not providing current ACK, RESULT, coordination, or fresh ping evidence. "
            "Emit a liveness ask, verify the shared trigger path, and use the TOD console if recovery evidence does not appear."
        )
    elif liveness_status == "degraded":
        escalation_code = "verify_tod_progress"
        escalation_detail = (
            "TOD progress evidence is weak or aging. Verify current task ownership, confirm liveness, and recover the bridge before sending more work."
        )

    return {
        "generated_at": reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "mim_tod_decision_snapshot_v1",
        "owner_actor": "MIM",
        "target_actor": "TOD",
        "active_task_id": active_task_id,
        "objective_id": objective_id,
        "state": state,
        "state_reason": state_reason,
        "questions": {
            "tod_knows_what_mim_did": {
                "known": tod_knows_mim_did,
                "evidence": tod_has_current_task_evidence,
                "detail": (
                    "TOD has current-task evidence."
                    if tod_knows_mim_did
                    else "MIM has not observed current-task TOD evidence yet."
                ),
            },
            "mim_knows_what_tod_did": {
                "known": mim_knows_tod_did,
                "evidence": mim_has_tod_activity_evidence,
                "detail": (
                    "MIM has recent TOD-side evidence."
                    if mim_knows_tod_did
                    else "MIM lacks fresh TOD-side evidence and should verify liveness/progress."
                ),
            },
            "tod_current_work": {
                "known": tod_work_known,
                "task_id": active_task_id,
                "objective_id": objective_id,
                "phase": tod_work_phase,
                "detail": tod_work_detail,
            },
            "tod_liveness": {
                "status": liveness_status,
                "ping_response_age_seconds": ping_age_seconds,
                "latest_progress_age_seconds": latest_progress_age_seconds,
                "console_probe_age_seconds": console_probe_age_seconds,
                "console_probe_status": console_probe_status,
                "console_probe_http_status": console_probe_http_status,
                "ask_required": escalation_required,
                "primary_alert_code": _first_text(primary_alert, "code"),
            },
        },
        "communication_escalation": {
            "required": escalation_required,
            "code": escalation_code,
            "detail": escalation_detail,
            "supplemental_console_probe": {
                "authoritative": False,
                "status": console_probe_status,
                "age_seconds": console_probe_age_seconds,
                "http_status": console_probe_http_status,
            },
            "emergency_threshold_seconds": int(idle.get("emergency_threshold_seconds", DEFAULT_TOD_SILENCE_EMERGENCY_SECONDS) or DEFAULT_TOD_SILENCE_EMERGENCY_SECONDS),
            "direct_execution_threshold_seconds": int(idle.get("direct_execution_threshold_seconds", DEFAULT_TOD_DIRECT_EXECUTION_SECONDS) or DEFAULT_TOD_DIRECT_EXECUTION_SECONDS),
            "trigger_artifact": "MIM_TO_TOD_TRIGGER.latest.json",
            "ping_artifact": "MIM_TO_TOD_PING.latest.json",
            "response_artifact": "TOD_TO_MIM_PING.latest.json",
            "console_url": tod_console_url.strip(),
            "kick_hint": (
                "Use the shared trigger/ping lane first; if TOD stays silent, inspect the TOD console and recover the TOD-side listener/executor."
                if escalation_required
                else "No TOD kick required right now."
            ),
        },
        "selected_action": {
            "code": _first_text(selected_action, "code") or "monitor_only",
            "detail": _first_text(selected_action, "detail") or "No blocking action selected; continue monitoring.",
        },
        "blocking_reason_codes": blocking_reason_codes,
    }


def build_publisher_warning(
    *,
    integration: dict | None,
    context_export: dict | None,
    handshake: dict | None,
    task_request: dict | None,
    coordination_ack: dict | None,
) -> dict:
    integration = integration if isinstance(integration, dict) else {}
    context_export = context_export if isinstance(context_export, dict) else {}
    handshake = handshake if isinstance(handshake, dict) else {}
    task_request = task_request if isinstance(task_request, dict) else {}
    coordination_ack = coordination_ack if isinstance(coordination_ack, dict) else {}

    handshake_truth = (
        handshake.get("truth") if isinstance(handshake.get("truth"), dict) else {}
    )
    mim_refresh = (
        integration.get("mim_refresh")
        if isinstance(integration.get("mim_refresh"), dict)
        else {}
    )
    mim_status = (
        integration.get("mim_status")
        if isinstance(integration.get("mim_status"), dict)
        else {}
    )
    published_handshake = (
        integration.get("mim_handshake")
        if isinstance(integration.get("mim_handshake"), dict)
        else {}
    )
    integrated_live_task = (
        integration.get("live_task_request")
        if isinstance(integration.get("live_task_request"), dict)
        else {}
    )

    canonical_objective = normalize_objective(
        context_export.get("objective_active")
        or handshake_truth.get("objective_active")
        or published_handshake.get("objective_active")
        or mim_status.get("objective_active")
    )
    canonical_release_tag = str(
        context_export.get("release_tag")
        or handshake_truth.get("release_tag")
        or published_handshake.get("release_tag")
        or ""
    ).strip()
    live_task_objective = normalize_objective(
        integrated_live_task.get("normalized_objective_id")
        or integrated_live_task.get("objective_id")
        or task_request.get("objective_id")
        or task_request.get("task_id")
    )
    coordination_objective = normalize_objective(
        coordination_ack.get("objective_id") or coordination_ack.get("task_id")
    )
    task_id = str(task_request.get("task_id") or "").strip()
    source_service = str(task_request.get("source_service") or "").strip()
    source_instance_id = str(task_request.get("source_instance_id") or "").strip()

    refresh_healthy = (
        str(mim_refresh.get("failure_reason") or "").strip() == ""
        and mim_refresh.get("attempted") is True
        and mim_refresh.get("copied_json") is True
        and mim_refresh.get("copied_yaml") is True
        and mim_refresh.get("copied_manifest") is True
    )
    shared_root = str(
        mim_refresh.get("resolved_source_root")
        or mim_refresh.get("ssh_remote_root")
        or ""
    ).strip()
    stale_publisher_service = bool(
        source_service
        and canonical_objective
        and canonical_objective not in source_service
    )
    mismatch_active = bool(
        refresh_healthy
        and canonical_objective
        and live_task_objective
        and canonical_objective != live_task_objective
    )

    message = ""
    if mismatch_active:
        message = (
            f"Remote shared root is healthy, canonical MIM export truth is objective {canonical_objective}, "
            f"but the live MIM task stream still emits objective {live_task_objective}."
        )

    return {
        "active": mismatch_active,
        "severity": "warning" if mismatch_active else "none",
        "code": "publisher_objective_mismatch" if mismatch_active else "",
        "message": message,
        "shared_root_healthy": refresh_healthy,
        "shared_root": shared_root,
        "canonical_objective_active": canonical_objective,
        "canonical_release_tag": canonical_release_tag,
        "live_task_objective": live_task_objective,
        "live_task_id": task_id,
        "live_source_service": source_service,
        "live_source_instance_id": source_instance_id,
        "coordination_objective": coordination_objective,
        "stale_publisher_service": stale_publisher_service,
        "hint": (
            "Restart or rotate the upstream MIM task publisher so live task packets match canonical export truth."
            if mismatch_active
            else ""
        ),
    }
