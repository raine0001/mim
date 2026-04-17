from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_service import summarize_execution_truth
from core.models import (
    CapabilityExecution,
    WorkspaceExecutionTruthGovernanceProfile,
    WorkspacePerceptionSource,
    WorkspaceStewardshipCycle,
)


GOVERNANCE_DECISIONS = {
    "monitor_only",
    "increase_visibility",
    "lower_autonomy_boundary",
    "prioritize_improvement",
    "require_sandbox_experiment",
    "escalate_to_operator",
}

PERCEPTION_STALE_SECONDS = 180.0


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _safe_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return float(default)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_governance_snapshot(*, managed_scope: str = "global") -> dict:
    scope = str(managed_scope or "").strip() or "global"
    return {
        "managed_scope": scope,
        "status": "inactive",
        "lookback_hours": 24,
        "execution_count": 0,
        "signal_count": 0,
        "confidence": 0.0,
        "governance_state": "stable",
        "governance_decision": "monitor_only",
        "governance_reason": "No execution-truth governance profile has been evaluated for this scope yet.",
        "trigger_counts": {},
        "trigger_evidence": {},
        "downstream_actions": {
            "strategy_weight_delta": 0.0,
            "improvement_priority_delta": 0.0,
            "preferred_backlog_decision": "",
            "autonomy_level_cap": "",
            "maintenance_auto_execute_allowed": True,
            "stewardship_auto_execute_allowed": True,
            "stewardship_weight_multiplier": 1.0,
            "visibility_only": False,
        },
        "reasoning": {},
        "execution_truth_summary": {},
        "metadata_json": {},
    }


def _scope_matches(*, row: WorkspaceExecutionTruthGovernanceProfile, managed_scope: str) -> bool:
    requested = str(managed_scope or "").strip() or "global"
    if requested == "global":
        return True
    return str(row.managed_scope or "").strip() == requested


def _stewardship_scope_matches(
    row: WorkspaceStewardshipCycle, managed_scope: str
) -> bool:
    requested = str(managed_scope or "").strip() or "global"
    if requested == "global":
        return True
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return str(metadata.get("managed_scope", "")).strip() == requested


def _perception_scope_refs(row: WorkspacePerceptionSource) -> set[str]:
    refs: set[str] = set()

    def _collect(value: object) -> None:
        text = str(value or "").strip()
        if text:
            refs.add(text)

    _collect(row.session_id)
    _collect(row.device_id)
    _collect(row.source_type)

    payload = (
        row.last_event_payload_json
        if isinstance(row.last_event_payload_json, dict)
        else {}
    )
    payload_metadata = (
        payload.get("metadata_json", {})
        if isinstance(payload.get("metadata_json", {}), dict)
        else {}
    )
    source_metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    for bucket in (source_metadata, payload_metadata, payload):
        if not isinstance(bucket, dict):
            continue
        for key in (
            "managed_scope",
            "target_scope",
            "scope",
            "session_id",
            "run_id",
        ):
            _collect(bucket.get(key))
    return refs


def _perception_scope_matches(*, row: WorkspacePerceptionSource, managed_scope: str) -> bool:
    requested = str(managed_scope or "").strip() or "global"
    if requested == "global":
        return True
    return requested in _perception_scope_refs(row)


def _trigger_counts_from_summary(*, summary: dict) -> dict:
    recent_executions = (
        summary.get("recent_executions", [])
        if isinstance(summary.get("recent_executions", []), list)
        else []
    )
    counts = {
        "execution_slower_than_expected": 0,
        "retry_instability_detected": 0,
        "fallback_path_used": 0,
        "simulation_reality_mismatch": 0,
        "environment_shift_during_execution": 0,
        "high_confidence_executions": 0,
    }
    confidence_total = 0.0
    for item in recent_executions:
        if not isinstance(item, dict):
            continue
        confidence = _bounded(float(item.get("truth_confidence", 0.0) or 0.0))
        confidence_total += confidence
        if confidence >= 0.65:
            counts["high_confidence_executions"] += 1
        for signal_type in (
            item.get("signal_types", [])
            if isinstance(item.get("signal_types", []), list)
            else []
        ):
            signal = str(signal_type or "").strip()
            if signal in counts:
                counts[signal] += 1

    execution_count = max(1, int(summary.get("execution_count", 0) or 0))
    counts["avg_truth_confidence"] = round(
        confidence_total / float(execution_count), 6
    )
    counts["freshness_weight"] = round(
        _safe_float(
            (
                summary.get("freshness", {})
                if isinstance(summary.get("freshness", {}), dict)
                else {}
            ).get("freshness_weight", 0.0),
            default=0.0,
        ),
        6,
    )
    counts["retry_density"] = round(
        counts["retry_instability_detected"] / float(execution_count), 6
    )
    return counts


