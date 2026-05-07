from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARED_ROOT = PROJECT_ROOT / "runtime" / "shared"
REQUEST_ARTIFACT = "MIM_TOD_TASK_REQUEST.latest.json"
ACK_ARTIFACT = "TOD_MIM_TASK_ACK.latest.json"
RESULT_ARTIFACT = "TOD_MIM_TASK_RESULT.latest.json"
TOD_INTEGRATION_ARTIFACT_CANDIDATES = (
    "TOD_INTEGRATION_STATUS.latest.json",
    "TOD_integration_status.latest.json",
)
MIM_CONTEXT_EXPORT_ARTIFACT = "MIM_CONTEXT_EXPORT.latest.json"
TOD_COORDINATION_REQUEST_ARTIFACT = "TOD_MIM_COORDINATION_REQUEST.latest.json"
MIM_TASK_STATUS_REVIEW_ARTIFACT = "MIM_TASK_STATUS_REVIEW.latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_timestamp_text(*values: object) -> str:
    candidates = []
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            candidates.append(parsed)
    if not candidates:
        return ""
    return max(candidates).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalize_objective_id(value: object) -> str:
    text = _text(value)
    if text.lower().startswith("objective-"):
        return text.split("objective-", 1)[1]
    return text


def _lineage_payload(*, request_id: object = "", task_id: object = "", objective_id: object = "", correlation_id: object = "") -> dict[str, str]:
    return {
        "request_id": _text(request_id),
        "task_id": _text(task_id),
        "objective_id": _normalize_objective_id(objective_id),
        "correlation_id": _text(correlation_id),
    }


def _has_lineage(payload: dict[str, str]) -> bool:
    return any(payload.values())


def _lineage_matches(left: dict[str, str], right: dict[str, str]) -> bool:
    for key in ("objective_id", "task_id", "request_id", "correlation_id"):
        left_value = _text(left.get(key))
        right_value = _text(right.get(key))
        if left_value and right_value and left_value != right_value:
            return False
    if left.get("task_id") and right.get("task_id"):
        return True
    if left.get("request_id") and right.get("request_id"):
        return True
    return False


def _active_lineage(
    request_payload: dict[str, object],
    ack_payload: dict[str, object],
    result_payload: dict[str, object],
) -> dict[str, str]:
    request_lineage = _lineage_payload(
        request_id=request_payload.get("request_id"),
        task_id=request_payload.get("task_id"),
        objective_id=request_payload.get("objective_id"),
        correlation_id=request_payload.get("correlation_id"),
    )
    if _has_lineage(request_lineage):
        return request_lineage

    ack_lineage = _lineage_payload(
        request_id=ack_payload.get("request_id"),
        task_id=ack_payload.get("task_id"),
        objective_id=ack_payload.get("objective_id"),
        correlation_id=ack_payload.get("correlation_id"),
    )
    result_lineage = _lineage_payload(
        request_id=result_payload.get("request_id"),
        task_id=result_payload.get("task_id"),
        objective_id=result_payload.get("objective_id"),
        correlation_id=result_payload.get("correlation_id"),
    )
    if _has_lineage(ack_lineage) and _lineage_matches(ack_lineage, result_lineage):
        return ack_lineage
    if _has_lineage(ack_lineage):
        return ack_lineage
    return result_lineage


def _lineage_mismatch_payload(
    *,
    active_lineage: dict[str, str],
    request_payload: dict[str, object],
    ack_payload: dict[str, object],
    result_payload: dict[str, object],
    review_payload: dict[str, object],
) -> dict[str, object]:
    request_generated_at = _text(request_payload.get("generated_at"))
    ack_generated_at = _text(ack_payload.get("generated_at"))
    result_generated_at = _text(result_payload.get("generated_at"))
    review_generated_at = _text(review_payload.get("generated_at"))
    return {
        "request_id": active_lineage.get("request_id") or active_lineage.get("task_id") or "",
        "task_id": active_lineage.get("task_id") or active_lineage.get("request_id") or "",
        "objective_id": active_lineage.get("objective_id") or "",
        "correlation_id": active_lineage.get("correlation_id") or "",
        "action_name": _text(request_payload.get("action_name")),
        "request_status": _text(request_payload.get("request_status")) or "recorded",
        "result_status": "rejected_lineage_mismatch",
        "result_reason": "Artifacts disagree on active request lineage; review/ack/result were not promoted.",
        "ack_status": _text(ack_payload.get("status")),
        "decision_code": "rejected_lineage_mismatch",
        "decision_detail": "MIM rejected mixed request lineage and kept the active request lineage unchanged.",
        "lineage_mismatch": True,
        "request_generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "review_generated_at": review_generated_at,
        "generated_at": _latest_timestamp_text(
            request_generated_at,
            ack_generated_at,
            result_generated_at,
            review_generated_at,
        ),
    }


