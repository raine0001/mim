from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_readiness_service import (
    execution_readiness_confidence,
    execution_readiness_policy_effects,
    execution_readiness_precedence,
    execution_readiness_posture,
    load_latest_execution_readiness,
)
from core.execution_trace_service import append_execution_trace_event, get_execution_trace
from core.models import (
    CapabilityExecution,
    ExecutionIntent,
    ExecutionRecoveryLearningProfile,
    ExecutionOverride,
    ExecutionRecoveryAttempt,
    ExecutionRecoveryOutcome,
    ExecutionStabilityProfile,
    ExecutionTaskOrchestration,
    ExecutionTrace,
)
from core.operator_resolution_service import (
    commitment_downstream_effects,
    commitment_is_active,
    commitment_snapshot,
    latest_active_operator_resolution_commitment,
)
from core.policy_conflict_resolution_service import _candidate_payload, _resolve_policy_conflict_profile
from core.state_bus_service import (
    append_state_bus_event,
    get_state_bus_snapshot,
    to_state_bus_snapshot_out,
    upsert_state_bus_snapshot,
)


RECOVERY_SOURCE = "objective96"
RECOVERY_DECISION_FAMILY = "execution_recovery"


def _env_positive_int(name: str, fallback: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return int(fallback)
    try:
        parsed = int(raw)
    except Exception:
        return int(fallback)
    return max(1, parsed)


RECOVERY_LEARNING_DECAY_DAYS = _env_positive_int("MIM_RECOVERY_LEARNING_DECAY_DAYS", 14)
RECOVERY_LEARNING_ALERT_ESCALATED_THRESHOLD = _env_positive_int(
    "MIM_RECOVERY_LEARNING_ALERT_ESCALATED_THRESHOLD", 3
)
RECOVERY_LEARNING_ALERT_OPERATOR_REQUIRED_THRESHOLD = _env_positive_int(
    "MIM_RECOVERY_LEARNING_ALERT_OPERATOR_REQUIRED_THRESHOLD", 3
)


def _safe_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _safe_int(raw: object, fallback: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(fallback)


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _profile_age_days(created_at: datetime | None) -> float | None:
    if created_at is None:
        return None
    ts = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 86400.0)


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


def _snapshot_scope(*, managed_scope: str, trace_id: str) -> str:
    scope = str(managed_scope or "").strip() or "global"
    trace = str(trace_id or "").strip() or "latest"
    return f"execution-recovery:{scope}:{trace}"


def _capability_family(capability_name: str) -> str:
    normalized = str(capability_name or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or "general"


def _recovery_summary(recovery: dict) -> str:
    decision = str(recovery.get("recovery_decision") or "").strip().replace("_", " ")
    reason = str(recovery.get("recovery_reason") or "").strip()
    if decision and reason:
        return f"{decision}: {reason}"
    return decision or reason


async def _latest_execution(*, trace_id: str, execution_id: int | None, db: AsyncSession) -> CapabilityExecution | None:
    if execution_id is not None:
        row = await db.get(CapabilityExecution, execution_id)
        if row is not None:
            return row
    normalized_trace = str(trace_id or "").strip()
    if not normalized_trace:
        return None
    return (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.trace_id == normalized_trace)
                .order_by(CapabilityExecution.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_orchestration(*, trace_id: str, db: AsyncSession) -> ExecutionTaskOrchestration | None:
    normalized_trace = str(trace_id or "").strip()
    if not normalized_trace:
        return None
    return (
        (
            await db.execute(
                select(ExecutionTaskOrchestration)
                .where(ExecutionTaskOrchestration.trace_id == normalized_trace)
                .order_by(ExecutionTaskOrchestration.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_stability(*, trace_id: str, managed_scope: str, db: AsyncSession) -> ExecutionStabilityProfile | None:
    normalized_trace = str(trace_id or "").strip()
    if normalized_trace:
        row = (
            (
                await db.execute(
                    select(ExecutionStabilityProfile)
                    .where(ExecutionStabilityProfile.trace_id == normalized_trace)
                    .order_by(ExecutionStabilityProfile.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return row
    scope = str(managed_scope or "").strip()
    if not scope:
        return None
    return (
        (
            await db.execute(
                select(ExecutionStabilityProfile)
                .where(ExecutionStabilityProfile.managed_scope == scope)
                .order_by(ExecutionStabilityProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _active_overrides(*, managed_scope: str, db: AsyncSession) -> list[ExecutionOverride]:
    scope = str(managed_scope or "").strip()
    if not scope:
        return []
    return list(
        (
            await db.execute(
                select(ExecutionOverride)
                .where(ExecutionOverride.managed_scope == scope)
                .where(ExecutionOverride.status == "active")
                .order_by(ExecutionOverride.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def list_execution_recovery_attempts(
    *, trace_id: str, db: AsyncSession, limit: int = 20
) -> list[ExecutionRecoveryAttempt]:
    normalized_trace = str(trace_id or "").strip()
    if not normalized_trace:
        return []
    return list(
        (
            await db.execute(
                select(ExecutionRecoveryAttempt)
                .where(ExecutionRecoveryAttempt.trace_id == normalized_trace)
                .order_by(ExecutionRecoveryAttempt.id.desc())
                .limit(max(1, min(int(limit), 100)))
            )
        )
        .scalars()
        .all()
    )


async def get_latest_execution_recovery_attempt(
    *, trace_id: str, db: AsyncSession
) -> ExecutionRecoveryAttempt | None:
    rows = await list_execution_recovery_attempts(trace_id=trace_id, db=db, limit=1)
    return rows[0] if rows else None


def to_execution_recovery_attempt_out(row: ExecutionRecoveryAttempt) -> dict:
    return {
        "recovery_attempt_id": int(row.id),
        "trace_id": row.trace_id,
        "execution_id": row.execution_id,
        "managed_scope": row.managed_scope,
        "recovery_decision": row.recovery_decision,
        "recovery_reason": row.recovery_reason,
        "attempt_number": int(row.attempt_number or 0),
        "resume_step_key": row.resume_step_key,
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "result_json": _safe_dict(row.result_json),
        "metadata_json": _safe_dict(row.metadata_json),
        "created_at": row.created_at,
    }


async def list_execution_recovery_outcomes(
    *, trace_id: str, db: AsyncSession, limit: int = 20
) -> list[ExecutionRecoveryOutcome]:
    normalized_trace = str(trace_id or "").strip()
    if not normalized_trace:
        return []
    return list(
        (
            await db.execute(
                select(ExecutionRecoveryOutcome)
                .where(ExecutionRecoveryOutcome.trace_id == normalized_trace)
                .order_by(ExecutionRecoveryOutcome.id.desc())
                .limit(max(1, min(int(limit), 100)))
            )
        )
        .scalars()
        .all()
    )


async def get_latest_execution_recovery_outcome(
    *, trace_id: str, db: AsyncSession
) -> ExecutionRecoveryOutcome | None:
    rows = await list_execution_recovery_outcomes(trace_id=trace_id, db=db, limit=1)
    return rows[0] if rows else None


def to_execution_recovery_outcome_out(row: ExecutionRecoveryOutcome) -> dict:
    return {
        "recovery_outcome_id": int(row.id),
        "attempt_id": row.attempt_id,
        "trace_id": row.trace_id,
        "execution_id": row.execution_id,
        "managed_scope": row.managed_scope,
        "outcome_status": row.outcome_status,
        "outcome_reason": row.outcome_reason,
        "learning_bias_json": _safe_dict(row.learning_bias_json),
        "outcome_score": float(row.outcome_score or 0.0),
        "result_json": _safe_dict(row.result_json),
        "metadata_json": _safe_dict(row.metadata_json),
        "created_at": row.created_at,
    }


def to_execution_recovery_learning_out(row: ExecutionRecoveryLearningProfile) -> dict:
    metadata_json = _safe_dict(row.metadata_json)
    return {
        "recovery_learning_profile_id": int(row.id),
        "managed_scope": row.managed_scope,
        "capability_family": row.capability_family,
        "capability_name": row.capability_name,
        "recovery_decision": row.recovery_decision,
        "learning_state": row.learning_state,
        "escalation_decision": row.escalation_decision,
        "rationale": row.rationale,
        "why_recovery_escalated_before_retry": str(
            metadata_json.get("why_recovery_escalated_before_retry") or ""
        ).strip(),
        "confidence": float(row.confidence or 0.0),
        "sample_count": int(row.sample_count or 0),
        "recovered_count": int(row.recovered_count or 0),
        "failed_again_count": int(row.failed_again_count or 0),
        "operator_required_count": int(row.operator_required_count or 0),
        "success_rate": float(row.success_rate or 0.0),
        "evidence_json": _safe_dict(row.evidence_json),
        "policy_effects_json": _safe_dict(row.policy_effects_json),
        "metadata_json": metadata_json,
        "created_at": row.created_at,
    }


async def list_execution_recovery_learning_profiles(
    *,
    managed_scope: str,
    db: AsyncSession,
    capability_family: str = "",
    recovery_decision: str = "",
    limit: int = 20,
) -> list[ExecutionRecoveryLearningProfile]:
    scope = str(managed_scope or "").strip()
    if not scope:
        return []
    stmt = (
        select(ExecutionRecoveryLearningProfile)
        .where(ExecutionRecoveryLearningProfile.managed_scope == scope)
        .order_by(ExecutionRecoveryLearningProfile.id.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    family = str(capability_family or "").strip()
    if family:
        stmt = stmt.where(ExecutionRecoveryLearningProfile.capability_family == family)
    decision = str(recovery_decision or "").strip()
    if decision:
        stmt = stmt.where(ExecutionRecoveryLearningProfile.recovery_decision == decision)
    return list((await db.execute(stmt)).scalars().all())


async def get_latest_execution_recovery_learning_profile(
    *,
    managed_scope: str,
    db: AsyncSession,
    capability_family: str,
    recovery_decision: str,
) -> ExecutionRecoveryLearningProfile | None:
    rows = await list_execution_recovery_learning_profiles(
        managed_scope=managed_scope,
        db=db,
        capability_family=capability_family,
        recovery_decision=recovery_decision,
        limit=1,
    )
    return rows[0] if rows else None


def _recovery_learning_evidence(samples: list[dict]) -> dict:
    recent_samples = []
    for item in samples[:5]:
        recent_samples.append(
            {
                "trace_id": str(item.get("trace_id") or "").strip(),
                "outcome_status": str(item.get("outcome_status") or "").strip(),
                "recovery_decision": str(item.get("recovery_decision") or "").strip(),
                "outcome_score": round(float(item.get("outcome_score") or 0.0), 6),
                "created_at": item.get("created_at"),
            }
        )
    return {
        "recent_samples": recent_samples,
        "sample_trace_ids": [str(item.get("trace_id") or "").strip() for item in samples[:5]],
    }


def _recovery_learning_policy_effects(
    *,
    target_decision: str,
    escalation_decision: str,
    why_escalated: str,
) -> dict:
    decision = str(target_decision or "").strip()
    escalation = str(escalation_decision or "continue_bounded_recovery").strip() or "continue_bounded_recovery"
    reason = str(why_escalated or "").strip()
    if escalation == "continue_bounded_recovery":
        return {
            "recovery_decision": decision,
            "recovery_reason": reason,
            "recommended_attempt_decision": decision,
            "recovery_allowed": True,
            "operator_action_required": False,
        }
    if escalation == "replan_capability_family":
        return {
            "recovery_decision": "rollback_and_replan",
            "recovery_reason": reason,
            "recommended_attempt_decision": "rollback_and_replan",
            "recovery_allowed": False,
            "operator_action_required": True,
        }
    return {
        "recovery_decision": "require_operator_resume",
        "recovery_reason": reason,
        "recommended_attempt_decision": decision,
        "recovery_allowed": False,
        "operator_action_required": True,
    }


async def evaluate_execution_recovery_learning(
    *,
    trace_id: str,
    execution_id: int | None,
    managed_scope: str,
    capability_name: str,
    recovery_decision: str,
    environment_shift_detected: bool = False,
    db: AsyncSession,
) -> ExecutionRecoveryLearningProfile | None:
    decision = str(recovery_decision or "").strip()
    if not decision:
        return None

    execution = await _latest_execution(trace_id=trace_id, execution_id=execution_id, db=db)
    scope = str(managed_scope or getattr(execution, "managed_scope", "") or "").strip() or "global"
    current_capability_name = (
        str(capability_name or getattr(execution, "capability_name", "") or "").strip() or "execution"
    )
    family = _capability_family(current_capability_name)

    joined_rows = list(
        (
            await db.execute(
                select(ExecutionRecoveryOutcome, ExecutionRecoveryAttempt, CapabilityExecution)
                .join(
                    ExecutionRecoveryAttempt,
                    ExecutionRecoveryAttempt.id == ExecutionRecoveryOutcome.attempt_id,
                    isouter=True,
                )
                .join(
                    CapabilityExecution,
                    CapabilityExecution.id == ExecutionRecoveryOutcome.execution_id,
                    isouter=True,
                )
                .where(ExecutionRecoveryOutcome.managed_scope == scope)
                .order_by(ExecutionRecoveryOutcome.id.desc())
                .limit(40)
            )
        )
        .all()
    )

    decay_cutoff_seconds = float(RECOVERY_LEARNING_DECAY_DAYS) * 86400.0
    samples: list[dict] = []
    decayed_outcome_count = 0
    for outcome_row, attempt_row, execution_row in joined_rows:
        sample_decision = str(getattr(attempt_row, "recovery_decision", "") or "").strip()
        sample_capability_name = str(getattr(execution_row, "capability_name", "") or "").strip()
        sample_family = _capability_family(sample_capability_name)
        if sample_family != family or sample_decision != decision:
            continue
        created_at = getattr(outcome_row, "created_at", None)
        if created_at is not None:
            ts = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
            if age_seconds > decay_cutoff_seconds:
                decayed_outcome_count += 1
                continue
        samples.append(
            {
                "trace_id": str(getattr(outcome_row, "trace_id", "") or "").strip(),
                "outcome_status": str(getattr(outcome_row, "outcome_status", "") or "").strip(),
                "recovery_decision": sample_decision,
                "outcome_score": float(getattr(outcome_row, "outcome_score", 0.0) or 0.0),
                "created_at": (
                    getattr(outcome_row, "created_at", None).isoformat()
                    if getattr(outcome_row, "created_at", None) is not None
                    else None
                ),
            }
        )
        if len(samples) >= 6:
            break

    sample_count = len(samples)
    recovered_count = sum(1 for item in samples if str(item.get("outcome_status") or "") == "recovered")
    failed_again_count = sum(1 for item in samples if str(item.get("outcome_status") or "") == "failed_again")
    operator_required_count = sum(
        1 for item in samples if str(item.get("outcome_status") or "") == "operator_required"
    )
    success_rate = float(recovered_count) / float(sample_count) if sample_count > 0 else 0.0

    learning_state = "monitor_only"
    escalation_decision = "continue_bounded_recovery"
    rationale = (
        f"No repeated recovery history exists yet for {decision} in scope {scope}."
        if sample_count <= 0
        else f"Recovery learning observed {sample_count} recent scoped outcomes for {decision} in scope {scope}."
    )
    why_recovery_escalated_before_retry = ""
    confidence = 0.0

    if failed_again_count >= 2:
        learning_state = "negative_recovery_pattern"
        escalation_decision = "require_operator_takeover"
        confidence = _bounded(0.52 + (failed_again_count * 0.16) + (sample_count * 0.04))
        why_recovery_escalated_before_retry = (
            f"Repeated {decision} recoveries failed again {failed_again_count} times in scope {scope}, so the next path escalates to operator takeover before another retry."
        )
        rationale = why_recovery_escalated_before_retry
    elif operator_required_count >= 2:
        learning_state = "operator_mediated_pattern"
        escalation_decision = "lower_scope_autonomy_for_recovery"
        confidence = _bounded(0.48 + (operator_required_count * 0.14) + (sample_count * 0.04))
        why_recovery_escalated_before_retry = (
            f"Recent {decision} recoveries still required operator intervention {operator_required_count} times in scope {scope}, so recovery autonomy is lowered before another retry."
        )
        rationale = why_recovery_escalated_before_retry
    elif recovered_count >= 2 and failed_again_count == 0 and operator_required_count == 0:
        learning_state = "reinforced_recovery_path"
        escalation_decision = "continue_bounded_recovery"
        confidence = _bounded(0.44 + (recovered_count * 0.14) + (sample_count * 0.03))
        rationale = (
            f"Recent {decision} recoveries succeeded {recovered_count} times in scope {scope}, reinforcing the bounded recovery path."
        )
    elif sample_count > 0:
        confidence = _bounded(0.2 + (sample_count * 0.08))

    if sample_count == 0 and decayed_outcome_count > 0:
        learning_state = "decayed_recovery_signal"
        escalation_decision = "continue_bounded_recovery"
        confidence = 0.0
        rationale = (
            f"Recovery learning history for {decision} in scope {scope} decayed after {RECOVERY_LEARNING_DECAY_DAYS} days of inactivity."
        )

    if environment_shift_detected:
        learning_state = "invalidated_environment_shift"
        escalation_decision = "continue_bounded_recovery"
        confidence = 0.0
        sample_count = 0
        recovered_count = 0
        failed_again_count = 0
        operator_required_count = 0
        success_rate = 0.0
        rationale = (
            f"Recovery learning for {decision} in scope {scope} was invalidated after an environment shift was detected."
        )
        why_recovery_escalated_before_retry = ""

    policy_effects_json = _recovery_learning_policy_effects(
        target_decision=decision,
        escalation_decision=escalation_decision,
        why_escalated=why_recovery_escalated_before_retry or rationale,
    )

    existing = await get_latest_execution_recovery_learning_profile(
        managed_scope=scope,
        db=db,
        capability_family=family,
        recovery_decision=decision,
    )
    if existing is None:
        existing = ExecutionRecoveryLearningProfile(
            managed_scope=scope,
            capability_family=family,
            capability_name=current_capability_name,
            recovery_decision=decision,
        )
        db.add(existing)

    existing.managed_scope = scope
    existing.capability_family = family
    existing.capability_name = current_capability_name
    existing.recovery_decision = decision
    existing.learning_state = learning_state
    existing.escalation_decision = escalation_decision
    existing.rationale = rationale
    existing.confidence = float(confidence)
    existing.sample_count = int(sample_count)
    existing.recovered_count = int(recovered_count)
    existing.failed_again_count = int(failed_again_count)
    existing.operator_required_count = int(operator_required_count)
    existing.success_rate = float(success_rate)
    existing.evidence_json = _json_safe(_recovery_learning_evidence(samples))
    existing.policy_effects_json = _json_safe(policy_effects_json)
    existing.metadata_json = _json_safe(
        {
            "objective": "objective97",
            "trace_id": str(trace_id or "").strip(),
            "execution_id": execution_id,
            "why_recovery_escalated_before_retry": why_recovery_escalated_before_retry,
            "decayed_outcome_count": int(decayed_outcome_count),
            "decay_days": int(RECOVERY_LEARNING_DECAY_DAYS),
            "environment_shift_detected": bool(environment_shift_detected),
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    await db.flush()
    return existing


async def reset_execution_recovery_learning_profiles(
    *,
    managed_scope: str,
    actor: str,
    reason: str,
    db: AsyncSession,
    capability_family: str = "",
    recovery_decision: str = "",
) -> dict:
    scope = str(managed_scope or "").strip()
    if not scope:
        return {"updated": 0, "profiles": []}
    stmt = select(ExecutionRecoveryLearningProfile).where(
        ExecutionRecoveryLearningProfile.managed_scope == scope
    )
    family = str(capability_family or "").strip()
    if family:
        stmt = stmt.where(ExecutionRecoveryLearningProfile.capability_family == family)
    decision = str(recovery_decision or "").strip()
    if decision:
        stmt = stmt.where(ExecutionRecoveryLearningProfile.recovery_decision == decision)
    rows = list((await db.execute(stmt)).scalars().all())
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row.learning_state = "manual_reset"
        row.escalation_decision = "continue_bounded_recovery"
        row.rationale = str(reason or "Recovery learning profile reset by operator.").strip()
        row.confidence = 0.0
        row.sample_count = 0
        row.recovered_count = 0
        row.failed_again_count = 0
        row.operator_required_count = 0
        row.success_rate = 0.0
        row.evidence_json = {}
        row.policy_effects_json = _json_safe(
            _recovery_learning_policy_effects(
                target_decision=str(row.recovery_decision or "").strip(),
                escalation_decision="continue_bounded_recovery",
                why_escalated=str(reason or "Recovery learning profile reset by operator.").strip(),
            )
        )
        row.metadata_json = _json_safe(
            {
                **_safe_dict(row.metadata_json),
                "last_reset_at": now_iso,
                "last_reset_actor": str(actor or "operator").strip() or "operator",
                "last_reset_reason": str(reason or "").strip(),
            }
        )
    await db.flush()
    return {
        "updated": len(rows),
        "profiles": [to_execution_recovery_learning_out(row) for row in rows],
    }


async def summarize_execution_recovery_learning_telemetry(
    *,
    db: AsyncSession,
    managed_scope: str = "",
    limit: int = 200,
) -> dict:
    max_limit = max(1, min(int(limit), 1000))
    stmt = select(ExecutionRecoveryLearningProfile).order_by(ExecutionRecoveryLearningProfile.id.desc()).limit(max_limit)
    scope = str(managed_scope or "").strip()
    if scope:
        stmt = stmt.where(ExecutionRecoveryLearningProfile.managed_scope == scope)
    rows = list((await db.execute(stmt)).scalars().all())

    by_scope: dict[str, dict] = {}
    escalated_profiles = 0
    operator_burden_profiles = 0
    stale_profiles = 0
    for row in rows:
        row_scope = str(row.managed_scope or "global").strip() or "global"
        bucket = by_scope.setdefault(
            row_scope,
            {
                "profiles": 0,
                "escalated_profiles": 0,
                "operator_required_total": 0,
                "stale_profiles": 0,
                "average_confidence": 0.0,
            },
        )
        bucket["profiles"] += 1
        bucket["operator_required_total"] += int(row.operator_required_count or 0)
        bucket["average_confidence"] += float(row.confidence or 0.0)
        age_days = _profile_age_days(row.created_at)
        is_stale = bool(age_days is not None and age_days > float(RECOVERY_LEARNING_DECAY_DAYS))
        if is_stale:
            bucket["stale_profiles"] += 1
            stale_profiles += 1
        if str(row.escalation_decision or "").strip() != "continue_bounded_recovery":
            bucket["escalated_profiles"] += 1
            escalated_profiles += 1
        if int(row.operator_required_count or 0) >= RECOVERY_LEARNING_ALERT_OPERATOR_REQUIRED_THRESHOLD:
            operator_burden_profiles += 1

    for bucket in by_scope.values():
        profiles = int(bucket.get("profiles", 0) or 0)
        bucket["average_confidence"] = round(
            float(bucket.get("average_confidence", 0.0) or 0.0) / float(profiles), 6
        ) if profiles > 0 else 0.0

    alerts = {
        "escalation_rate_high": escalated_profiles >= RECOVERY_LEARNING_ALERT_ESCALATED_THRESHOLD,
        "operator_burden_high": operator_burden_profiles >= RECOVERY_LEARNING_ALERT_OPERATOR_REQUIRED_THRESHOLD,
        "stale_learning_profiles_present": stale_profiles > 0,
    }

    return {
        "managed_scope": scope,
        "window": {
            "sampled_profiles": len(rows),
            "max_profiles": max_limit,
            "decay_days": int(RECOVERY_LEARNING_DECAY_DAYS),
        },
        "metrics": {
            "escalated_profiles": escalated_profiles,
            "operator_burden_profiles": operator_burden_profiles,
            "stale_profiles": stale_profiles,
            "scopes": by_scope,
        },
        "alerts": alerts,
    }


async def publish_execution_recovery_state(
    *,
    db: AsyncSession,
    actor: str,
    source: str,
    recovery_state: dict,
    metadata_json: dict | None = None,
) -> dict:
    normalized = _safe_dict(_json_safe(recovery_state))
    objective_tag = "objective97" if isinstance(normalized.get("recovery_learning"), dict) else "objective96"
    snapshot_scope = _snapshot_scope(
        managed_scope=str(normalized.get("managed_scope") or "global"),
        trace_id=str(normalized.get("trace_id") or "latest"),
    )
    existing_snapshot = await get_state_bus_snapshot(snapshot_scope=snapshot_scope, db=db)
    existing_payload = (
        existing_snapshot.state_payload_json
        if existing_snapshot is not None and isinstance(existing_snapshot.state_payload_json, dict)
        else {}
    )
    changed = existing_payload != normalized
    event = None
    event_type = "recovery_state_unchanged"
    if changed:
        previous_decision = str(existing_payload.get("recovery_decision") or "").strip()
        current_decision = str(normalized.get("recovery_decision") or "").strip()
        current_required = bool(normalized.get("operator_action_required", False))
        current_allowed = bool(normalized.get("recovery_allowed", False))
        if current_required:
            event_type = "recovery_operator_required"
        elif current_allowed:
            event_type = "recovery_available"
        elif previous_decision and previous_decision != current_decision:
            event_type = "recovery_changed"
        else:
            event_type = "recovery_updated"
        event = await append_state_bus_event(
            actor=actor,
            source=source or RECOVERY_SOURCE,
            event_domain="tod.runtime",
            event_type=event_type,
            stream_key=snapshot_scope,
            payload_json={"recovery": normalized, "previous": existing_payload},
            metadata_json={
                "objective": objective_tag,
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
            db=db,
        )

    snapshot = await upsert_state_bus_snapshot(
        actor=actor,
        source=source or RECOVERY_SOURCE,
        snapshot_scope=snapshot_scope,
        state_payload_json=normalized,
        last_event_id=int(event.id) if event is not None else None,
        metadata_json={
            "objective": objective_tag,
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


def _commitment_requires_recovery_review(commitment: object) -> bool:
    if commitment is None or not commitment_is_active(commitment):
        return False
    effects = commitment_downstream_effects(commitment)
    requested_level = str(
        effects.get("autonomy_level") or effects.get("autonomy_level_cap") or ""
    ).strip()
    return str(getattr(commitment, "decision_type", "") or "").strip() in {
        "defer_action",
        "require_additional_evidence",
        "lower_autonomy_for_scope",
    } or requested_level in {"manual_only", "operator_required"}


def _conflict_recovery_effects(
    *,
    recovery_decision: str,
    recovery_reason: str,
    recommended_attempt_decision: str,
    recovery_allowed: bool,
    operator_action_required: bool,
) -> dict:
    return {
        "recovery_decision": str(recovery_decision or "").strip(),
        "recovery_reason": str(recovery_reason or "").strip(),
        "recommended_attempt_decision": str(recommended_attempt_decision or "").strip(),
        "recovery_allowed": bool(recovery_allowed),
        "operator_action_required": bool(operator_action_required),
    }


async def _resolve_execution_recovery_conflict(
    *,
    db: AsyncSession,
    actor: str,
    source: str,
    managed_scope: str,
    trace_id: str,
    execution_id: int | None,
    capability_name: str,
    requested_executor: str,
    execution_status: str,
    retry_pressure: int,
    mitigation_state: str,
    checkpoint_json: dict,
    base_decision: str,
    base_reason: str,
    base_recommended_attempt_decision: str,
    base_recovery_allowed: bool,
    base_operator_action_required: bool,
    latest_outcome: ExecutionRecoveryOutcome | None,
    recovery_learning: dict | None,
) -> dict:
    scope = str(managed_scope or "").strip() or "global"
    readiness = load_latest_execution_readiness(
        action="execution_recovery",
        capability_name=capability_name,
        managed_scope=scope,
        requested_executor=requested_executor,
        metadata_json={"trace_id": trace_id, "execution_id": execution_id},
    )
    candidates = [
        _candidate_payload(
            source="recovery_policy",
            posture="caution" if base_operator_action_required else "promote",
            precedence_rank=45.0,
            confidence=0.78 if base_recovery_allowed else 0.68,
            freshness_weight=1.0,
            rationale=base_reason,
            snapshot={
                "execution_status": execution_status,
                "retry_pressure": retry_pressure,
                "mitigation_state": mitigation_state,
                "checkpoint_json": checkpoint_json,
            },
            policy_effects_json=_conflict_recovery_effects(
                recovery_decision=base_decision,
                recovery_reason=base_reason,
                recommended_attempt_decision=base_recommended_attempt_decision,
                recovery_allowed=base_recovery_allowed,
                operator_action_required=base_operator_action_required,
            ),
        )
    ]

    candidates.append(
        _candidate_payload(
            source="execution_readiness",
            posture=execution_readiness_posture(readiness),
            precedence_rank=execution_readiness_precedence(
                readiness,
                blocking_rank=110.0,
                advisory_rank=72.0,
                ready_rank=40.0,
            ),
            confidence=execution_readiness_confidence(readiness),
            freshness_weight=1.0,
            rationale=str(readiness.get("detail") or "execution readiness policy enforced").strip(),
            snapshot=readiness,
            policy_effects_json={
                **execution_readiness_policy_effects(readiness=readiness, surface="execution"),
                **_conflict_recovery_effects(
                    recovery_decision=(
                        "no_recovery_available"
                        if str(readiness.get("policy_outcome") or "allow").strip().lower() == "block"
                        else "require_operator_resume"
                    ),
                    recovery_reason=str(readiness.get("detail") or "Execution readiness constrained recovery.").strip(),
                    recommended_attempt_decision=(
                        base_recommended_attempt_decision
                        if str(readiness.get("policy_outcome") or "allow").strip().lower() == "degrade"
                        else ""
                    ),
                    recovery_allowed=str(readiness.get("policy_outcome") or "allow").strip().lower() == "allow",
                    operator_action_required=str(readiness.get("policy_outcome") or "allow").strip().lower() != "allow",
                ),
            },
        )
    )

    commitment = await latest_active_operator_resolution_commitment(scope=scope, db=db, limit=20)
    if commitment is not None and commitment_is_active(commitment):
        candidates.append(
            _candidate_payload(
                source="operator_commitment",
                posture="caution" if _commitment_requires_recovery_review(commitment) else "promote",
                precedence_rank=100.0,
                confidence=float(getattr(commitment, "confidence", 0.0) or 0.0),
                freshness_weight=1.0,
                rationale=str(getattr(commitment, "reason", "") or "").strip(),
                snapshot=commitment_snapshot(commitment),
                policy_effects_json=_conflict_recovery_effects(
                    recovery_decision="require_operator_resume" if _commitment_requires_recovery_review(commitment) else base_decision,
                    recovery_reason="Active operator commitment requires bounded operator-mediated recovery.",
                    recommended_attempt_decision=base_recommended_attempt_decision,
                    recovery_allowed=not _commitment_requires_recovery_review(commitment) and base_recovery_allowed,
                    operator_action_required=_commitment_requires_recovery_review(commitment),
                ),
            )
        )

    override_rows = await _active_overrides(managed_scope=scope, db=db)
    override_types = [
        str(row.override_type or "").strip()
        for row in override_rows
        if str(row.override_type or "").strip()
    ]
    if "hard_stop" in override_types:
        candidates.append(
            _candidate_payload(
                source="execution_override",
                posture="caution",
                precedence_rank=120.0,
                confidence=1.0,
                freshness_weight=1.0,
                rationale="Active hard-stop override blocks autonomous recovery.",
                snapshot={"override_types": override_types},
                policy_effects_json=_conflict_recovery_effects(
                    recovery_decision="hard_stop_persisted",
                    recovery_reason="Active hard-stop override blocks autonomous recovery.",
                    recommended_attempt_decision="",
                    recovery_allowed=False,
                    operator_action_required=True,
                ),
            )
        )
    elif "pause" in override_types:
        candidates.append(
            _candidate_payload(
                source="execution_override",
                posture="caution",
                precedence_rank=115.0,
                confidence=1.0,
                freshness_weight=1.0,
                rationale="Active pause override requires explicit operator resume.",
                snapshot={"override_types": override_types},
                policy_effects_json=_conflict_recovery_effects(
                    recovery_decision="require_operator_resume",
                    recovery_reason="Active pause override requires explicit operator resume.",
                    recommended_attempt_decision=base_recommended_attempt_decision,
                    recovery_allowed=False,
                    operator_action_required=True,
                ),
            )
        )

    learning_snapshot = _safe_dict(recovery_learning if isinstance(recovery_learning, dict) else {})
    learning_effects = _safe_dict(learning_snapshot.get("policy_effects_json", {}))
    learning_escalation = str(learning_snapshot.get("escalation_decision") or "").strip()
    latest_outcome_status = str(latest_outcome.outcome_status or "").strip() if latest_outcome is not None else ""
    stability_guardrail_required = False
    if mitigation_state == "hard_stop_active" or retry_pressure >= 2:
        stability_guardrail_required = True
    elif mitigation_state == "review_required":
        stability_guardrail_required = latest_outcome_status in {"failed_again", "operator_required"} or learning_escalation in {
            "require_operator_takeover",
            "pause_scope_for_review",
            "replan_capability_family",
            "lower_scope_autonomy_for_recovery",
        }

    if stability_guardrail_required:
        stability_precedence = 82.0
        if mitigation_state == "review_required" and learning_escalation in {
            "require_operator_takeover",
            "pause_scope_for_review",
            "replan_capability_family",
            "lower_scope_autonomy_for_recovery",
        }:
            stability_precedence = 74.0
        candidates.append(
            _candidate_payload(
                source="stability_guardrail",
                posture="caution",
                precedence_rank=stability_precedence,
                confidence=0.84,
                freshness_weight=1.0,
                rationale="Stability posture requires rollback or operator-mediated recovery.",
                snapshot={
                    "mitigation_state": mitigation_state,
                    "retry_pressure": retry_pressure,
                },
                policy_effects_json=_conflict_recovery_effects(
                    recovery_decision="rollback_and_replan",
                    recovery_reason="Stability guardrails require rollback and replan instead of repeating recovery.",
                    recommended_attempt_decision="rollback_and_replan",
                    recovery_allowed=False,
                    operator_action_required=True,
                ),
            )
        )

    if latest_outcome is not None:
        outcome_status = str(latest_outcome.outcome_status or "").strip()
        if outcome_status == "failed_again":
            candidates.append(
                _candidate_payload(
                    source="recovery_outcome_learning",
                    posture="caution",
                    precedence_rank=78.0,
                    confidence=max(0.72, float(latest_outcome.outcome_score or 0.0)),
                    freshness_weight=1.0,
                    rationale=str(latest_outcome.outcome_reason or "").strip(),
                    snapshot=to_execution_recovery_outcome_out(latest_outcome),
                    policy_effects_json=_conflict_recovery_effects(
                        recovery_decision="rollback_and_replan",
                        recovery_reason="Recent accepted recovery attempt failed again, so rollback and replan is safer than repeating the same path.",
                        recommended_attempt_decision="rollback_and_replan",
                        recovery_allowed=False,
                        operator_action_required=True,
                    ),
                )
            )
        elif outcome_status == "operator_required":
            candidates.append(
                _candidate_payload(
                    source="recovery_outcome_learning",
                    posture="caution",
                    precedence_rank=76.0,
                    confidence=max(0.68, float(latest_outcome.outcome_score or 0.0)),
                    freshness_weight=1.0,
                    rationale=str(latest_outcome.outcome_reason or "").strip(),
                    snapshot=to_execution_recovery_outcome_out(latest_outcome),
                    policy_effects_json=_conflict_recovery_effects(
                        recovery_decision="require_operator_resume",
                        recovery_reason="Recent recovery outcome still requires explicit operator intervention.",
                        recommended_attempt_decision=base_recommended_attempt_decision,
                        recovery_allowed=False,
                        operator_action_required=True,
                    ),
                )
            )

    if learning_effects and _safe_int(learning_snapshot.get("sample_count", 0), 0) > 0:
        candidates.append(
            _candidate_payload(
                source="recovery_learning",
                posture="caution" if learning_escalation != "continue_bounded_recovery" else "promote",
                precedence_rank=79.0 if learning_escalation != "continue_bounded_recovery" else 42.0,
                confidence=max(0.52, float(learning_snapshot.get("confidence", 0.0) or 0.0)),
                freshness_weight=1.0,
                rationale=str(
                    learning_snapshot.get("why_recovery_escalated_before_retry")
                    or learning_snapshot.get("rationale")
                    or ""
                ).strip(),
                snapshot=learning_snapshot,
                policy_effects_json=learning_effects,
            )
        )

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=RECOVERY_DECISION_FAMILY,
        proposal_type=str(execution_status or "recovery").strip() or "recovery",
        actor=actor,
        proposal_id=None,
        candidates=candidates,
        metadata_json={
            "trace_id": trace_id,
            "objective": "objective96",
            "source": source,
        },
        effect_mode="winner_always",
        effect_sources=None,
    )


async def evaluate_execution_recovery(
    *,
    trace_id: str,
    execution_id: int | None,
    managed_scope: str,
    environment_shift_detected: bool = False,
    db: AsyncSession,
) -> dict | None:
    execution = await _latest_execution(trace_id=trace_id, execution_id=execution_id, db=db)
    normalized_trace = str(trace_id or getattr(execution, "trace_id", "") or "").strip()
    trace = await get_execution_trace(trace_id=normalized_trace, db=db) if normalized_trace else None
    if execution is None and trace is None:
        return None

    scope = (
        str(managed_scope or "").strip()
        or str(getattr(execution, "managed_scope", "") or "").strip()
        or str(getattr(trace, "managed_scope", "") or "").strip()
        or "global"
    )
    orchestration = await _latest_orchestration(trace_id=normalized_trace, db=db)
    stability = await _latest_stability(trace_id=normalized_trace, managed_scope=scope, db=db)
    overrides = await _active_overrides(managed_scope=scope, db=db)
    attempts = await list_execution_recovery_attempts(trace_id=normalized_trace, db=db, limit=50)
    latest_outcome = await get_latest_execution_recovery_outcome(trace_id=normalized_trace, db=db)

    override_types = [str(row.override_type or "").strip() for row in overrides if str(row.override_type or "").strip()]
    checkpoint = _safe_dict(getattr(orchestration, "checkpoint_json", {}))
    feedback = _safe_dict(getattr(execution, "feedback_json", {}))
    retry_pressure = max(
        _safe_int(getattr(orchestration, "retry_count", 0), 0),
        _safe_int(feedback.get("retry_count", 0), 0),
        len(attempts),
    )
    metrics = _safe_dict(getattr(stability, "metrics_json", {}))
    mitigation_state = str(getattr(stability, "mitigation_state", "monitor_only") or "monitor_only").strip() or "monitor_only"
    execution_status = str(getattr(execution, "status", getattr(trace, "lifecycle_status", "")) or "unknown").strip() or "unknown"
    latest_outcome_status = str(latest_outcome.outcome_status or "").strip() if latest_outcome is not None else ""
    inferred_environment_shift = bool(environment_shift_detected)
    dispatch_decision = str(getattr(execution, "dispatch_decision", "") or "").strip()
    capability_name = str(
        getattr(execution, "capability_name", "") or getattr(trace, "capability_name", "") or "execution"
    ).strip() or "execution"
    current_step = str(
        getattr(orchestration, "current_step_key", "")
        or checkpoint.get("latest_step_key")
        or getattr(trace, "current_stage", "")
        or execution_status
    ).strip()
    has_checkpoint = bool(checkpoint)

    recovery_decision = "no_recovery_available"
    recommended_attempt_decision = ""
    recovery_reason = "Execution is not eligible for recovery."
    operator_action_required = False
    recovery_allowed = False

    if "hard_stop" in override_types:
        recovery_decision = "hard_stop_persisted"
        recovery_reason = "Active hard-stop override blocks autonomous recovery."
        operator_action_required = True
    elif "pause" in override_types:
        recovery_decision = "require_operator_resume"
        recommended_attempt_decision = "resume_from_checkpoint" if has_checkpoint else "restart_execution"
        recovery_reason = "Active pause override requires explicit operator resume."
        operator_action_required = True
    elif execution_status == "failed":
        if retry_pressure >= 2 or (
            mitigation_state == "review_required" and latest_outcome_status in {"failed_again", "operator_required"}
        ):
            recovery_decision = "rollback_and_replan"
            recovery_reason = "Retry pressure or instability requires rollback and replan instead of another bounded retry."
            operator_action_required = True
        elif has_checkpoint:
            recovery_decision = "retry_current_step"
            recommended_attempt_decision = "retry_current_step"
            recovery_reason = "Failure occurred with a durable checkpoint, so the next bounded action is retrying the current step."
            recovery_allowed = True
        else:
            recovery_decision = "restart_execution"
            recommended_attempt_decision = "restart_execution"
            recovery_reason = "Failure occurred before a usable checkpoint, so restarting the execution is the safest bounded action."
            recovery_allowed = True
    elif execution_status == "blocked":
        if dispatch_decision == "requires_confirmation" or "pause" in str(getattr(execution, "reason", "") or "").lower():
            recovery_decision = "require_operator_resume"
            recommended_attempt_decision = "resume_from_checkpoint" if has_checkpoint else "restart_execution"
            recovery_reason = "The blocked execution is waiting on explicit operator resume."
            operator_action_required = True
        elif has_checkpoint:
            recovery_decision = "resume_from_checkpoint"
            recommended_attempt_decision = "resume_from_checkpoint"
            recovery_reason = "The execution is blocked without an active hard stop, and a checkpoint is available for safe resume."
            recovery_allowed = True
        else:
            recovery_decision = "no_recovery_available"
            recovery_reason = "Blocked execution has no checkpoint and no allowed autonomous recovery path."
    elif execution_status in {"pending", "pending_confirmation"}:
        recovery_decision = "require_operator_resume"
        recommended_attempt_decision = "resume_from_checkpoint" if has_checkpoint else "restart_execution"
        recovery_reason = "Pending execution requires explicit operator resume before recovery can proceed."
        operator_action_required = True
    elif execution_status in {"accepted", "running", "dispatched"}:
        recovery_decision = "no_recovery_available"
        recovery_reason = "Execution is still active, so recovery is not available yet."
    elif execution_status == "succeeded":
        recovery_decision = "no_recovery_available"
        recovery_reason = "Execution already succeeded, so no recovery action is required."

    latest_attempt = attempts[0] if attempts else None
    recovery_learning_row = await evaluate_execution_recovery_learning(
        trace_id=normalized_trace,
        execution_id=int(getattr(execution, "id", 0) or 0) or None,
        managed_scope=scope,
        capability_name=capability_name,
        recovery_decision=str(recommended_attempt_decision or recovery_decision or "").strip(),
        environment_shift_detected=inferred_environment_shift,
        db=db,
    )
    recovery_learning = (
        to_execution_recovery_learning_out(recovery_learning_row)
        if recovery_learning_row is not None
        else {}
    )
    conflict_resolution = await _resolve_execution_recovery_conflict(
        db=db,
        actor="system",
        source="execution_recovery_service",
        managed_scope=scope,
        trace_id=normalized_trace,
        execution_id=int(getattr(execution, "id", 0) or 0) or None,
        capability_name=str(getattr(execution, "capability_name", "") or getattr(trace, "capability_name", "") or "execution").strip(),
        requested_executor=str(getattr(execution, "requested_executor", "") or "tod").strip() or "tod",
        execution_status=execution_status,
        retry_pressure=retry_pressure,
        mitigation_state=mitigation_state,
        checkpoint_json=checkpoint,
        base_decision=recovery_decision,
        base_reason=recovery_reason,
        base_recommended_attempt_decision=recommended_attempt_decision,
        base_recovery_allowed=recovery_allowed,
        base_operator_action_required=operator_action_required,
        latest_outcome=latest_outcome,
        recovery_learning=recovery_learning,
    )
    conflict_effects = _safe_dict(conflict_resolution.get("policy_effects_json", {}))
    if "recovery_decision" in conflict_effects:
        recovery_decision = str(conflict_effects.get("recovery_decision") or recovery_decision).strip()
    if "recommended_attempt_decision" in conflict_effects:
        recommended_attempt_decision = str(
            conflict_effects.get("recommended_attempt_decision") or recommended_attempt_decision
        ).strip()
    if "recovery_reason" in conflict_effects:
        recovery_reason = str(conflict_effects.get("recovery_reason") or recovery_reason).strip()
    if "recovery_allowed" in conflict_effects:
        recovery_allowed = bool(conflict_effects.get("recovery_allowed", False))
    if "operator_action_required" in conflict_effects:
        operator_action_required = bool(conflict_effects.get("operator_action_required", False))

    return {
        "trace_id": normalized_trace,
        "execution_id": int(getattr(execution, "id", 0) or 0) or None,
        "managed_scope": scope,
        "execution_status": execution_status,
        "dispatch_decision": dispatch_decision,
        "recovery_decision": recovery_decision,
        "recommended_attempt_decision": recommended_attempt_decision,
        "recovery_reason": recovery_reason,
        "operator_action_required": operator_action_required,
        "recovery_allowed": recovery_allowed,
        "resume_step_key": current_step,
        "attempt_number": len(attempts) + 1,
        "retry_pressure": retry_pressure,
        "mitigation_state": mitigation_state,
        "active_override_types": override_types,
        "latest_attempt": (
            to_execution_recovery_attempt_out(latest_attempt) if latest_attempt is not None else {}
        ),
        "latest_outcome": (
            to_execution_recovery_outcome_out(latest_outcome) if latest_outcome is not None else {}
        ),
        "recovery_learning": recovery_learning,
        "why_recovery_escalated_before_retry": str(
            recovery_learning.get("why_recovery_escalated_before_retry") or ""
        ).strip(),
        "conflict_resolution": conflict_resolution,
        "summary": _recovery_summary(
            {
                "recovery_decision": recovery_decision,
                "recovery_reason": recovery_reason,
            }
        ),
        "checkpoint_json": checkpoint,
    }


async def evaluate_execution_recovery_outcome(
    *,
    trace_id: str,
    execution_id: int | None,
    managed_scope: str,
    actor: str,
    source: str,
    metadata_json: dict | None,
    db: AsyncSession,
) -> ExecutionRecoveryOutcome | None:
    normalized_trace = str(trace_id or "").strip()
    execution = await _latest_execution(trace_id=normalized_trace, execution_id=execution_id, db=db)
    if execution is None:
        return None
    normalized_trace = str(normalized_trace or execution.trace_id or "").strip()
    if not normalized_trace:
        return None

    attempts = await list_execution_recovery_attempts(trace_id=normalized_trace, db=db, limit=20)
    accepted_attempt = next((row for row in attempts if str(row.status or "").strip() == "accepted"), None)
    if accepted_attempt is None:
        return None

    execution_status = str(execution.status or "").strip()
    if execution_status not in {"succeeded", "failed", "blocked", "pending_confirmation"}:
        return None

    existing = (
        (
            await db.execute(
                select(ExecutionRecoveryOutcome)
                .where(ExecutionRecoveryOutcome.attempt_id == int(accepted_attempt.id))
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = ExecutionRecoveryOutcome(
            attempt_id=int(accepted_attempt.id),
            trace_id=normalized_trace,
            execution_id=int(execution.id),
            source=str(source or RECOVERY_SOURCE).strip() or RECOVERY_SOURCE,
            actor=str(actor or "system").strip() or "system",
            managed_scope=str(managed_scope or execution.managed_scope or "global").strip() or "global",
        )
        db.add(existing)

    outcome_status = "operator_required"
    outcome_reason = "Recovery remains blocked pending operator action."
    learning_bias_json: dict = {
        "prefer_decision": "",
        "avoid_decision": "",
        "latest_recovery_decision": str(accepted_attempt.recovery_decision or "").strip(),
    }
    outcome_score = 0.25
    if execution_status == "succeeded":
        outcome_status = "recovered"
        outcome_reason = "The execution succeeded after the accepted recovery attempt."
        learning_bias_json["prefer_decision"] = str(accepted_attempt.recovery_decision or "").strip()
        outcome_score = 1.0
    elif execution_status == "failed":
        outcome_status = "failed_again"
        outcome_reason = "The execution failed again after the accepted recovery attempt."
        learning_bias_json["avoid_decision"] = str(accepted_attempt.recovery_decision or "").strip()
        outcome_score = 0.95
    elif execution_status in {"blocked", "pending_confirmation"}:
        outcome_status = "operator_required"
        outcome_reason = "The recovery path still requires explicit operator intervention."
        learning_bias_json["avoid_decision"] = str(accepted_attempt.recovery_decision or "").strip()
        outcome_score = 0.7

    existing.source = str(source or RECOVERY_SOURCE).strip() or RECOVERY_SOURCE
    existing.actor = str(actor or "system").strip() or "system"
    existing.trace_id = normalized_trace
    existing.execution_id = int(execution.id)
    existing.managed_scope = str(managed_scope or execution.managed_scope or "global").strip() or "global"
    existing.outcome_status = outcome_status
    existing.outcome_reason = outcome_reason
    existing.learning_bias_json = learning_bias_json
    existing.outcome_score = float(outcome_score)
    existing.result_json = _json_safe({
        "execution_status": execution_status,
        "execution_reason": str(execution.reason or "").strip(),
        "attempt": to_execution_recovery_attempt_out(accepted_attempt),
    })
    existing.metadata_json = _safe_dict(_json_safe(metadata_json if isinstance(metadata_json, dict) else {}))
    await db.flush()
    return existing


async def sync_execution_recovery_state(
    *,
    trace_id: str,
    execution_id: int | None,
    managed_scope: str,
    actor: str,
    source: str,
    metadata_json: dict | None,
    db: AsyncSession,
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=trace_id,
        execution_id=execution_id,
        managed_scope=managed_scope,
        db=db,
    )
    if evaluation is None:
        return {}

    normalized_trace = str(evaluation.get("trace_id") or trace_id or "").strip()
    outcome = await evaluate_execution_recovery_outcome(
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        managed_scope=str(evaluation.get("managed_scope") or managed_scope or "global"),
        actor=actor,
        source=source,
        metadata_json=metadata_json,
        db=db,
    )
    if outcome is not None:
        evaluation["latest_outcome"] = to_execution_recovery_outcome_out(outcome)

    execution = await _latest_execution(
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        db=db,
    )

    target_recovery_decision = str(
        evaluation.get("recommended_attempt_decision") or evaluation.get("recovery_decision") or ""
    ).strip()
    recovery_learning_row = await evaluate_execution_recovery_learning(
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        managed_scope=str(evaluation.get("managed_scope") or managed_scope or "global"),
        capability_name=str(getattr(execution, "capability_name", "") or "execution").strip() or "execution",
        recovery_decision=target_recovery_decision,
        environment_shift_detected=False,
        db=db,
    )
    if recovery_learning_row is not None:
        evaluation["recovery_learning"] = to_execution_recovery_learning_out(recovery_learning_row)
        evaluation["why_recovery_escalated_before_retry"] = str(
            evaluation["recovery_learning"].get("why_recovery_escalated_before_retry") or ""
        ).strip()

    trace = await get_execution_trace(trace_id=normalized_trace, db=db) if normalized_trace else None
    orchestration = await _latest_orchestration(trace_id=normalized_trace, db=db)
    stability = await _latest_stability(
        trace_id=normalized_trace,
        managed_scope=str(evaluation.get("managed_scope") or managed_scope or "global"),
        db=db,
    )
    recovery_state = {
        **evaluation,
        "summary": _recovery_summary(evaluation),
    }
    published = await publish_execution_recovery_state(
        db=db,
        actor=actor,
        source=source,
        recovery_state=recovery_state,
        metadata_json={
            "trace_id": normalized_trace,
            "execution_id": evaluation.get("execution_id"),
            **_safe_dict(metadata_json if isinstance(metadata_json, dict) else {}),
        },
    )
    safe_recovery_state = _json_safe(recovery_state)
    safe_published = _json_safe(published)
    safe_latest_outcome = _json_safe(recovery_state.get("latest_outcome", {}))
    safe_recovery_learning = _json_safe(recovery_state.get("recovery_learning", {}))

    if execution is not None:
        execution.feedback_json = {
            **_safe_dict(execution.feedback_json),
            "recovery_evaluation": safe_recovery_state,
            "recovery_state_bus": safe_published,
            "latest_recovery_outcome": safe_latest_outcome,
            "recovery_learning": safe_recovery_learning,
        }
    if orchestration is not None:
        orchestration.metadata_json = {
            **_safe_dict(orchestration.metadata_json),
            "recovery_evaluation": safe_recovery_state,
            "recovery_state_bus": safe_published,
            "recovery_learning": safe_recovery_learning,
        }
    if stability is not None:
        stability.metadata_json = {
            **_safe_dict(stability.metadata_json),
            "recovery_learning": safe_recovery_learning,
        }
    if trace is not None:
        trace.metadata_json = {
            **_safe_dict(trace.metadata_json),
            "recovery_evaluation": safe_recovery_state,
            "recovery_state_bus": safe_published,
            "latest_recovery_outcome": safe_latest_outcome,
            "recovery_learning": safe_recovery_learning,
        }
    await db.flush()
    return {
        "recovery": recovery_state,
        "state_bus": published,
    }


async def record_execution_recovery_attempt(
    *,
    trace_id: str,
    execution_id: int | None,
    managed_scope: str,
    requested_decision: str,
    actor: str,
    source: str,
    reason: str,
    operator_ack: bool,
    metadata_json: dict | None,
    db: AsyncSession,
) -> ExecutionRecoveryAttempt | None:
    evaluation = await evaluate_execution_recovery(
        trace_id=trace_id,
        execution_id=execution_id,
        managed_scope=managed_scope,
        db=db,
    )
    if evaluation is None:
        return None

    normalized_trace = str(evaluation.get("trace_id") or "").strip()
    selected_decision = str(requested_decision or evaluation.get("recommended_attempt_decision") or evaluation.get("recovery_decision") or "").strip()
    evaluation_decision = str(evaluation.get("recovery_decision") or "").strip()
    status = "blocked_by_policy"
    final_reason = str(reason or evaluation.get("recovery_reason") or "").strip() or "execution recovery attempt recorded"
    accepted = False

    if evaluation_decision in {"hard_stop_persisted", "rollback_and_replan", "no_recovery_available"}:
        accepted = False
    elif evaluation_decision == "require_operator_resume":
        if operator_ack and selected_decision in {"resume_from_checkpoint", "retry_current_step", "restart_execution"}:
            accepted = True
            status = "accepted"
            if not reason.strip():
                final_reason = "Operator acknowledged the recovery and approved a safe resume path."
        else:
            final_reason = final_reason or "Operator acknowledgement is required before recovery can proceed."
    elif selected_decision == evaluation_decision and bool(evaluation.get("recovery_allowed", False)):
        accepted = True
        status = "accepted"

    row = ExecutionRecoveryAttempt(
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        source=str(source or "execution_control").strip() or "execution_control",
        actor=str(actor or "system").strip() or "system",
        managed_scope=str(evaluation.get("managed_scope") or managed_scope or "global").strip() or "global",
        recovery_decision=selected_decision or evaluation_decision,
        recovery_reason=final_reason,
        attempt_number=_safe_int(evaluation.get("attempt_number"), 1),
        resume_step_key=str(evaluation.get("resume_step_key") or "").strip(),
        status=status,
        result_json={
            "accepted": accepted,
            "evaluation": _json_safe(evaluation),
            "operator_ack": bool(operator_ack),
            "requested_decision": selected_decision,
        },
        metadata_json=_safe_dict(_json_safe(metadata_json if isinstance(metadata_json, dict) else {})),
    )
    db.add(row)
    await db.flush()

    trace = await get_execution_trace(trace_id=normalized_trace, db=db) if normalized_trace else None
    execution = await _latest_execution(trace_id=normalized_trace, execution_id=evaluation.get("execution_id"), db=db)
    orchestration = await _latest_orchestration(trace_id=normalized_trace, db=db)
    intent = (
        (
            await db.execute(
                select(ExecutionIntent)
                .where(ExecutionIntent.trace_id == normalized_trace)
                .order_by(ExecutionIntent.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
        if normalized_trace
        else None
    )

    if accepted:
        if orchestration is not None:
            orchestration.orchestration_status = "recovery_pending"
            orchestration.current_step_key = "recovery"
            orchestration.checkpoint_json = {
                **_safe_dict(orchestration.checkpoint_json),
                "latest_recovery_attempt_id": int(row.id),
                "latest_recovery_decision": row.recovery_decision,
                "resume_step_key": row.resume_step_key,
            }
            if row.recovery_decision in {"retry_current_step", "restart_execution"}:
                orchestration.retry_count = int(orchestration.retry_count or 0) + 1
            orchestration.metadata_json = {
                **_safe_dict(orchestration.metadata_json),
                "latest_recovery_attempt_id": int(row.id),
                "latest_recovery_decision": row.recovery_decision,
                "operator_mediated": bool(operator_ack),
            }
        if intent is not None:
            intent.resumption_count = int(intent.resumption_count or 0) + 1
        if execution is not None:
            execution.status = "accepted"
            execution.dispatch_decision = "accepted"
            execution.reason = final_reason
            execution.feedback_json = {
                **_safe_dict(execution.feedback_json),
                "latest_recovery_attempt_id": int(row.id),
                "latest_recovery_decision": row.recovery_decision,
                "recovery_state": "recovery_pending",
                "recovery_operator_ack": bool(operator_ack),
            }
        if trace is not None:
            trace.current_stage = "recovery"
            trace.lifecycle_status = "active"
            trace.causality_graph_json = {
                **_safe_dict(trace.causality_graph_json),
                "latest_recovery_attempt_id": int(row.id),
                "latest_recovery_decision": row.recovery_decision,
            }
            trace.metadata_json = {
                **_safe_dict(trace.metadata_json),
                "latest_recovery_attempt_id": int(row.id),
                "latest_recovery_decision": row.recovery_decision,
                "recovery_state": "recovery_pending",
            }

    await append_execution_trace_event(
        db=db,
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        intent_id=int(intent.id) if intent is not None else None,
        event_type="recovery_attempted" if accepted else "recovery_blocked",
        event_stage="recovery",
        causality_role="effect",
        summary=(
            f"Recovery attempt {row.id} recorded with decision {row.recovery_decision}"
            if accepted
            else f"Recovery attempt {row.id} blocked by policy"
        ),
        payload_json={
            "recovery_attempt_id": int(row.id),
            "recovery_decision": row.recovery_decision,
            "status": row.status,
            "operator_ack": bool(operator_ack),
            "evaluation": _json_safe(evaluation),
        },
    )
    await sync_execution_recovery_state(
        trace_id=normalized_trace,
        execution_id=evaluation.get("execution_id"),
        managed_scope=str(evaluation.get("managed_scope") or managed_scope or "global"),
        actor=actor,
        source=source,
        metadata_json={
            "recovery_attempt_id": int(row.id),
            "operator_ack": bool(operator_ack),
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
    )
    await db.flush()
    return row