def _recent_stewardship_correlation(*, rows: list[WorkspaceStewardshipCycle]) -> dict:
    correlated_rows = 0
    persistent_rows = 0
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        verification = (
            metadata.get("verification", {})
            if isinstance(metadata.get("verification", {}), dict)
            else {}
        )
        persistent = bool(verification.get("persistent_degradation", False))
        signal_count = _safe_int(verification.get("execution_truth_signal_count", 0))
        if persistent:
            persistent_rows += 1
        if persistent and signal_count > 0:
            correlated_rows += 1
    return {
        "persistent_degradation_cycles": persistent_rows,
        "correlated_stewardship_cycles": correlated_rows,
    }


def _freshness_from_timestamp(timestamp: datetime | None, *, decay_window_hours: int) -> dict:
    if timestamp is None:
        return {
            "latest_observed_at": None,
            "latest_age_seconds": None,
            "freshness_weight": 0.0,
        }
    now = datetime.now(timezone.utc)
    age_seconds = max(0.0, (now - timestamp).total_seconds())
    window_seconds = float(max(1, int(decay_window_hours)) * 3600)
    freshness_weight = _bounded(1.0 - (age_seconds / window_seconds))
    return {
        "latest_observed_at": timestamp.isoformat(),
        "latest_age_seconds": round(age_seconds, 6),
        "freshness_weight": round(freshness_weight, 6),
    }


def _source_noise_weight(*, row: WorkspacePerceptionSource) -> float:
    accepted_count = _safe_int(row.accepted_count, default=0)
    dropped_count = _safe_int(row.dropped_count, default=0)
    duplicate_count = _safe_int(row.duplicate_count, default=0)
    low_confidence_count = _safe_int(row.low_confidence_count, default=0)
    total_events = max(1, accepted_count + dropped_count)
    duplicate_ratio = duplicate_count / float(total_events)
    low_confidence_ratio = low_confidence_count / float(total_events)
    degraded_weight = (
        1.0 if str(row.health_status or "").strip() != "healthy" else 0.0
    )
    return _bounded(
        (duplicate_ratio * 0.4)
        + (low_confidence_ratio * 0.45)
        + (degraded_weight * 0.15)
    )