def _objective_id_for_dispatch_kind(dispatch_kind: str, action_name: str) -> str:
    normalized_dispatch_kind = str(dispatch_kind or "").strip().lower()
    normalized_action_name = str(action_name or "").strip().lower()
    objective_ids = {
        "bounded_status_request": "mim-tod-status-dispatch",
        "bounded_objective_summary_request": "mim-tod-objective-summary-dispatch",
        "bounded_recent_changes_request": "mim-tod-recent-changes-dispatch",
        "bounded_warnings_summary_request": "mim-tod-warnings-summary-dispatch",
        "bounded_bridge_warning_request": "mim-tod-bridge-warning-dispatch",
        "bounded_bridge_warning_recommendation_request": "mim-tod-bridge-warning-next-step-dispatch",
    }
    objective_id = objective_ids.get(normalized_dispatch_kind, "")
    if objective_id:
        return objective_id
    if normalized_action_name:
        return f"mim-{normalized_action_name.replace('_', '-')}-dispatch"
    return "mim-tod-dispatch"


def synchronize_latest_result_artifact_from_dispatch(
    dispatch: dict[str, object] | None,
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    payload = dispatch if isinstance(dispatch, dict) else {}
    request_id = str(payload.get("request_id") or payload.get("task_id") or "").strip()
    if not request_id:
        return {}

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    action_name = str(payload.get("action_name") or "").strip()
    dispatch_kind = str(payload.get("dispatch_kind") or "").strip()
    result_reason = _compact_text(str(payload.get("result_reason") or "").strip(), 220)
    result_generated_at = str(payload.get("result_generated_at") or _utc_now()).strip() or _utc_now()

    result_payload: dict[str, object] = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": str(payload.get("task_id") or request_id).strip(),
        "objective_id": _objective_id_for_dispatch_kind(dispatch_kind, action_name),
        "action_name": action_name,
        "status": str(payload.get("result_status") or payload.get("status") or "succeeded").strip() or "succeeded",
        "result_status": str(payload.get("result_status") or payload.get("status") or "succeeded").strip() or "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": dispatch_kind,
    }

    snapshot_keys = (
        "tod_status_snapshot",
        "tod_objective_snapshot",
        "tod_recent_changes_snapshot",
        "tod_warnings_summary_snapshot",
        "tod_bridge_warning_snapshot",
        "tod_bridge_warning_recommendation_snapshot",
    )
    for key in snapshot_keys:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            result_payload[key] = value

    _write_json(shared_root / RESULT_ARTIFACT, result_payload)
    return result_payload


def _read_first_json(shared_root: Path, *artifact_names: str) -> dict[str, object]:
    for artifact_name in artifact_names:
        payload = _read_json(shared_root / artifact_name)
        if payload:
            return payload
    return {}


def _compact_text(value: object, limit: int = 160) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _current_tod_status_snapshot(*, shared_root: Path) -> dict[str, object]:
    ack_payload = _read_json(shared_root / ACK_ARTIFACT)
    result_payload = _read_json(shared_root / RESULT_ARTIFACT)

    ack_status = str(ack_payload.get("status") or "").strip()
    result_status = str(
        result_payload.get("result_status") or result_payload.get("status") or ""
    ).strip()
    result_reason = str(
        result_payload.get("result_reason") or result_payload.get("reason") or ""
    ).strip()

    summary_parts = [
        "TOD bridge accepted the bounded status check.",
    ]
    if ack_status:
        summary_parts.append(f"Previous ACK status: {ack_status}.")
    if result_status:
        summary_parts.append(f"Previous result status: {result_status}.")
    if result_reason:
        summary_parts.append(_compact_text(result_reason, 180))

    return {
        "ack_status": ack_status,
        "result_status": result_status,
        "result_reason": result_reason,
        "summary": _compact_text(" ".join(part for part in summary_parts if part), 240),
    }


