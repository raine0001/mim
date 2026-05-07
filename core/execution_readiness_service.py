from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.state_bus_service import (
    append_state_bus_event,
    get_state_bus_snapshot,
    to_state_bus_snapshot_out,
    upsert_state_bus_snapshot,
)

READINESS_SOURCE = "objective90"
READINESS_SIGNAL_NAME = "execution-readiness"
READINESS_OUTCOME_ORDER = {"allow": 0, "degrade": 1, "block": 2}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _json_list(raw: object) -> list:
    return raw if isinstance(raw, list) else []


def _json_safe(raw: object) -> object:
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(raw, dict):
        return {str(key): _json_safe(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return [_json_safe(item) for item in raw]
    if isinstance(raw, tuple):
        return [_json_safe(item) for item in raw]
    return raw


def _parse_timestamp(raw: object) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_artifact(path: Path) -> tuple[dict, dict] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    readiness = _json_dict(payload.get("execution_readiness"))
    if not readiness:
        readiness = _json_dict(_json_dict(payload.get("execution_trace")).get("execution_readiness"))
    generated_at = (
        _parse_timestamp(payload.get("generated_at"))
        or _parse_timestamp(readiness.get("generated_at"))
        or _utcnow()
    )
    return payload, {
        "artifact_path": str(path),
        "artifact_name": path.name,
        "generated_at": generated_at.isoformat(),
        "has_readiness": bool(readiness),
    }


def _artifact_identity(payload: dict) -> dict[str, str]:
    execution_trace = _json_dict(payload.get("execution_trace"))
    integration = _json_dict(payload.get("integration"))
    return {
        "request_id": str(payload.get("request_id") or "").strip(),
        "task_id": str(payload.get("task_id") or "").strip(),
        "objective_id": str(
            payload.get("objective_id")
            or integration.get("tod_current_objective")
            or integration.get("mim_objective_active")
            or ""
        ).strip(),
        "correlation_id": str(
            payload.get("correlation_id")
            or execution_trace.get("correlation_id")
            or ""
        ).strip(),
    }


def _same_execution_lineage(left: dict[str, str], right: dict[str, str]) -> bool:
    left_task_id = str(left.get("task_id") or "").strip()
    right_task_id = str(right.get("task_id") or "").strip()
    if not left_task_id or not right_task_id or left_task_id != right_task_id:
        return False

    for key in ("objective_id", "request_id", "correlation_id"):
        left_value = str(left.get(key) or "").strip()
        right_value = str(right.get(key) or "").strip()
        if left_value and right_value and left_value != right_value:
            return False
    return True


def _is_superseded_readiness_candidate(
    candidate: tuple[dict, dict],
    artifacts: list[tuple[dict, dict]],
) -> bool:
    candidate_generated_at = _parse_timestamp(candidate[1].get("generated_at")) or _utcnow()
    candidate_identity = _artifact_identity(candidate[0])
    for other_payload, other_metadata in artifacts:
        if other_metadata.get("has_readiness"):
            continue
        other_generated_at = _parse_timestamp(other_metadata.get("generated_at")) or _utcnow()
        if other_generated_at <= candidate_generated_at:
            continue
        if _same_execution_lineage(candidate_identity, _artifact_identity(other_payload)):
            return True
    return False


def _default_readiness(*, action: str, capability_name: str, managed_scope: str) -> dict:
    return {
        "status": "missing",
        "source": "readiness_signal_unavailable",
        "detail": "Execution readiness artifact is unavailable.",
        "valid": False,
        "execution_allowed": False,
        "authoritative": False,
        "freshness_state": "missing",
        "signal_name": READINESS_SIGNAL_NAME,
        "evaluated_action": action,
        "policy_outcome": "block",
        "gate_state": "blocked",
        "decision_path": [
            f"signal:{READINESS_SIGNAL_NAME}",
            "status:missing",
            "policy_outcome:block",
        ],
        "capability_name": str(capability_name or "").strip(),
        "managed_scope": str(managed_scope or "").strip() or "global",
        "loaded_at": _utcnow().isoformat(),
    }


def _normalize_policy_outcome(*, raw_outcome: object, valid: bool, execution_allowed: bool) -> str:
    outcome = str(raw_outcome or "").strip().lower()
    if outcome in READINESS_OUTCOME_ORDER:
        return outcome
    if not execution_allowed:
        return "block"
    if valid:
        return "allow"
    return "degrade"


def normalize_execution_readiness(
    readiness: dict | None,
    *,
    action: str,
    capability_name: str,
    managed_scope: str,
    requested_executor: str,
    metadata_json: dict | None = None,
    artifact_metadata: dict | None = None,
) -> dict:
    raw = readiness if isinstance(readiness, dict) else {}
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    artifact = artifact_metadata if isinstance(artifact_metadata, dict) else {}
    status = str(raw.get("status") or "unknown").strip().lower() or "unknown"
    valid = bool(raw.get("valid")) if raw.get("valid") is not None else status == "valid"
    execution_allowed = (
        bool(raw.get("execution_allowed"))
        if raw.get("execution_allowed") is not None
        else valid
    )
    policy_outcome = _normalize_policy_outcome(
        raw_outcome=raw.get("policy_outcome"),
        valid=valid,
        execution_allowed=execution_allowed,
    )
    gate_state = "ready"
    if policy_outcome == "block":
        gate_state = "blocked"
    elif policy_outcome == "degrade" or not valid:
        gate_state = "degraded"
    decision_path = [
        str(item).strip()
        for item in _json_list(raw.get("decision_path"))
        if str(item).strip()
    ]
    if not decision_path:
        decision_path = [
            f"signal:{READINESS_SIGNAL_NAME}",
            f"status:{status}",
            f"policy_outcome:{policy_outcome}",
        ]
    return {
        "status": status,
        "source": str(raw.get("source") or "unknown").strip() or "unknown",
        "detail": str(raw.get("detail") or "").strip(),
        "valid": valid,
        "execution_allowed": execution_allowed,
        "authoritative": bool(raw.get("authoritative", False)),
        "freshness_state": str(raw.get("freshness_state") or status).strip() or status,
        "signal_name": str(raw.get("signal_name") or READINESS_SIGNAL_NAME).strip() or READINESS_SIGNAL_NAME,
        "evaluated_action": str(raw.get("evaluated_action") or action).strip() or action,
        "policy_outcome": policy_outcome,
        "gate_state": gate_state,
        "decision_path": decision_path,
        "capability_name": str(capability_name or "").strip(),
        "managed_scope": str(managed_scope or metadata.get("managed_scope") or "").strip() or "global",
        "requested_executor": str(requested_executor or "").strip(),
        "artifact_path": str(artifact.get("artifact_path") or "").strip(),
        "artifact_name": str(artifact.get("artifact_name") or "").strip(),
        "artifact_generated_at": str(artifact.get("generated_at") or "").strip(),
        "loaded_at": _utcnow().isoformat(),
    }


def load_latest_execution_readiness(
    *,
    action: str,
    capability_name: str,
    managed_scope: str,
    requested_executor: str,
    metadata_json: dict | None = None,
) -> dict:
    artifacts: list[tuple[dict, dict]] = []
    for configured_path in (
        settings.execution_readiness_task_result_path,
        settings.execution_readiness_command_status_path,
    ):
        loaded = _load_artifact(Path(configured_path))
        if loaded is not None:
            artifacts.append(loaded)

    candidates = [
        artifact
        for artifact in artifacts
        if artifact[1].get("has_readiness")
        and not _is_superseded_readiness_candidate(artifact, artifacts)
    ]

    if not candidates:
        return _default_readiness(
            action=action,
            capability_name=capability_name,
            managed_scope=managed_scope,
        )

    candidates.sort(
        key=lambda item: _parse_timestamp(item[1].get("generated_at")) or _utcnow(),
        reverse=True,
    )
    payload, artifact_metadata = candidates[0]
    readiness = _json_dict(payload.get("execution_readiness"))
    if not readiness:
        readiness = _json_dict(_json_dict(payload.get("execution_trace")).get("execution_readiness"))
    return normalize_execution_readiness(
        readiness,
        action=action,
        capability_name=capability_name,
        managed_scope=managed_scope,
        requested_executor=requested_executor,
        metadata_json=metadata_json,
        artifact_metadata=artifact_metadata,
    )


def execution_readiness_posture(readiness: dict) -> str:
    policy_outcome = str(readiness.get("policy_outcome") or "allow").strip().lower()
    if policy_outcome in {"block", "degrade"}:
        return "caution"
    if not bool(readiness.get("valid", False)):
        return "caution"
    return "promote"


def execution_readiness_confidence(readiness: dict) -> float:
    if str(readiness.get("policy_outcome") or "").strip().lower() == "block":
        return 0.98
    if str(readiness.get("policy_outcome") or "").strip().lower() == "degrade":
        return 0.9
    if bool(readiness.get("valid", False)):
        return 0.92
    return 0.74


def execution_readiness_precedence(
    readiness: dict,
    *,
    blocking_rank: float,
    advisory_rank: float,
    ready_rank: float,
) -> float:
    policy_outcome = str(readiness.get("policy_outcome") or "allow").strip().lower()
    if policy_outcome in {"block", "degrade"}:
        return float(blocking_rank)
    if not bool(readiness.get("valid", False)):
        return float(advisory_rank)
    return float(ready_rank)


def execution_readiness_policy_effects(*, readiness: dict, surface: str) -> dict:
    policy_outcome = str(readiness.get("policy_outcome") or "allow").strip().lower()
    valid = bool(readiness.get("valid", False))
    detail = str(readiness.get("detail") or "execution readiness requires policy handling").strip()

    if surface == "execution":
        if policy_outcome == "block":
            return {
                "target_dispatch_decision": "blocked",
                "target_status": "blocked",
                "reason": "execution_readiness_blocked",
                "why_policy_prevailed": detail,
            }
        if policy_outcome == "degrade":
            return {
                "require_operator_confirmation": True,
                "target_dispatch_decision": "requires_confirmation",
                "target_status": "pending_confirmation",
                "reason": "execution_readiness_degraded",
                "why_policy_prevailed": detail,
            }
        return {
            "readiness_observed": True,
            "readiness_gate_state": str(readiness.get("gate_state") or "ready").strip(),
            "why_policy_prevailed": detail if not valid else "",
        }

    if surface == "proposal":
        if policy_outcome == "block":
            return {
                "priority_delta": -0.32,
                "score_cap": 0.3,
                "require_operator_confirmation": True,
                "suppress_before_arbitration": True,
                "why_policy_prevailed": detail,
            }
        if policy_outcome == "degrade":
            return {
                "priority_delta": -0.18,
                "score_cap": 0.52,
                "require_operator_confirmation": True,
                "why_policy_prevailed": detail,
            }
        if not valid:
            return {
                "priority_delta": -0.08,
                "score_cap": 0.72,
                "why_policy_prevailed": detail,
            }
        return {
            "priority_delta": 0.0,
            "score_cap": None,
            "why_policy_prevailed": "",
        }

    if surface == "stewardship":
        allow_auto_execution = policy_outcome == "allow" and valid
        return {
            "allow_auto_execution": allow_auto_execution,
            "last_decision_summary": (
                "execution_readiness_ready"
                if allow_auto_execution
                else "defer_to_execution_readiness"
            ),
            "why_policy_prevailed": detail if not allow_auto_execution else "",
        }

    if surface == "autonomy":
        if policy_outcome in {"block", "degrade"} or not valid:
            return {
                "target_level": "operator_required",
                "why_policy_prevailed": detail,
            }
        return {
            "target_level": "",
            "why_policy_prevailed": "",
        }

    return {}


def execution_readiness_summary(readiness: dict) -> dict:
    policy_outcome = str(readiness.get("policy_outcome") or "allow").strip().lower()
    status = str(readiness.get("status") or "unknown").strip().lower()
    detail = str(readiness.get("detail") or "").strip()
    summary = f"{status} ({policy_outcome})" if status else policy_outcome
    if detail:
        summary = f"{summary}: {detail}"
    return {
        "status": status,
        "policy_outcome": policy_outcome,
        "gate_state": str(readiness.get("gate_state") or "").strip(),
        "managed_scope": str(readiness.get("managed_scope") or "").strip(),
        "summary": summary.strip(),
        "detail": detail,
    }


def _snapshot_scope(managed_scope: str) -> str:
    scope = str(managed_scope or "").strip() or "global"
    return f"execution-readiness:{scope}"


def _severity(readiness: dict) -> int:
    outcome = str(readiness.get("policy_outcome") or "allow").strip().lower()
    score = READINESS_OUTCOME_ORDER.get(outcome, 0)
    if not bool(readiness.get("valid", False)) and score < READINESS_OUTCOME_ORDER["degrade"]:
        return READINESS_OUTCOME_ORDER["degrade"]
    return score


async def publish_execution_readiness_state(
    *,
    db: AsyncSession,
    actor: str,
    source: str,
    readiness: dict,
    metadata_json: dict | None = None,
) -> dict:
    normalized = _json_dict(_json_safe(readiness))
    snapshot_scope = _snapshot_scope(str(normalized.get("managed_scope") or "global"))
    existing_snapshot = await get_state_bus_snapshot(snapshot_scope=snapshot_scope, db=db)
    existing_payload = (
        existing_snapshot.state_payload_json
        if existing_snapshot is not None and isinstance(existing_snapshot.state_payload_json, dict)
        else {}
    )
    changed = existing_payload != normalized
    event = None
    event_type = "readiness_changed"
    if changed:
        previous_severity = _severity(existing_payload)
        current_severity = _severity(normalized)
        if existing_payload and previous_severity > current_severity:
            event_type = "readiness_recovered"
        elif current_severity > previous_severity or not bool(normalized.get("valid", False)):
            event_type = "readiness_degraded"
        event = await append_state_bus_event(
            actor=actor,
            source=source or READINESS_SOURCE,
            event_domain="tod.runtime",
            event_type=event_type,
            stream_key=snapshot_scope,
            payload_json={
                "readiness": normalized,
                "previous": existing_payload,
            },
            metadata_json={
                "objective": "objective90",
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
            db=db,
        )

    snapshot = await upsert_state_bus_snapshot(
        actor=actor,
        source=source or READINESS_SOURCE,
        snapshot_scope=snapshot_scope,
        state_payload_json=normalized,
        last_event_id=int(event.id) if event is not None else None,
        metadata_json={
            "objective": "objective90",
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
    )
    return {
        "snapshot_scope": snapshot_scope,
        "event_type": event_type if event is not None else "",
        "event_id": int(event.id) if event is not None else None,
        "changed": changed,
        "snapshot": to_state_bus_snapshot_out(snapshot),
    }