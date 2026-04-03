#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from pathlib import Path


TERMINAL_SUCCESS_STATUSES = {"completed", "succeeded", "approved", "done"}
TERMINAL_FAILURE_STATUSES = {"failed", "blocked", "rejected", "cancelled", "canceled"}
TERMINAL_RESULT_STATUSES = TERMINAL_SUCCESS_STATUSES | TERMINAL_FAILURE_STATUSES
ACTIVE_OPERATOR_INCIDENT_PRECEDENCE = "prefer_incident_over_latest"


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


def _bridge_current_processing_task_id(task_result: dict) -> str:
    bridge_runtime = _as_dict(task_result.get("bridge_runtime"))
    current_processing = _as_dict(bridge_runtime.get("current_processing"))
    return _first_text(current_processing, "task_id", "request_id")


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

    if authoritative_processing and stale_marker_present and (stale_request_matches or stale_trigger_matches):
        return result_request_id, "task_result_marked_prior_request_stale"

    if (
        result_status in TERMINAL_RESULT_STATUSES
        and ack_and_result_agree
        and active_processing_matches
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
    actionable_trigger = trigger_name not in {"", "liveness_ping"}

    request_task_id = _first_text(task_request, "task_id", "request_id")
    trigger_task_id = _first_text(trigger, "task_id", "request_id") if actionable_trigger else ""
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
    trigger_superseded = bool(trigger_task_id and trigger_task_id != authoritative_task_id)
    stale_request_matches = stale_request_task_id in {request_task_id, trigger_task_id}
    active = bool((request_superseded or trigger_superseded) and stale_request_matches)

    return {
        "active": active,
        "authoritative_task_id": authoritative_task_id,
        "authoritative_task_reason": authoritative_task_reason,
        "request_task_id": request_task_id,
        "trigger_task_id": trigger_task_id,
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
    stale_ack_watchdog = _as_dict(stale_ack_watchdog)
    catchup_status = _as_dict(catchup_status)
    liveness_events = liveness_events if isinstance(liveness_events, list) else []

    alerts: list[dict[str, object]] = []

    if str(stale_ack_watchdog.get("status") or "").strip().lower() == "alert":
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
    system_alert_summary: dict | None = None,
    idle_seconds: int = 120,
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
    system_alert_summary = _as_dict(system_alert_summary)

    trigger_name = _first_text(trigger, "trigger")
    actionable_trigger = trigger_name not in {"", "liveness_ping"}

    request_task_id = _first_text(task_request, "task_id", "request_id")
    trigger_task_id = _first_text(trigger, "task_id", "request_id") if actionable_trigger else ""
    trigger_ack_task_id = _first_text(trigger_ack, "task_id", "request_id")
    task_ack_request_id = _first_text(task_ack, "request_id", "task_id")
    result_request_id = _first_text(task_result, "request_id", "task_id")
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
            result_request_id=result_request_id,
            task_result=task_result,
        )
    active_task_id = authoritative_task_id or trigger_task_id or request_task_id or persistent_task_id

    persistent_status = _first_text(persistent_task, "status").lower()
    result_status = _first_text(task_result, "status").lower()
    current_processing_task_id = _bridge_current_processing_task_id(task_result)
    terminal_authoritative_result = bool(
        active_task_id
        and result_request_id == active_task_id
        and result_status in TERMINAL_RESULT_STATUSES
    )
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
    if highest_alert_severity == "critical" and not terminal_execution_failure:
        add_reason("system_alert_critical")
        primary_code = _first_text(primary_alert, "code") or "critical_alert"
        primary_detail = _first_text(primary_alert, "detail") or "Critical system alert active"
        add_action(
            "acknowledge_and_remediate_system_alerts",
            f"Resolve critical system alert '{primary_code}' before continuing dispatch. Detail: {primary_detail}",
        )

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

    if active_task_id and task_ack_request_id and task_ack_request_id != active_task_id:
        add_reason("task_ack_request_mismatch")
        add_action(
            "reissue_task_with_matching_ack",
            "Reissue the active task and require TOD_MIM_TASK_ACK.latest.json request_id to exactly match the current task_id.",
        )

    if active_task_id and result_request_id and result_request_id != active_task_id:
        add_reason("task_result_request_mismatch")
        add_action(
            "reissue_task_with_matching_result",
            "Reissue the active task and require TOD_MIM_TASK_RESULT.latest.json request_id to exactly match the current task_id.",
        )

    if active_task_id and trigger_ack_task_id == active_task_id and not task_ack_request_id:
        add_action(
            "wait_for_task_ack",
            "TOD has acknowledged the trigger but has not yet published TOD_MIM_TASK_ACK.latest.json for the active task.",
        )

    if active_task_id and task_ack_request_id == active_task_id and not result_request_id:
        add_action(
            "wait_for_task_result",
            "TOD has accepted the task but has not yet published TOD_MIM_TASK_RESULT.latest.json for the active task.",
        )

    review_state = "no_active_task"
    if active_task_id:
        review_state = "queued"
    if result_request_id == active_task_id and result_status in TERMINAL_SUCCESS_STATUSES:
        review_state = "completed"
    elif result_request_id == active_task_id and result_status in TERMINAL_FAILURE_STATUSES:
        review_state = "failed"
    elif task_ack_request_id == active_task_id:
        review_state = "awaiting_result"
    elif trigger_ack_task_id == active_task_id:
        review_state = "awaiting_task_ack"
    elif actionable_trigger and active_task_id:
        review_state = "awaiting_trigger_ack"

    latest_progress_age_candidates = [
        age
        for age in (
            result_age if result_request_id == active_task_id else None,
            task_ack_age if task_ack_request_id == active_task_id else None,
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

    if persistent_status == "queued" and not actionable_trigger and not request_task_id:
        add_reason("queued_not_dispatched")
        add_action(
            "dispatch_queued_task",
            "Publish the queued persistent TOD task into the shared request and trigger artifacts so execution can begin.",
        )

    if idle_active and blocking_reason_codes:
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
            "trigger_task_id": trigger_task_id,
            "trigger_ack_task_id": trigger_ack_task_id,
            "task_ack_request_id": task_ack_request_id,
            "result_request_id": result_request_id,
            "authoritative_task_id": authoritative_task_id,
            "authoritative_task_reason": authoritative_task_reason,
            "persistent_task_id": persistent_task_id,
            "persistent_status": persistent_status,
            "result_status": result_status,
            "trigger_name": trigger_name,
        },
        "state": review_state,
        "state_reason": state_reason,
        "blocking_reason_codes": blocking_reason_codes,
        "idle": {
            "active": idle_active,
            "threshold_seconds": int(idle_seconds),
            "latest_progress_age_seconds": latest_progress_age,
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
        "system_alerts": {
            "active": bool(system_alert_summary.get("active") is True),
            "highest_severity": highest_alert_severity,
            "primary_alert_code": _first_text(primary_alert, "code"),
        },
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
        task_request.get("objective_id") or task_request.get("task_id")
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