def _current_tod_objective_summary_snapshot(*, shared_root: Path) -> dict[str, object]:
    integration_payload = _read_first_json(
        shared_root,
        *TOD_INTEGRATION_ARTIFACT_CANDIDATES,
    )
    context_payload = _read_json(shared_root / MIM_CONTEXT_EXPORT_ARTIFACT)

    mim_status = (
        integration_payload.get("mim_status")
        if isinstance(integration_payload.get("mim_status"), dict)
        else {}
    )
    alignment_payload = (
        integration_payload.get("objective_alignment")
        if isinstance(integration_payload.get("objective_alignment"), dict)
        else {}
    )

    blockers = [
        str(item).strip()
        for item in (
            mim_status.get("blockers")
            if isinstance(mim_status.get("blockers"), list)
            else context_payload.get("blockers")
            if isinstance(context_payload.get("blockers"), list)
            else []
        )
        if str(item).strip()
    ]

    objective_id = str(
        alignment_payload.get("tod_current_objective")
        or mim_status.get("objective_active")
        or context_payload.get("objective_active")
        or integration_payload.get("current_next_objective")
        or ""
    ).strip()
    phase = str(mim_status.get("phase") or context_payload.get("phase") or "").strip()
    alignment_status = str(alignment_payload.get("status") or "").strip()

    summary_parts = []
    if objective_id:
        summary_parts.append(f"Current objective is {objective_id}.")
    else:
        summary_parts.append("Current objective is not available from the shared artifacts.")
    if phase:
        summary_parts.append(f"Current phase is {phase}.")
    if alignment_status:
        summary_parts.append(f"MIM and TOD are {alignment_status} on the active objective.")
    if blockers:
        summary_parts.append("Active blockers: " + ", ".join(blockers) + ".")
    else:
        summary_parts.append("There are no active blockers in the shared objective snapshot.")

    return {
        "objective_id": objective_id,
        "phase": phase,
        "alignment_status": alignment_status,
        "blockers": blockers,
        "summary": _compact_text(" ".join(summary_parts), 240),
    }


def _current_tod_recent_changes_snapshot(*, shared_root: Path) -> dict[str, object]:
    integration_payload = _read_first_json(
        shared_root,
        *TOD_INTEGRATION_ARTIFACT_CANDIDATES,
    )
    context_payload = _read_json(shared_root / MIM_CONTEXT_EXPORT_ARTIFACT)
    review_payload = _read_json(shared_root / MIM_TASK_STATUS_REVIEW_ARTIFACT)

    live_task_request = (
        integration_payload.get("live_task_request")
        if isinstance(integration_payload.get("live_task_request"), dict)
        else {}
    )
    system_alerts = (
        review_payload.get("system_alerts")
        if isinstance(review_payload.get("system_alerts"), dict)
        else {}
    )
    verification = (
        context_payload.get("verification")
        if isinstance(context_payload.get("verification"), dict)
        else {}
    )

    objective_id = str(
        context_payload.get("objective_active")
        or context_payload.get("current_next_objective")
        or ""
    ).strip()
    latest_completed_objective = str(
        context_payload.get("latest_completed_objective") or ""
    ).strip()
    context_notes = [
        str(item).strip()
        for item in (context_payload.get("notes") or [])
        if str(item).strip()
    ]
    blockers = [
        str(item).strip()
        for item in (context_payload.get("blockers") or [])
        if str(item).strip()
    ]
    live_request_objective_id = str(live_task_request.get("objective_id") or "").strip()
    normalized_live_objective_id = str(
        live_task_request.get("normalized_objective_id") or objective_id or ""
    ).strip()
    promotion_reason = str(live_task_request.get("promotion_reason") or "").strip()
    stale_reason = str(live_task_request.get("stale_reason") or "").strip()
    alert_code = str(system_alerts.get("primary_alert_code") or "").strip()
    regression_status = str(verification.get("regression_status") or "").strip()
    prod_smoke_status = str(verification.get("prod_smoke_status") or "").strip()
    prod_promotion_status = str(verification.get("prod_promotion_status") or "").strip()

    summary_parts = []
    if objective_id and latest_completed_objective and latest_completed_objective != objective_id:
        summary_parts.append(
            f"Current objective moved from {latest_completed_objective} to {objective_id}."
        )
    elif objective_id:
        summary_parts.append(f"Current objective remains {objective_id}.")
    else:
        summary_parts.append("Current objective is not available from the shared artifacts.")

    if context_notes:
        summary_parts.append(_compact_text(context_notes[0], 180))

    if live_request_objective_id and normalized_live_objective_id:
        if live_request_objective_id != normalized_live_objective_id:
            summary_parts.append(
                f"Live task request still references {live_request_objective_id} while canonical objective state is {normalized_live_objective_id}."
            )
        else:
            summary_parts.append(
                f"Live task request is aligned with objective {normalized_live_objective_id}."
            )

    if alert_code:
        summary_parts.append(f"Active system alert: {alert_code}.")
    if blockers:
        summary_parts.append("Active blockers: " + ", ".join(blockers) + ".")

    verification_parts = [
        f"regression {regression_status}" if regression_status else "",
        f"prod smoke {prod_smoke_status}" if prod_smoke_status else "",
        f"prod promotion {prod_promotion_status}" if prod_promotion_status else "",
    ]
    verification_summary = ", ".join(part for part in verification_parts if part)
    if verification_summary:
        summary_parts.append(f"Verification state: {verification_summary}.")
    if promotion_reason:
        summary_parts.append(f"Promotion note: {promotion_reason}.")
    if stale_reason:
        summary_parts.append(f"Stale reason: {stale_reason}.")

    return {
        "objective_id": objective_id,
        "latest_completed_objective": latest_completed_objective,
        "context_notes": context_notes,
        "live_request_objective_id": live_request_objective_id,
        "normalized_live_objective_id": normalized_live_objective_id,
        "promotion_reason": promotion_reason,
        "stale_reason": stale_reason,
        "alert_code": alert_code,
        "blockers": blockers,
        "regression_status": regression_status,
        "prod_smoke_status": prod_smoke_status,
        "prod_promotion_status": prod_promotion_status,
        "summary": _compact_text(" ".join(summary_parts), 240),
    }