def _latest_camera_labels(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    observations = (
        payload.get("observations", [])
        if isinstance(payload.get("observations", []), list)
        else []
    )
    labels = [
        str(item.get("object_label", "")).strip()
        for item in observations
        if isinstance(item, dict) and str(item.get("object_label", "")).strip()
    ]
    if labels:
        return sorted(set(labels))
    label = str(payload.get("object_label", "")).strip()
    return [label] if label else []


def _perception_signal_summary(
    *, rows: list[WorkspacePerceptionSource], lookback_hours: int
) -> dict:
    if not rows:
        return {
            "source_count": 0,
            "active_source_count": 0,
            "camera_source_count": 0,
            "mic_source_count": 0,
            "stale_source_count": 0,
            "degraded_source_count": 0,
            "avg_confidence": 0.0,
            "freshness_weight": 0.0,
            "sensor_noise_weight": 0.0,
            "camera_grounding_weight": 0.0,
            "mic_grounding_weight": 0.0,
            "perception_grounding_weight": 0.0,
            "latest_camera": {},
            "latest_microphone": {},
        }

    now = datetime.now(timezone.utc)
    source_count = len(rows)
    active_source_count = 0
    camera_source_count = 0
    mic_source_count = 0
    stale_source_count = 0
    degraded_source_count = 0
    confidence_total = 0.0
    confidence_count = 0
    freshness_total = 0.0
    noise_total = 0.0
    camera_grounding_weight = 0.0
    mic_grounding_weight = 0.0
    latest_camera: dict = {}
    latest_microphone: dict = {}
    latest_camera_timestamp: datetime | None = None
    latest_microphone_timestamp: datetime | None = None

    for row in rows:
        source_type = str(row.source_type or "").strip()
        if source_type == "camera":
            camera_source_count += 1
        if source_type == "microphone":
            mic_source_count += 1

        if str(row.health_status or "").strip() != "healthy":
            degraded_source_count += 1

        payload = (
            row.last_event_payload_json
            if isinstance(row.last_event_payload_json, dict)
            else {}
        )
        status = str(payload.get("status", "")).strip()
        event_timestamp = (
            _parse_timestamp(payload.get("timestamp"))
            or row.last_accepted_at
            or row.last_seen_at
            or row.created_at
        )
        freshness = _freshness_from_timestamp(
            event_timestamp,
            decay_window_hours=lookback_hours,
        )
        freshness_weight = _safe_float(freshness.get("freshness_weight", 0.0))
        freshness_total += freshness_weight

        age_seconds = (
            max(0.0, (now - event_timestamp).total_seconds())
            if event_timestamp is not None
            else None
        )
        is_active = age_seconds is not None and age_seconds <= PERCEPTION_STALE_SECONDS
        if is_active:
            active_source_count += 1
        else:
            stale_source_count += 1

        noise_weight = _source_noise_weight(row=row)
        if not is_active:
            noise_weight = _bounded(noise_weight + 0.2)
        noise_total += noise_weight

        source_confidence = _safe_float(payload.get("confidence", 0.0))
        if source_confidence > 0.0:
            confidence_total += source_confidence
            confidence_count += 1

        floor = _safe_float(row.confidence_floor, default=0.0)
        confidence_above_floor = source_confidence >= max(floor, 0.55)
        healthy = str(row.health_status or "").strip() == "healthy"

        if source_type == "camera":
            observations = (
                payload.get("observations", [])
                if isinstance(payload.get("observations", []), list)
                else []
            )
            latest_snapshot = {
                "status": status or ("accepted" if row.last_accepted_at else "unknown"),
                "confidence": round(source_confidence, 6),
                "freshness": freshness,
                "labels": _latest_camera_labels(payload),
                "zone": str(payload.get("zone", "")).strip(),
                "source_id": int(row.id),
                "device_id": row.device_id,
                "health_status": row.health_status,
                "observation_count": len(observations),
            }
            if latest_camera_timestamp is None or (
                event_timestamp is not None and event_timestamp > latest_camera_timestamp
            ):
                latest_camera_timestamp = event_timestamp
                latest_camera = latest_snapshot

            if (
                status == "accepted"
                and confidence_above_floor
                and healthy
                and freshness_weight >= 0.2
                and latest_snapshot["labels"]
            ):
                candidate = _bounded(
                    (source_confidence * 0.6)
                    + (freshness_weight * 0.4)
                    - (noise_weight * 0.35)
                )
                camera_grounding_weight = max(camera_grounding_weight, candidate)

        if source_type == "microphone":
            transcript = str(payload.get("transcript", "")).strip()
            heartbeat_only = status == "heartbeat_no_transcript" or (
                not transcript and status != "accepted"
            )
            latest_snapshot = {
                "status": status or ("accepted" if row.last_accepted_at else "unknown"),
                "confidence": round(source_confidence, 6),
                "freshness": freshness,
                "source_id": int(row.id),
                "device_id": row.device_id,
                "health_status": row.health_status,
                "heartbeat_only": heartbeat_only,
                "transcript_present": bool(transcript),
            }
            if latest_microphone_timestamp is None or (
                event_timestamp is not None
                and event_timestamp > latest_microphone_timestamp
            ):
                latest_microphone_timestamp = event_timestamp
                latest_microphone = latest_snapshot

            if (
                status == "accepted"
                and bool(transcript)
                and confidence_above_floor
                and healthy
                and freshness_weight >= 0.2
            ):
                candidate = _bounded(
                    (source_confidence * 0.55)
                    + (freshness_weight * 0.45)
                    - (noise_weight * 0.3)
                )
                mic_grounding_weight = max(mic_grounding_weight, candidate)

    avg_confidence = round(confidence_total / float(max(1, confidence_count)), 6)
    avg_freshness = round(freshness_total / float(max(1, source_count)), 6)
    sensor_noise_weight = round(noise_total / float(max(1, source_count)), 6)
    perception_grounding_weight = round(
        _bounded(
            (camera_grounding_weight * 0.65)
            + (mic_grounding_weight * 0.2)
            + (avg_confidence * 0.05)
            + (avg_freshness * 0.1)
            - (sensor_noise_weight * 0.15)
        ),
        6,
    )

    return {
        "source_count": source_count,
        "active_source_count": active_source_count,
        "camera_source_count": camera_source_count,
        "mic_source_count": mic_source_count,
        "stale_source_count": stale_source_count,
        "degraded_source_count": degraded_source_count,
        "avg_confidence": avg_confidence,
        "freshness_weight": avg_freshness,
        "sensor_noise_weight": sensor_noise_weight,
        "camera_grounding_weight": round(camera_grounding_weight, 6),
        "mic_grounding_weight": round(mic_grounding_weight, 6),
        "perception_grounding_weight": perception_grounding_weight,
        "latest_camera": latest_camera,
        "latest_microphone": latest_microphone,
    }


def _perception_grounding_classification(
    *, trigger_counts: dict, perception_summary: dict
) -> str:
    if not isinstance(perception_summary, dict) or _safe_int(
        perception_summary.get("source_count", 0)
    ) <= 0:
        return "insufficient_signal"

    sensor_noise_weight = _safe_float(
        perception_summary.get("sensor_noise_weight", 0.0)
    )
    camera_grounding_weight = _safe_float(
        perception_summary.get("camera_grounding_weight", 0.0)
    )
    mic_grounding_weight = _safe_float(
        perception_summary.get("mic_grounding_weight", 0.0)
    )
    perception_grounding_weight = _safe_float(
        perception_summary.get("perception_grounding_weight", 0.0)
    )
    world_shift_detected = (
        _safe_int(trigger_counts.get("environment_shift_during_execution", 0)) > 0
        or _safe_int(trigger_counts.get("simulation_reality_mismatch", 0)) > 0
    )
    execution_instability_detected = (
        _safe_int(trigger_counts.get("execution_slower_than_expected", 0)) > 0
        or _safe_int(trigger_counts.get("retry_instability_detected", 0)) > 0
        or _safe_int(trigger_counts.get("fallback_path_used", 0)) > 0
    )

    if sensor_noise_weight >= 0.6 and camera_grounding_weight < 0.55:
        return "sensor_noise"
    if world_shift_detected and camera_grounding_weight >= 0.55 and sensor_noise_weight < 0.6:
        if execution_instability_detected:
            return "mixed"
        return "world_drift"
    if execution_instability_detected and (
        camera_grounding_weight < 0.55 or sensor_noise_weight >= 0.45
    ):
        return "execution_drift"
    if perception_grounding_weight >= 0.45 and mic_grounding_weight >= 0.45:
        return "mixed"
    return "insufficient_signal"


def _governance_state_from_decision(decision: str) -> str:
    if decision in {"monitor_only"}:
        return "stable"
    if decision in {"increase_visibility", "prioritize_improvement"}:
        return "elevated"
    return "critical"


def _downstream_actions(*, decision: str) -> dict:
    base = {
        "strategy_weight_delta": 0.0,
        "improvement_priority_delta": 0.0,
        "preferred_backlog_decision": "",
        "autonomy_level_cap": "",
        "maintenance_auto_execute_allowed": True,
        "stewardship_auto_execute_allowed": True,
        "stewardship_weight_multiplier": 1.0,
        "visibility_only": False,
    }
    if decision == "increase_visibility":
        return {
            **base,
            "strategy_weight_delta": 0.06,
            "improvement_priority_delta": 0.05,
            "stewardship_weight_multiplier": 1.1,
            "visibility_only": True,
        }
    if decision == "prioritize_improvement":
        return {
            **base,
            "strategy_weight_delta": 0.08,
            "improvement_priority_delta": 0.18,
            "preferred_backlog_decision": "request_operator_review",
            "stewardship_weight_multiplier": 1.15,
            "visibility_only": True,
        }
    if decision == "lower_autonomy_boundary":
        return {
            **base,
            "strategy_weight_delta": 0.12,
            "improvement_priority_delta": 0.12,
            "preferred_backlog_decision": "request_operator_review",
            "autonomy_level_cap": "bounded_auto",
            "maintenance_auto_execute_allowed": False,
            "stewardship_auto_execute_allowed": False,
            "stewardship_weight_multiplier": 1.25,
        }
    if decision == "require_sandbox_experiment":
        return {
            **base,
            "strategy_weight_delta": 0.1,
            "improvement_priority_delta": 0.22,
            "preferred_backlog_decision": "auto_experiment",
            "autonomy_level_cap": "operator_required",
            "maintenance_auto_execute_allowed": False,
            "stewardship_auto_execute_allowed": False,
            "stewardship_weight_multiplier": 1.2,
        }
    if decision == "escalate_to_operator":
        return {
            **base,
            "strategy_weight_delta": 0.14,
            "improvement_priority_delta": 0.16,
            "preferred_backlog_decision": "request_operator_review",
            "autonomy_level_cap": "operator_required",
            "maintenance_auto_execute_allowed": False,
            "stewardship_auto_execute_allowed": False,
            "stewardship_weight_multiplier": 1.35,
        }
    return base


def _decision_from_evidence(
    *, summary: dict, trigger_counts: dict, stewardship: dict, perception_summary: dict
) -> tuple[str, str, float, dict]:
    execution_count = _safe_int(summary.get("execution_count", 0))
    signal_count = _safe_int(summary.get("deviation_signal_count", 0))
    freshness_weight = _safe_float(trigger_counts.get("freshness_weight", 0.0))
    avg_truth_confidence = _safe_float(
        trigger_counts.get("avg_truth_confidence", 0.0)
    )
    high_confidence_executions = _safe_int(
        trigger_counts.get("high_confidence_executions", 0)
    )
    perception_grounding_weight = _safe_float(
        perception_summary.get("perception_grounding_weight", 0.0)
    )
    perception_sensor_noise_weight = _safe_float(
        perception_summary.get("sensor_noise_weight", 0.0)
    )
    perception_classification = _perception_grounding_classification(
        trigger_counts=trigger_counts,
        perception_summary=perception_summary,
    )
    evidence_quality = _bounded(
        (avg_truth_confidence * 0.45)
        + (_bounded(execution_count / 4.0) * 0.2)
        + (_bounded(signal_count / 6.0) * 0.2)
        + (freshness_weight * 0.15)
        + (perception_grounding_weight * 0.12)
        - (perception_sensor_noise_weight * 0.1)
    )

    repeated_latency_drift = (
        _safe_int(trigger_counts.get("execution_slower_than_expected", 0)) >= 2
    )
    rising_retry_density = _safe_float(
        trigger_counts.get("retry_density", 0.0)
    ) >= 0.4 and _safe_int(trigger_counts.get("retry_instability_detected", 0)) >= 2
    repeated_fallback_dependence = (
        _safe_int(trigger_counts.get("fallback_path_used", 0)) >= 2
    )
    simulation_mismatch_clusters = (
        _safe_int(trigger_counts.get("simulation_reality_mismatch", 0)) >= 2
    )
    stewardship_correlation = (
        _safe_int(stewardship.get("correlated_stewardship_cycles", 0)) >= 2
    )
    severe_runtime_instability = (
        _safe_int(trigger_counts.get("retry_instability_detected", 0))
        + _safe_int(trigger_counts.get("fallback_path_used", 0))
        + _safe_int(trigger_counts.get("simulation_reality_mismatch", 0))
    ) >= 4

    evidence = {
        "repeated_latency_drift": repeated_latency_drift,
        "rising_retry_density": rising_retry_density,
        "repeated_fallback_dependence": repeated_fallback_dependence,
        "simulation_mismatch_clusters": simulation_mismatch_clusters,
        "stewardship_drift_correlation": stewardship_correlation,
        "severe_runtime_instability": severe_runtime_instability,
        "evidence_quality": round(evidence_quality, 6),
        "avg_truth_confidence": round(avg_truth_confidence, 6),
        "high_confidence_executions": high_confidence_executions,
        "freshness_weight": round(freshness_weight, 6),
        "perception_grounding_classification": perception_classification,
        "perception_grounding_weight": round(perception_grounding_weight, 6),
        "perception_sensor_noise_weight": round(
            perception_sensor_noise_weight, 6
        ),
    }

    if execution_count <= 0 or signal_count <= 0:
        return (
            "monitor_only",
            "No execution-truth drift signals were available for governance.",
            evidence_quality,
            evidence,
        )

    if evidence_quality < 0.55 or high_confidence_executions <= 0:
        return (
            "monitor_only",
            "Execution-truth evidence is too weak or too low-confidence to change governance state.",
            evidence_quality,
            evidence,
        )

    if (
        perception_classification == "sensor_noise"
        and not stewardship_correlation
        and not severe_runtime_instability
        and not repeated_latency_drift
        and not rising_retry_density
        and not repeated_fallback_dependence
        and simulation_mismatch_clusters
    ):
        return (
            "increase_visibility",
            "Recent perception grounding is too noisy to treat runtime mismatch as confirmed world drift, so governance is limited to visibility until cleaner sensor evidence arrives.",
            evidence_quality,
            evidence,
        )

    if stewardship_correlation and (
        simulation_mismatch_clusters
        or repeated_fallback_dependence
        or severe_runtime_instability
    ):
        return (
            "escalate_to_operator",
            "Repeated execution-truth drift is correlating with persistent stewardship degradation, so operator escalation is required.",
            evidence_quality,
            evidence,
        )
    if stewardship_correlation or severe_runtime_instability:
        return (
            "lower_autonomy_boundary",
            "Execution-truth drift is persistent enough to lower autonomy until runtime stability improves.",
            evidence_quality,
            evidence,
        )
    if simulation_mismatch_clusters or repeated_fallback_dependence:
        if perception_classification == "world_drift":
            return (
                "require_sandbox_experiment",
                "Fresh high-confidence perception corroborates world drift, so the current path should be sandboxed before further trust is granted.",
                evidence_quality,
                evidence,
            )
        return (
            "require_sandbox_experiment",
            "Execution-truth drift indicates the current path should be sandboxed before further trust is granted.",
            evidence_quality,
            evidence,
        )
    if repeated_latency_drift or rising_retry_density:
        return (
            "prioritize_improvement",
            "Repeated latency and retry drift should move runtime hardening higher in the improvement queue.",
            evidence_quality,
            evidence,
        )
    return (
        "increase_visibility",
        "Execution-truth drift is real but not yet severe enough to reduce autonomy automatically.",
        evidence_quality,
        evidence,
    )


async def evaluate_execution_truth_governance(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    lookback_hours: int,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceExecutionTruthGovernanceProfile:
    scope = str(managed_scope or "").strip() or "global"
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))

    execution_rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.created_at >= since)
            .order_by(CapabilityExecution.id.desc())
            .limit(500)
        )
    ).scalars().all()
    summary = summarize_execution_truth(
        execution_rows,
        managed_scope=scope,
        max_age_hours=max(1, int(lookback_hours)),
    )

    stewardship_rows = (
        await db.execute(
            select(WorkspaceStewardshipCycle)
            .where(WorkspaceStewardshipCycle.created_at >= since)
            .order_by(WorkspaceStewardshipCycle.id.desc())
            .limit(200)
        )
    ).scalars().all()
    stewardship_rows = [
        row for row in stewardship_rows if _stewardship_scope_matches(row, scope)
    ]

    perception_rows = (
        await db.execute(
            select(WorkspacePerceptionSource)
            .order_by(WorkspacePerceptionSource.id.desc())
            .limit(200)
        )
    ).scalars().all()
    perception_rows = [
        row
        for row in perception_rows
        if _perception_scope_matches(row=row, managed_scope=scope)
        and (
            (row.last_seen_at is not None and row.last_seen_at >= since)
            or (row.last_accepted_at is not None and row.last_accepted_at >= since)
            or row.created_at >= since
        )
    ]

    trigger_counts = _trigger_counts_from_summary(summary=summary)
    stewardship_correlation = _recent_stewardship_correlation(rows=stewardship_rows)
    perception_summary = _perception_signal_summary(
        rows=perception_rows,
        lookback_hours=max(1, int(lookback_hours)),
    )
    decision, reason, confidence, evidence = _decision_from_evidence(
        summary=summary,
        trigger_counts=trigger_counts,
        stewardship=stewardship_correlation,
        perception_summary=perception_summary,
    )
    downstream_actions = _downstream_actions(decision=decision)

    row = WorkspaceExecutionTruthGovernanceProfile(
        source=source,
        actor=actor,
        managed_scope=scope,
        status="active",
        lookback_hours=max(1, int(lookback_hours)),
        execution_count=_safe_int(summary.get("execution_count", 0)),
        signal_count=_safe_int(summary.get("deviation_signal_count", 0)),
        confidence=round(_bounded(confidence), 6),
        governance_state=_governance_state_from_decision(decision),
        governance_decision=decision,
        governance_reason=reason,
        trigger_counts_json={
            **trigger_counts,
            **stewardship_correlation,
            "perception_source_count": _safe_int(
                perception_summary.get("source_count", 0)
            ),
            "active_perception_source_count": _safe_int(
                perception_summary.get("active_source_count", 0)
            ),
            "perception_stale_source_count": _safe_int(
                perception_summary.get("stale_source_count", 0)
            ),
        },
        trigger_evidence_json={
            **evidence,
            "perception_grounding": perception_summary,
        },
        downstream_actions_json=downstream_actions,
        reasoning_json={
            "thresholds": {
                "minimum_evidence_quality": 0.55,
                "repeated_latency_drift_count": 2,
                "retry_density_threshold": 0.4,
                "repeated_fallback_dependence_count": 2,
                "simulation_mismatch_cluster_count": 2,
                "stewardship_correlation_cycles": 2,
                "perception_sensor_noise_threshold": 0.6,
                "perception_world_grounding_threshold": 0.55,
            },
            "decision_trace": {
                "decision": decision,
                "reason": reason,
            },
            "perception_grounding": perception_summary,
        },
        execution_truth_summary_json=summary,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective81_execution_truth_governance": True,
            "objective82_live_perception_grounding": True,
        },
    )
    db.add(row)
    await db.flush()
    return row


async def list_execution_truth_governance_profiles(
    *, managed_scope: str, limit: int, db: AsyncSession
) -> list[WorkspaceExecutionTruthGovernanceProfile]:
    rows = (
        await db.execute(
            select(WorkspaceExecutionTruthGovernanceProfile)
            .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
            .limit(max(1, min(500, int(limit))))
        )
    ).scalars().all()
    scope = str(managed_scope or "").strip()
    if not scope:
        return rows
    return [row for row in rows if _scope_matches(row=row, managed_scope=scope)]


async def get_execution_truth_governance_profile(
    *, governance_id: int, db: AsyncSession
) -> WorkspaceExecutionTruthGovernanceProfile | None:
    return (
        await db.execute(
            select(WorkspaceExecutionTruthGovernanceProfile).where(
                WorkspaceExecutionTruthGovernanceProfile.id == governance_id
            )
        )
    ).scalars().first()


async def latest_execution_truth_governance_snapshot(
    *, managed_scope: str, db: AsyncSession
) -> dict:
    scope = str(managed_scope or "").strip() or "global"
    rows = await list_execution_truth_governance_profiles(
        managed_scope=scope,
        limit=50,
        db=db,
    )
    if not rows:
        return _default_governance_snapshot(managed_scope=scope)
    exact = next(
        (
            row for row in rows if str(row.managed_scope or "").strip() == scope
        ),
        None,
    )
    chosen = exact or rows[0]
    return to_execution_truth_governance_out(chosen)


def to_execution_truth_governance_out(
    row: WorkspaceExecutionTruthGovernanceProfile,
) -> dict:
    return {
        "governance_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "managed_scope": row.managed_scope,
        "status": row.status,
        "lookback_hours": int(row.lookback_hours or 0),
        "execution_count": int(row.execution_count or 0),
        "signal_count": int(row.signal_count or 0),
        "confidence": float(row.confidence or 0.0),
        "governance_state": row.governance_state,
        "governance_decision": row.governance_decision,
        "governance_reason": row.governance_reason,
        "trigger_counts": row.trigger_counts_json
        if isinstance(row.trigger_counts_json, dict)
        else {},
        "trigger_evidence": row.trigger_evidence_json
        if isinstance(row.trigger_evidence_json, dict)
        else {},
        "downstream_actions": row.downstream_actions_json
        if isinstance(row.downstream_actions_json, dict)
        else {},
        "reasoning": row.reasoning_json
        if isinstance(row.reasoning_json, dict)
        else {},
        "execution_truth_summary": row.execution_truth_summary_json
        if isinstance(row.execution_truth_summary_json, dict)
        else {},
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "created_at": (
            row.created_at.isoformat()
            if isinstance(row.created_at, datetime)
            else row.created_at
        ),
    }