def _current_tod_warnings_summary_snapshot(*, shared_root: Path) -> dict[str, object]:
    integration_payload = _read_first_json(
        shared_root,
        *TOD_INTEGRATION_ARTIFACT_CANDIDATES,
    )
    context_payload = _read_json(shared_root / MIM_CONTEXT_EXPORT_ARTIFACT)
    review_payload = _read_json(shared_root / MIM_TASK_STATUS_REVIEW_ARTIFACT)

    mim_status = (
        integration_payload.get("mim_status")
        if isinstance(integration_payload.get("mim_status"), dict)
        else {}
    )
    live_task_request = (
        integration_payload.get("live_task_request")
        if isinstance(integration_payload.get("live_task_request"), dict)
        else {}
    )
    system_alerts = (
        review_payload.get("system_alerts")
        if isinstance(review_payload.get("system_alerts"), dict)
        else {}
    )
    authority = (
        review_payload.get("authority")
        if isinstance(review_payload.get("authority"), dict)
        else {}
    )

    blockers = [
        str(item).strip()
        for item in (
            mim_status.get("blockers")
            if isinstance(mim_status.get("blockers"), list)
            else context_payload.get("blockers")
            if isinstance(context_payload.get("blockers"), list)
            else []
        )
        if str(item).strip()
    ]
    primary_alert_code = str(system_alerts.get("primary_alert_code") or "").strip()
    highest_severity = str(system_alerts.get("highest_severity") or "").strip()
    system_alerts_active = bool(system_alerts.get("active"))
    authority_reason = str(authority.get("reason_code") or "").strip()
    promotion_reason = str(live_task_request.get("promotion_reason") or "").strip()
    stale_reason = str(live_task_request.get("stale_reason") or "").strip()
    stale_prior_objective = bool(live_task_request.get("stale_prior_objective"))

    summary_parts = []
    if system_alerts_active and primary_alert_code:
        if highest_severity:
            summary_parts.append(
                f"Current warning summary: {primary_alert_code} at {highest_severity} severity."
            )
        else:
            summary_parts.append(f"Current warning summary: {primary_alert_code} is active.")
    elif blockers:
        summary_parts.append("Current warning summary is driven by active blockers.")
    else:
        summary_parts.append("There are no active warning codes in the current shared artifacts.")

    if blockers:
        summary_parts.append("Active blockers: " + ", ".join(blockers) + ".")
    if stale_prior_objective and stale_reason:
        summary_parts.append(f"Live request warning: {stale_reason}.")
    elif promotion_reason:
        summary_parts.append(f"Live request note: {promotion_reason}.")
    if authority_reason:
        summary_parts.append(f"Authority note: {authority_reason}.")

    return {
        "primary_alert_code": primary_alert_code,
        "highest_severity": highest_severity,
        "system_alerts_active": system_alerts_active,
        "blockers": blockers,
        "promotion_reason": promotion_reason,
        "stale_reason": stale_reason,
        "authority_reason": authority_reason,
        "summary": _compact_text(" ".join(summary_parts), 240),
    }


def _current_tod_bridge_warning_snapshot(*, shared_root: Path) -> dict[str, object]:
    integration_payload = _read_first_json(
        shared_root,
        *TOD_INTEGRATION_ARTIFACT_CANDIDATES,
    )
    coordination_payload = _read_json(shared_root / TOD_COORDINATION_REQUEST_ARTIFACT)
    review_payload = _read_json(shared_root / MIM_TASK_STATUS_REVIEW_ARTIFACT)

    live_task_request = (
        integration_payload.get("live_task_request")
        if isinstance(integration_payload.get("live_task_request"), dict)
        else {}
    )
    system_alerts = (
        review_payload.get("system_alerts")
        if isinstance(review_payload.get("system_alerts"), dict)
        else {}
    )
    evidence_payload = (
        coordination_payload.get("evidence")
        if isinstance(coordination_payload.get("evidence"), dict)
        else {}
    )

    issue_code = str(
        coordination_payload.get("issue_code")
        or system_alerts.get("primary_alert_code")
        or ""
    ).strip()
    issue_summary = str(
        coordination_payload.get("issue_summary")
        or coordination_payload.get("requested_action")
        or ""
    ).strip()
    canonical_objective_id = str(
        evidence_payload.get("canonical_expected_objective_id")
        or integration_payload.get("current_next_objective")
        or ""
    ).strip()
    stale_objective_id = str(
        evidence_payload.get("stale_live_task_request_objective_id")
        or live_task_request.get("objective_id")
        or ""
    ).strip()
    stale_reason = str(
        live_task_request.get("stale_reason")
        or live_task_request.get("promotion_reason")
        or ""
    ).strip()
    bridge_failure_modes = [
        str(item).strip()
        for item in (
            evidence_payload.get("bridge_failure_modes")
            if isinstance(evidence_payload.get("bridge_failure_modes"), list)
            else []
        )
        if str(item).strip()
    ]

    summary_parts = []
    if issue_code:
        summary_parts.append(f"Current bridge warning is {issue_code}.")
    else:
        summary_parts.append("There is no explicit bridge warning code in the live coordination artifacts.")
    if issue_summary:
        summary_parts.append(_compact_text(issue_summary, 180))
    if canonical_objective_id and stale_objective_id:
        summary_parts.append(
            f"The canonical objective is {canonical_objective_id}, but the stale live request still references {stale_objective_id}."
        )
    if stale_reason:
        summary_parts.append(f"Live warning reason: {stale_reason}.")
    if bridge_failure_modes:
        summary_parts.append(
            "Observed bridge failure modes: " + ", ".join(bridge_failure_modes) + "."
        )

    return {
        "issue_code": issue_code,
        "issue_summary": issue_summary,
        "canonical_objective_id": canonical_objective_id,
        "stale_objective_id": stale_objective_id,
        "stale_reason": stale_reason,
        "bridge_failure_modes": bridge_failure_modes,
        "summary": _compact_text(" ".join(summary_parts), 240),
    }


def _current_tod_bridge_warning_recommendation_snapshot(
    *, shared_root: Path
) -> dict[str, object]:
    bridge_warning_snapshot = _current_tod_bridge_warning_snapshot(shared_root=shared_root)
    coordination_payload = _read_json(shared_root / TOD_COORDINATION_REQUEST_ARTIFACT)
    review_payload = _read_json(shared_root / MIM_TASK_STATUS_REVIEW_ARTIFACT)

    issue_code = str(
        bridge_warning_snapshot.get("issue_code")
        or coordination_payload.get("issue_code")
        or ""
    ).strip()
    requested_action = str(coordination_payload.get("requested_action") or "").strip()
    authority_reason = str(
        (
            review_payload.get("authority")
            if isinstance(review_payload.get("authority"), dict)
            else {}
        ).get("reason_code")
        or ""
    ).strip()
    canonical_objective_id = str(
        bridge_warning_snapshot.get("canonical_objective_id") or ""
    ).strip()
    stale_objective_id = str(
        bridge_warning_snapshot.get("stale_objective_id") or ""
    ).strip()
    stale_reason = str(bridge_warning_snapshot.get("stale_reason") or "").strip()

    if requested_action:
        next_safe_action = requested_action
    elif issue_code == "publication_surface_divergence":
        next_safe_action = (
            "Republish the live task-request surface from the canonical MIM objective, "
            "or acknowledge why the canonical export should remain ahead of live publication."
        )
    else:
        next_safe_action = (
            "Review the current bridge warning evidence and acknowledge the bounded next step "
            "before changing the live publication surface."
        )

    summary_parts = [f"TOD should next {next_safe_action[0].lower() + next_safe_action[1:]}"]
    if issue_code:
        summary_parts.append(f"This recommendation is scoped to bridge warning {issue_code}.")
    if canonical_objective_id and stale_objective_id:
        summary_parts.append(
            f"Keep the canonical objective {canonical_objective_id} aligned while the stale live request still references {stale_objective_id}."
        )
    if stale_reason:
        summary_parts.append(f"Current stale reason: {stale_reason}.")
    if authority_reason:
        summary_parts.append(f"Authority note: {authority_reason}.")

    return {
        "issue_code": issue_code,
        "next_safe_action": next_safe_action,
        "canonical_objective_id": canonical_objective_id,
        "stale_objective_id": stale_objective_id,
        "stale_reason": stale_reason,
        "authority_reason": authority_reason,
        "summary": _compact_text(" ".join(summary_parts), 240),
        "bridge_warning_snapshot": bridge_warning_snapshot,
    }


def create_primitive_request_recovery(
    *,
    session_key: str,
    message_id: int,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    request_id = f"primitive-request-{timestamp}-{uuid4().hex[:8]}"
    objective_id = f"primitive-request-{message_id}"
    detail = " ".join(str(content or "").strip().split())
    decision = {
        "code": "fresh_request_recorded",
        "detail": "Recorded one fresh operator request for MIM handling.",
    }
    payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": objective_id,
        "session_key": str(session_key or "").strip(),
        "message_id": int(message_id),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": detail[:120],
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded follow-through or an explicit blocker.",
        "decision_code": str(decision["code"]),
        "decision_detail": str(decision["detail"]),
        "generated_at": _utc_now(),
        "content": detail,
        "decision": decision,
    }
    _write_json(shared_root / REQUEST_ARTIFACT, payload)
    return payload


def dispatch_bounded_tod_status_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-status-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_status_check",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD status response.",
        "decision_code": "tod_status_dispatch_requested",
        "decision_detail": "MIM routed one bounded TOD status request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_status_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded status request from MIM.",
        "action_name": "tod_status_check",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    status_snapshot = _current_tod_status_snapshot(shared_root=shared_root)
    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(status_snapshot.get("summary") or "TOD accepted the bounded status check.").strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-status-dispatch",
        "action_name": "tod_status_check",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_status_request",
        "tod_status_snapshot": status_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_status_check",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_status_dispatch_completed",
        "decision_detail": "MIM dispatched one bounded TOD status request and TOD returned a status result.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
    }


def dispatch_bounded_tod_objective_summary_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-objective-summary-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_objective_summary",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD objective summary.",
        "decision_code": "tod_objective_summary_requested",
        "decision_detail": "MIM routed one bounded TOD objective-summary request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_objective_summary_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded current-objective summary request from MIM.",
        "action_name": "tod_objective_summary",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    objective_snapshot = _current_tod_objective_summary_snapshot(shared_root=shared_root)
    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(
            objective_snapshot.get("summary")
            or "TOD returned the current objective summary."
        ).strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-objective-summary-dispatch",
        "action_name": "tod_objective_summary",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_objective_summary_request",
        "tod_objective_snapshot": objective_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_objective_summary",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_objective_summary_completed",
        "decision_detail": "MIM dispatched one bounded TOD objective-summary request and TOD returned the current objective summary.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
        "dispatch_kind": "bounded_objective_summary_request",
        "tod_objective_snapshot": objective_snapshot,
    }


def dispatch_bounded_tod_recent_changes_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-recent-changes-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_recent_changes_summary",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD recent-changes summary.",
        "decision_code": "tod_recent_changes_requested",
        "decision_detail": "MIM routed one bounded TOD recent-changes summary request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_recent_changes_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded recent-changes summary request from MIM.",
        "action_name": "tod_recent_changes_summary",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    recent_changes_snapshot = _current_tod_recent_changes_snapshot(shared_root=shared_root)
    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(
            recent_changes_snapshot.get("summary")
            or "TOD returned the recent changes that affect the current objective."
        ).strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-recent-changes-dispatch",
        "action_name": "tod_recent_changes_summary",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_recent_changes_request",
        "tod_recent_changes_snapshot": recent_changes_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_recent_changes_summary",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_recent_changes_completed",
        "decision_detail": "MIM dispatched one bounded TOD recent-changes summary request and TOD returned the current material changes.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
        "dispatch_kind": "bounded_recent_changes_request",
        "tod_recent_changes_snapshot": recent_changes_snapshot,
    }


def dispatch_bounded_tod_warnings_summary_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    warnings_snapshot = _current_tod_warnings_summary_snapshot(shared_root=shared_root)
    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-warnings-summary-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_warnings_summary",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD warnings summary.",
        "decision_code": "tod_warnings_summary_requested",
        "decision_detail": "MIM routed one bounded TOD warnings-summary request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_warnings_summary_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded warnings-summary request from MIM.",
        "action_name": "tod_warnings_summary",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(
            warnings_snapshot.get("summary")
            or "TOD returned the current warnings summary."
        ).strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-warnings-summary-dispatch",
        "action_name": "tod_warnings_summary",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_warnings_summary_request",
        "tod_warnings_summary_snapshot": warnings_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_warnings_summary",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_warnings_summary_completed",
        "decision_detail": "MIM dispatched one bounded TOD warnings-summary request and TOD returned the current warning state.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
        "dispatch_kind": "bounded_warnings_summary_request",
        "tod_warnings_summary_snapshot": warnings_snapshot,
    }


def dispatch_bounded_tod_bridge_warning_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    bridge_warning_snapshot = _current_tod_bridge_warning_snapshot(shared_root=shared_root)
    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-bridge-warning-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_bridge_warning_explanation",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD bridge-warning explanation.",
        "decision_code": "tod_bridge_warning_requested",
        "decision_detail": "MIM routed one bounded TOD bridge-warning explanation request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_bridge_warning_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded bridge-warning explanation request from MIM.",
        "action_name": "tod_bridge_warning_explanation",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(
            bridge_warning_snapshot.get("summary")
            or "TOD returned the current bridge warning explanation."
        ).strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-bridge-warning-dispatch",
        "action_name": "tod_bridge_warning_explanation",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_bridge_warning_request",
        "tod_bridge_warning_snapshot": bridge_warning_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_bridge_warning_explanation",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_bridge_warning_completed",
        "decision_detail": "MIM dispatched one bounded TOD bridge-warning explanation request and TOD returned the live bridge warning details.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
        "dispatch_kind": "bounded_bridge_warning_request",
        "tod_bridge_warning_snapshot": bridge_warning_snapshot,
    }


def dispatch_bounded_tod_bridge_warning_recommendation_request(
    *,
    request_id: str,
    session_key: str,
    content: str,
    actor: str,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")

    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)

    recommendation_snapshot = _current_tod_bridge_warning_recommendation_snapshot(
        shared_root=shared_root
    )
    detail = _compact_text(content, 200)
    request_generated_at = _utc_now()
    request_payload = {
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-bridge-warning-next-step-dispatch",
        "session_key": str(session_key or "").strip(),
        "actor": str(actor or "operator").strip() or "operator",
        "action_name": "tod_bridge_warning_recommendation",
        "request_status": "recorded",
        "result_status": "pending",
        "result_reason": "Awaiting bounded TOD bridge-warning next-step recommendation.",
        "decision_code": "tod_bridge_warning_recommendation_requested",
        "decision_detail": "MIM routed one bounded TOD bridge-warning next-step recommendation request through the shared bridge.",
        "generated_at": request_generated_at,
        "content": detail,
        "target_executor": "tod",
        "dispatch_kind": "bounded_bridge_warning_recommendation_request",
    }
    _write_json(shared_root / REQUEST_ARTIFACT, request_payload)

    ack_generated_at = _utc_now()
    ack_payload = {
        "generated_at": ack_generated_at,
        "packet_type": "tod-mim-task-ack-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "status": "accepted",
        "ack_reason": "TOD accepted the bounded bridge-warning next-step recommendation request from MIM.",
        "action_name": "tod_bridge_warning_recommendation",
    }
    _write_json(shared_root / ACK_ARTIFACT, ack_payload)

    result_generated_at = _utc_now()
    result_reason = _compact_text(
        str(
            recommendation_snapshot.get("summary")
            or "TOD returned the bounded bridge-warning next-step recommendation."
        ).strip(),
        220,
    )
    result_payload = {
        "generated_at": result_generated_at,
        "packet_type": "tod-mim-task-result-v1",
        "source": "tod",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": "mim-tod-bridge-warning-next-step-dispatch",
        "action_name": "tod_bridge_warning_recommendation",
        "status": "succeeded",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "summary": result_reason,
        "dispatch_kind": "bounded_bridge_warning_recommendation_request",
        "tod_bridge_warning_recommendation_snapshot": recommendation_snapshot,
    }
    _write_json(shared_root / RESULT_ARTIFACT, result_payload)

    return {
        "request_id": request_id,
        "task_id": request_id,
        "action_name": "tod_bridge_warning_recommendation",
        "request_status": "accepted",
        "result_status": "succeeded",
        "result_reason": result_reason,
        "decision_code": "tod_bridge_warning_recommendation_completed",
        "decision_detail": "MIM dispatched one bounded TOD bridge-warning next-step recommendation request and TOD returned the current safe recommendation.",
        "generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "request_path": str(shared_root / REQUEST_ARTIFACT),
        "ack_path": str(shared_root / ACK_ARTIFACT),
        "result_path": str(shared_root / RESULT_ARTIFACT),
        "target_executor": "tod",
        "dispatch_kind": "bounded_bridge_warning_recommendation_request",
        "tod_bridge_warning_recommendation_snapshot": recommendation_snapshot,
    }


def load_authoritative_request_status(
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    request_payload = _read_json(shared_root / REQUEST_ARTIFACT)
    ack_payload = _read_json(shared_root / ACK_ARTIFACT)
    result_payload = _read_json(shared_root / RESULT_ARTIFACT)
    review_payload = _read_json(shared_root / MIM_TASK_STATUS_REVIEW_ARTIFACT)
    review_task = (
        review_payload.get("task")
        if isinstance(review_payload.get("task"), dict)
        else {}
    )

    active_lineage = _active_lineage(request_payload, ack_payload, result_payload)
    if not _has_lineage(active_lineage):
        return {}

    review_request_id = str(
        review_task.get("request_request_id")
        or review_task.get("result_request_id")
        or ""
    ).strip()
    review_task_id = str(
        review_task.get("active_task_id")
        or review_task.get("request_task_id")
        or review_task.get("result_task_id")
        or ""
    ).strip()
    review_objective_id = str(
        review_task.get("objective_id")
        or ""
    ).strip()
    review_generated_at = str(review_payload.get("generated_at") or "").strip()
    review_result_status = str(
        review_task.get("result_status")
        or review_payload.get("state")
        or ""
    ).strip()
    review_result_reason = str(review_payload.get("state_reason") or "").strip()
    review_lineage = _lineage_payload(
        request_id=review_request_id,
        task_id=review_task_id,
        objective_id=review_objective_id,
        correlation_id=review_task.get("correlation_id"),
    )

    mismatched_lineages = []
    for payload in (
        _lineage_payload(
            request_id=ack_payload.get("request_id"),
            task_id=ack_payload.get("task_id"),
            objective_id=ack_payload.get("objective_id"),
            correlation_id=ack_payload.get("correlation_id"),
        ),
        _lineage_payload(
            request_id=result_payload.get("request_id"),
            task_id=result_payload.get("task_id"),
            objective_id=result_payload.get("objective_id"),
            correlation_id=result_payload.get("correlation_id"),
        ),
        review_lineage,
    ):
        if _has_lineage(payload) and not _lineage_matches(active_lineage, payload):
            mismatched_lineages.append(payload)

    if mismatched_lineages:
        return _lineage_mismatch_payload(
            active_lineage=active_lineage,
            request_payload=request_payload,
            ack_payload=ack_payload,
            result_payload=result_payload,
            review_payload=review_payload,
        )

    request_id = active_lineage.get("request_id") or active_lineage.get("task_id") or ""

    result_status = str(
        result_payload.get("result_status") or result_payload.get("status") or ""
    ).strip()
    result_reason = str(
        result_payload.get("result_reason")
        or result_payload.get("reason")
        or ""
    ).strip()
    request_generated_at = str(request_payload.get("generated_at") or "").strip()
    ack_generated_at = str(ack_payload.get("generated_at") or "").strip()
    result_generated_at = str(result_payload.get("generated_at") or "").strip()
    review_matches_active = _has_lineage(review_lineage) and _lineage_matches(active_lineage, review_lineage)

    return {
        "request_id": request_id,
        "task_id": str(
            active_lineage.get("task_id")
            or active_lineage.get("request_id")
            or request_id
        ).strip(),
        "objective_id": str(
            active_lineage.get("objective_id")
            or ""
        ).strip(),
        "correlation_id": str(active_lineage.get("correlation_id") or "").strip(),
        "action_name": str(request_payload.get("action_name") or "").strip(),
        "request_status": str(request_payload.get("request_status") or "recorded").strip(),
        "result_status": review_result_status if review_matches_active and review_result_status else result_status or str(request_payload.get("result_status") or "pending").strip(),
        "result_reason": review_result_reason if review_matches_active and review_result_reason else result_reason or str(request_payload.get("result_reason") or "").strip(),
        "ack_status": str(ack_payload.get("status") or "").strip(),
        "decision_code": str(
            request_payload.get("decision_code")
            or (
                request_payload.get("decision", {}).get("code")
                if isinstance(request_payload.get("decision"), dict)
                else ""
            )
            or "fresh_request_recorded"
        ).strip(),
        "decision_detail": str(
            request_payload.get("decision_detail")
            or (
                request_payload.get("decision", {}).get("detail")
                if isinstance(request_payload.get("decision"), dict)
                else ""
            )
            or "Recorded one fresh operator request for MIM handling."
        ).strip(),
        "request_generated_at": request_generated_at,
        "ack_generated_at": ack_generated_at,
        "result_generated_at": result_generated_at,
        "review_generated_at": review_generated_at,
        "generated_at": _latest_timestamp_text(
            request_generated_at,
            ack_generated_at,
            result_generated_at,
            review_generated_at,
        ),
    }