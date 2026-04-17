from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_service import execution_truth_scope_matches
from core.models import (
    CapabilityExecution,
    ExecutionRecoveryAttempt,
    ExecutionRecoveryOutcome,
    WorkspaceInquiryQuestion,
    WorkspaceMaintenanceRun,
    WorkspaceOperatorResolutionCommitment,
    WorkspaceOperatorResolutionCommitmentMonitoringProfile,
    WorkspaceOperatorResolutionCommitmentOutcomeProfile,
    WorkspaceStewardshipCycle,
)
from core.operator_resolution_service import (
    commitment_is_recovery_policy_tuning_derived,
    normalize_scope,
    sync_commitment_expiration,
)


TERMINAL_COMMITMENT_STATUSES = {
    "satisfied",
    "abandoned",
    "ineffective",
    "harmful",
    "superseded",
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _json_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _commitment_ref_matches(payload: dict, *, commitment_id: int) -> bool:
    commitment = payload.get("operator_resolution_commitment", {})
    if not isinstance(commitment, dict):
        return False
    return _safe_int(commitment.get("commitment_id", 0)) == int(commitment_id)


def _scope_matches(*, managed_scope: str, payload: dict) -> bool:
    scope = normalize_scope(managed_scope)
    if scope == "global":
        return True
    for key in ("managed_scope", "target_scope", "scope", "zone", "scan_area"):
        value = str(payload.get(key, "") or "").strip()
        if value == scope:
            return True
    return False


def _stewardship_cycle_matches(
    row: WorkspaceStewardshipCycle,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    metadata = _json_dict(row.metadata_json)
    decision = _json_dict(row.decision_json)
    verification = _json_dict(metadata.get("verification", {}))
    integration = _json_dict(row.integration_evidence_json)
    for bucket in (metadata, decision, verification, integration):
        if _commitment_ref_matches(bucket, commitment_id=commitment_id):
            return True
        if _scope_matches(managed_scope=managed_scope, payload=bucket):
            return True
    return False


def _maintenance_run_matches(
    row: WorkspaceMaintenanceRun,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    metadata = _json_dict(row.metadata_json)
    outcomes = _json_dict(row.maintenance_outcomes_json)
    for bucket in (metadata, outcomes):
        if _commitment_ref_matches(bucket, commitment_id=commitment_id):
            return True
        if _scope_matches(managed_scope=managed_scope, payload=bucket):
            return True
    return False


def _inquiry_question_matches(
    row: WorkspaceInquiryQuestion,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    trigger_evidence = _json_dict(row.trigger_evidence_json)
    metadata = _json_dict(row.metadata_json)
    if _safe_int(trigger_evidence.get("commitment_id", 0)) == int(commitment_id):
        return True
    if _safe_int(trigger_evidence.get("monitoring_commitment_id", 0)) == int(commitment_id):
        return True
    for bucket in (trigger_evidence, metadata):
        if _scope_matches(managed_scope=managed_scope, payload=bucket):
            return True
    return False


def _recovery_context(commitment: WorkspaceOperatorResolutionCommitment) -> dict:
    recommendation = (
        commitment.recommendation_snapshot_json
        if isinstance(commitment.recommendation_snapshot_json, dict)
        else {}
    )
    provenance = commitment.provenance_json if isinstance(commitment.provenance_json, dict) else {}
    return {
        "trace_id": str(recommendation.get("trace_id") or provenance.get("trace_id") or "").strip(),
        "execution_id": _safe_int(
            recommendation.get("execution_id") or provenance.get("execution_id") or 0
        ),
    }


def _recovery_attempt_matches(
    row: ExecutionRecoveryAttempt,
    *,
    managed_scope: str,
    trace_id: str,
    execution_id: int,
) -> bool:
    if normalize_scope(row.managed_scope) != normalize_scope(managed_scope):
        return False
    if trace_id and str(row.trace_id or "").strip() == trace_id:
        return True
    if execution_id > 0 and int(row.execution_id or 0) == execution_id:
        return True
    return True


def _recovery_outcome_matches(
    row: ExecutionRecoveryOutcome,
    *,
    managed_scope: str,
    trace_id: str,
    execution_id: int,
) -> bool:
    if normalize_scope(row.managed_scope) != normalize_scope(managed_scope):
        return False
    if trace_id and str(row.trace_id or "").strip() == trace_id:
        return True
    if execution_id > 0 and int(row.execution_id or 0) == execution_id:
        return True
    return True


def _retry_count_for_execution(row: CapabilityExecution) -> int:
    feedback = _json_dict(row.feedback_json)
    truth = _json_dict(row.execution_truth_json)
    return max(
        _safe_int(truth.get("retry_count", 0)),
        _safe_int(feedback.get("operator_retry_count", 0)),
        _safe_int(feedback.get("autonomy_retry_count", 0)),
    )


def _effective_learning_bias(outcome: WorkspaceOperatorResolutionCommitmentOutcomeProfile) -> str:
    metadata = _json_dict(outcome.metadata_json)
    learning_bias = _json_dict(metadata.get("operator_learning_bias", {}))
    value = str(learning_bias.get("bias", "") or "").strip()
    if value:
        return value
    return str(_json_dict(outcome.learning_signals_json).get("repeat_commitment_bias", "") or "").strip()


def _pattern_summary(
    *,
    prior_rows: list[WorkspaceOperatorResolutionCommitmentOutcomeProfile],
    commitment: WorkspaceOperatorResolutionCommitment,
) -> dict:
    same_decision = [
        row for row in prior_rows if str(row.decision_type or "").strip() == str(commitment.decision_type or "").strip()
    ]
    repeated_successes = sum(1 for row in same_decision if str(row.outcome_status or "").strip() == "satisfied")
    repeated_ineffective = sum(1 for row in same_decision if str(row.outcome_status or "").strip() == "ineffective")
    repeated_harmful = sum(1 for row in same_decision if str(row.outcome_status or "").strip() == "harmful")
    distinct_statuses = {
        str(row.outcome_status or "").strip()
        for row in same_decision
        if str(row.outcome_status or "").strip()
    }
    conflicting = len(distinct_statuses & {"satisfied", "ineffective", "harmful"}) >= 2
    return {
        "same_decision_type_count": len(same_decision),
        "repeated_successful_commitments": repeated_successes,
        "repeated_ineffective_commitments": repeated_ineffective,
        "repeated_harmful_commitments": repeated_harmful,
        "conflicting_commitments": conflicting,
        "recent_outcomes": [
            {
                "outcome_id": int(row.id),
                "outcome_status": str(row.outcome_status or "").strip(),
                "decision_type": str(row.decision_type or "").strip(),
                "created_at": row.created_at.isoformat(),
                "learning_bias": _effective_learning_bias(row),
            }
            for row in prior_rows[:5]
        ],
    }


def _derive_learning_signals(
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
    outcome_status: str,
    pattern_summary: dict,
    stability_score: float,
    retry_pressure_score: float,
) -> dict:
    decision_type = str(commitment.decision_type or "").strip()
    repeat_bias = "neutral"
    inquiry_bias = "monitor_commitment_pattern"
    strategy_priority_delta = 0.0
    backlog_priority_delta = 0.0
    autonomy_level_cap = ""

    if outcome_status == "satisfied":
        repeat_bias = "repeat"
        inquiry_bias = "similar_commitment_can_repeat_with_monitoring"
        if decision_type in {"require_additional_evidence", "defer_action"}:
            strategy_priority_delta = 0.08
        backlog_priority_delta = -0.04
    elif outcome_status == "ineffective":
        repeat_bias = "avoid"
        inquiry_bias = "ask_before_similar_commitment"
        strategy_priority_delta = -0.08
        backlog_priority_delta = 0.12
        autonomy_level_cap = "supervised"
    elif outcome_status == "harmful":
        repeat_bias = "avoid"
        inquiry_bias = "ask_before_similar_commitment"
        strategy_priority_delta = -0.12
        backlog_priority_delta = 0.18
        autonomy_level_cap = "operator_required"
    elif outcome_status == "abandoned":
        repeat_bias = "cautious"
        inquiry_bias = "ask_before_similar_commitment"
        strategy_priority_delta = -0.04
        backlog_priority_delta = 0.08
        autonomy_level_cap = "supervised"

    if bool(pattern_summary.get("conflicting_commitments", False)):
        inquiry_bias = "ask_before_similar_commitment"
        backlog_priority_delta = max(backlog_priority_delta, 0.1)

    return {
        "repeat_commitment_bias": repeat_bias,
        "inquiry_bias": inquiry_bias,
        "strategy_priority_delta": round(strategy_priority_delta, 6),
        "backlog_priority_delta": round(backlog_priority_delta, 6),
        "autonomy_level_cap": autonomy_level_cap,
        "decision_type": decision_type,
        "commitment_family": str(commitment.commitment_family or "").strip(),
        "monitoring_stability": round(stability_score, 6),
        "retry_pressure_score": round(retry_pressure_score, 6),
    }


def _recommended_actions(
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
    outcome_status: str,
    learning_signals: dict,
) -> list[dict]:
    commitment_id = int(commitment.id)
    actions: list[dict] = []
    if outcome_status in {"ineffective", "harmful", "abandoned"}:
        actions.append(
            {
                "action": "review_similar_commitments",
                "label": "Review whether similar commitments should be avoided in this scope",
                "effect_type": "no_action",
                "params": {"commitment_id": commitment_id},
            }
        )
    if str(learning_signals.get("inquiry_bias", "") or "").strip() == "ask_before_similar_commitment":
        actions.append(
            {
                "action": "prompt_learning_inquiry",
                "label": "Ask whether similar commitments should be avoided next time",
                "effect_type": "no_action",
                "params": {"commitment_id": commitment_id},
            }
        )
    return actions


def to_operator_resolution_commitment_outcome_out(
    row: WorkspaceOperatorResolutionCommitmentOutcomeProfile,
) -> dict:
    return {
        "outcome_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "commitment_id": int(row.commitment_id),
        "managed_scope": row.managed_scope,
        "commitment_family": row.commitment_family,
        "decision_type": row.decision_type,
        "status": row.status,
        "commitment_status": row.commitment_status,
        "outcome_status": row.outcome_status,
        "outcome_reason": row.outcome_reason,
        "evaluation_window_hours": int(row.evaluation_window_hours or 0),
        "evidence_count": int(row.evidence_count or 0),
        "monitoring_profile_count": int(row.monitoring_profile_count or 0),
        "stewardship_cycle_count": int(row.stewardship_cycle_count or 0),
        "maintenance_run_count": int(row.maintenance_run_count or 0),
        "inquiry_question_count": int(row.inquiry_question_count or 0),
        "execution_count": int(row.execution_count or 0),
        "retry_count": int(row.retry_count or 0),
        "blocked_auto_execution_count": int(row.blocked_auto_execution_count or 0),
        "allowed_auto_execution_count": int(row.allowed_auto_execution_count or 0),
        "potential_violation_count": int(row.potential_violation_count or 0),
        "governance_conflict_count": int(row.governance_conflict_count or 0),
        "effectiveness_score": round(float(row.effectiveness_score or 0.0), 6),
        "stability_score": round(float(row.stability_score or 0.0), 6),
        "retry_pressure_score": round(float(row.retry_pressure_score or 0.0), 6),
        "learning_confidence": round(float(row.learning_confidence or 0.0), 6),
        "learning_signals": _json_dict(row.learning_signals_json),
        "pattern_summary": _json_dict(row.pattern_summary_json),
        "recommended_actions": row.recommended_actions_json if isinstance(row.recommended_actions_json, list) else [],
        "reasoning": _json_dict(row.reasoning_json),
        "metadata_json": _json_dict(row.metadata_json),
        "created_at": row.created_at,
    }


async def latest_commitment_outcome_profile(
    *,
    commitment_id: int,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentOutcomeProfile | None:
    return (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentOutcomeProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentOutcomeProfile.commitment_id
                    == int(commitment_id)
                )
                .order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def list_commitment_outcome_profiles(
    *,
    commitment_id: int,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceOperatorResolutionCommitmentOutcomeProfile]:
    return (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentOutcomeProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentOutcomeProfile.commitment_id
                    == int(commitment_id)
                )
                .order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )


async def get_commitment_outcome_profile(
    *,
    outcome_id: int,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentOutcomeProfile | None:
    return await db.get(WorkspaceOperatorResolutionCommitmentOutcomeProfile, int(outcome_id))


async def latest_scope_commitment_outcome_profile(
    *,
    managed_scope: str,
    db: AsyncSession,
    decision_types: set[str] | None = None,
    commitment_families: set[str] | None = None,
    limit: int = 20,
) -> WorkspaceOperatorResolutionCommitmentOutcomeProfile | None:
    rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentOutcomeProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentOutcomeProfile.managed_scope
                    == normalize_scope(managed_scope)
                )
                .order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )
    decision_filter = {str(item or "").strip() for item in (decision_types or set()) if str(item or "").strip()}
    family_filter = {str(item or "").strip() for item in (commitment_families or set()) if str(item or "").strip()}
    for row in rows:
        if decision_filter and str(row.decision_type or "").strip() not in decision_filter:
            continue
        if family_filter and str(row.commitment_family or "").strip() not in family_filter:
            continue
        return row
    return None


async def evaluate_operator_resolution_commitment_outcome(
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
    actor: str,
    source: str,
    lookback_hours: int,
    target_status: str,
    outcome_reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentOutcomeProfile:
    now = datetime.now(timezone.utc)
    sync_commitment_expiration(commitment, now=now)
    since = now - timedelta(hours=max(1, int(lookback_hours)))
    managed_scope = normalize_scope(commitment.managed_scope)

    monitoring_rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentMonitoringProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentMonitoringProfile.commitment_id
                    == int(commitment.id)
                )
                .where(WorkspaceOperatorResolutionCommitmentMonitoringProfile.created_at >= since)
                .order_by(WorkspaceOperatorResolutionCommitmentMonitoringProfile.id.asc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    stewardship_rows = (
        (
            await db.execute(
                select(WorkspaceStewardshipCycle)
                .where(WorkspaceStewardshipCycle.created_at >= since)
                .order_by(WorkspaceStewardshipCycle.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    stewardship_rows = [
        row
        for row in stewardship_rows
        if _stewardship_cycle_matches(row, commitment_id=int(commitment.id), managed_scope=managed_scope)
    ]

    maintenance_rows = (
        (
            await db.execute(
                select(WorkspaceMaintenanceRun)
                .where(WorkspaceMaintenanceRun.created_at >= since)
                .order_by(WorkspaceMaintenanceRun.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    maintenance_rows = [
        row
        for row in maintenance_rows
        if _maintenance_run_matches(row, commitment_id=int(commitment.id), managed_scope=managed_scope)
    ]

    inquiry_rows = (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.created_at >= since)
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    inquiry_rows = [
        row
        for row in inquiry_rows
        if _inquiry_question_matches(row, commitment_id=int(commitment.id), managed_scope=managed_scope)
    ]

    execution_rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.created_at >= since)
                .order_by(CapabilityExecution.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    execution_rows = [
        row
        for row in execution_rows
        if execution_truth_scope_matches(row=row, managed_scope=managed_scope)
    ]

    recovery_attempt_rows: list[ExecutionRecoveryAttempt] = []
    recovery_outcome_rows: list[ExecutionRecoveryOutcome] = []
    if commitment_is_recovery_policy_tuning_derived(commitment):
        recovery_context = _recovery_context(commitment)
        recovery_attempt_rows = (
            (
                await db.execute(
                    select(ExecutionRecoveryAttempt)
                    .where(ExecutionRecoveryAttempt.created_at >= since)
                    .order_by(ExecutionRecoveryAttempt.id.desc())
                    .limit(300)
                )
            )
            .scalars()
            .all()
        )
        recovery_attempt_rows = [
            row
            for row in recovery_attempt_rows
            if _recovery_attempt_matches(
                row,
                managed_scope=managed_scope,
                trace_id=str(recovery_context.get("trace_id") or ""),
                execution_id=_safe_int(recovery_context.get("execution_id", 0)),
            )
        ]
        recovery_outcome_rows = (
            (
                await db.execute(
                    select(ExecutionRecoveryOutcome)
                    .where(ExecutionRecoveryOutcome.created_at >= since)
                    .order_by(ExecutionRecoveryOutcome.id.desc())
                    .limit(300)
                )
            )
            .scalars()
            .all()
        )
        recovery_outcome_rows = [
            row
            for row in recovery_outcome_rows
            if _recovery_outcome_matches(
                row,
                managed_scope=managed_scope,
                trace_id=str(recovery_context.get("trace_id") or ""),
                execution_id=_safe_int(recovery_context.get("execution_id", 0)),
            )
        ]

    prior_outcomes = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentOutcomeProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentOutcomeProfile.managed_scope
                    == managed_scope
                )
                .order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.id.desc())
                .limit(25)
            )
        )
        .scalars()
        .all()
    )

    monitoring_count = len(monitoring_rows)
    blocked_count = sum(int(row.blocked_auto_execution_count or 0) for row in monitoring_rows)
    allowed_count = sum(int(row.allowed_auto_execution_count or 0) for row in monitoring_rows)
    violation_count = sum(int(row.potential_violation_count or 0) for row in monitoring_rows)
    conflict_count = sum(
        1
        for row in monitoring_rows
        if str(row.governance_state or "").strip() in {"drifting", "violating", "expired"}
        or str(row.governance_decision or "").strip() in {"operator_review_required", "replace_commitment"}
    )
    avg_health = (
        sum(float(row.health_score or 0.0) for row in monitoring_rows) / monitoring_count
        if monitoring_count
        else 0.0
    )
    avg_compliance = (
        sum(float(row.compliance_score or 0.0) for row in monitoring_rows) / monitoring_count
        if monitoring_count
        else 0.0
    )
    avg_drift = (
        sum(float(row.drift_score or 0.0) for row in monitoring_rows) / monitoring_count
        if monitoring_count
        else 1.0
    )
    first_monitoring = monitoring_rows[0] if monitoring_rows else None
    last_monitoring = monitoring_rows[-1] if monitoring_rows else None
    health_delta = (
        float(last_monitoring.health_score or 0.0) - float(first_monitoring.health_score or 0.0)
        if first_monitoring is not None and last_monitoring is not None
        else 0.0
    )
    compliance_delta = (
        float(last_monitoring.compliance_score or 0.0) - float(first_monitoring.compliance_score or 0.0)
        if first_monitoring is not None and last_monitoring is not None
        else 0.0
    )
    drift_delta = (
        float(first_monitoring.drift_score or 0.0) - float(last_monitoring.drift_score or 0.0)
        if first_monitoring is not None and last_monitoring is not None
        else 0.0
    )
    retry_count = sum(_retry_count_for_execution(row) for row in execution_rows)
    execution_count = len(execution_rows)
    recovery_outcome_count = len(recovery_outcome_rows)
    recovery_recovered_count = sum(
        1
        for row in recovery_outcome_rows
        if str(row.outcome_status or "").strip() == "recovered"
    )
    recovery_failed_again_count = sum(
        1
        for row in recovery_outcome_rows
        if str(row.outcome_status or "").strip() == "failed_again"
    )
    recovery_operator_required_count = sum(
        1
        for row in recovery_outcome_rows
        if str(row.outcome_status or "").strip() == "operator_required"
    )
    retry_pressure_score = _bounded(
        float(retry_count) / float(max(1, execution_count * 2)),
    )
    blocked_ratio = float(blocked_count) / float(max(1, blocked_count + allowed_count))
    trend_score = _bounded(0.5 + ((health_delta + compliance_delta + drift_delta) / 3.0))
    effectiveness_score = _bounded(
        (avg_compliance * 0.35)
        + (avg_health * 0.25)
        + (trend_score * 0.15)
        + ((1.0 - blocked_ratio) * 0.15)
        + ((1.0 - retry_pressure_score) * 0.10)
    )
    stability_score = _bounded(
        (avg_health * 0.35)
        + (avg_compliance * 0.25)
        + ((1.0 - avg_drift) * 0.20)
        + ((1.0 - retry_pressure_score) * 0.20)
    )
    if recovery_outcome_count > 0:
        recovery_success_rate = float(recovery_recovered_count) / float(recovery_outcome_count)
        recovery_escalation_rate = float(
            recovery_failed_again_count + recovery_operator_required_count
        ) / float(recovery_outcome_count)
        effectiveness_score = _bounded(
            effectiveness_score + (recovery_success_rate * 0.20) - (recovery_escalation_rate * 0.20)
        )
        stability_score = _bounded(
            stability_score + (recovery_success_rate * 0.15) - (recovery_escalation_rate * 0.15)
        )

    derived_status = str(target_status or "").strip().lower()
    commitment_status = str(commitment.status or "").strip().lower()
    if not derived_status:
        if commitment_status == "superseded":
            derived_status = "superseded"
        elif recovery_failed_again_count >= 2:
            derived_status = "harmful"
        elif recovery_operator_required_count >= 2 and recovery_recovered_count == 0:
            derived_status = "ineffective"
        elif recovery_recovered_count > 0 and recovery_failed_again_count == 0:
            derived_status = "satisfied"
        elif violation_count >= 2 and (avg_health <= 0.45 or conflict_count >= 1):
            derived_status = "harmful"
        elif commitment_status in {"revoked", "expired"}:
            derived_status = "abandoned"
        elif (
            effectiveness_score < 0.45
            or retry_pressure_score >= 0.45
            or blocked_count >= max(2, allowed_count + 1)
            or conflict_count >= 2
        ):
            derived_status = "ineffective"
        elif stability_score >= 0.70 and effectiveness_score >= 0.70 and retry_pressure_score <= 0.25:
            derived_status = "satisfied"
        else:
            derived_status = "ineffective" if blocked_count > 0 else "satisfied"

    pattern_summary = _pattern_summary(prior_rows=prior_outcomes, commitment=commitment)
    learning_signals = _derive_learning_signals(
        commitment=commitment,
        outcome_status=derived_status,
        pattern_summary=pattern_summary,
        stability_score=stability_score,
        retry_pressure_score=retry_pressure_score,
    )
    if str(outcome_reason or "").strip():
        final_reason = str(outcome_reason).strip()
    elif derived_status == "harmful":
        final_reason = "Commitment repeatedly conflicted with runtime evidence and raised violation risk."
    elif derived_status == "ineffective":
        final_reason = "Commitment created repeated execution friction without improving scope stability."
    elif derived_status == "abandoned":
        final_reason = "Commitment ended before reaching a stable beneficial outcome."
    elif derived_status == "superseded":
        final_reason = "Commitment was replaced by a newer commitment for the same scope."
    else:
        final_reason = "Commitment improved scope stability without sustained runtime conflict."
    if commitment_is_recovery_policy_tuning_derived(commitment) and recovery_outcome_count > 0:
        final_reason = (
            f"Recovery evidence recorded {recovery_recovered_count} recovered, "
            f"{recovery_failed_again_count} failed-again, and {recovery_operator_required_count} "
            f"operator-required outcomes while this commitment was active."
        )

    recommended_actions = _recommended_actions(
        commitment=commitment,
        outcome_status=derived_status,
        learning_signals=learning_signals,
    )
    learning_confidence = _bounded(
        min(1.0, (0.25 * monitoring_count) + (0.15 * len(stewardship_rows)) + (0.1 * len(maintenance_rows)) + (0.1 * execution_count)),
    )
    evidence_count = (
        monitoring_count
        + len(stewardship_rows)
        + len(maintenance_rows)
        + len(inquiry_rows)
        + execution_count
        + len(recovery_attempt_rows)
        + recovery_outcome_count
    )

    should_update_commitment_status = (
        derived_status in TERMINAL_COMMITMENT_STATUSES
        and commitment_status != derived_status
    )
    if commitment_is_recovery_policy_tuning_derived(commitment) and derived_status == "satisfied":
        should_update_commitment_status = False

    if should_update_commitment_status:
        commitment.status = derived_status
        commitment.metadata_json = {
            **(_json_dict(commitment.metadata_json)),
            "objective87_last_outcome": {
                "outcome_status": derived_status,
                "outcome_reason": final_reason,
                "evaluated_at": now.isoformat(),
            },
        }

    row = WorkspaceOperatorResolutionCommitmentOutcomeProfile(
        source=source,
        actor=actor,
        commitment_id=int(commitment.id),
        managed_scope=managed_scope,
        commitment_family=str(commitment.commitment_family or "").strip() or "general",
        decision_type=str(commitment.decision_type or "").strip() or "approve_current_path",
        status="evaluated",
        commitment_status=str(commitment.status or "").strip(),
        outcome_status=derived_status,
        outcome_reason=final_reason,
        evaluation_window_hours=max(1, int(lookback_hours)),
        evidence_count=evidence_count,
        monitoring_profile_count=monitoring_count,
        stewardship_cycle_count=len(stewardship_rows),
        maintenance_run_count=len(maintenance_rows),
        inquiry_question_count=len(inquiry_rows),
        execution_count=execution_count,
        retry_count=retry_count,
        blocked_auto_execution_count=blocked_count,
        allowed_auto_execution_count=allowed_count,
        potential_violation_count=violation_count,
        governance_conflict_count=conflict_count,
        effectiveness_score=effectiveness_score,
        stability_score=stability_score,
        retry_pressure_score=retry_pressure_score,
        learning_confidence=learning_confidence,
        learning_signals_json=learning_signals,
        pattern_summary_json=pattern_summary,
        recommended_actions_json=recommended_actions,
        reasoning_json={
            "monitoring": {
                "avg_health": round(avg_health, 6),
                "avg_compliance": round(avg_compliance, 6),
                "avg_drift": round(avg_drift, 6),
                "health_delta": round(health_delta, 6),
                "compliance_delta": round(compliance_delta, 6),
                "drift_delta": round(drift_delta, 6),
            },
            "counts": {
                "monitoring_profiles": monitoring_count,
                "stewardship_cycles": len(stewardship_rows),
                "maintenance_runs": len(maintenance_rows),
                "inquiry_questions": len(inquiry_rows),
                "executions": execution_count,
                "recovery_attempts": len(recovery_attempt_rows),
                "recovery_outcomes": recovery_outcome_count,
                "recovery_recovered": recovery_recovered_count,
                "recovery_failed_again": recovery_failed_again_count,
                "recovery_operator_required": recovery_operator_required_count,
                "retries": retry_count,
                "blocked_auto_executions": blocked_count,
                "allowed_auto_executions": allowed_count,
                "potential_violations": violation_count,
                "governance_conflicts": conflict_count,
            },
            "recovery_commitment": commitment_is_recovery_policy_tuning_derived(commitment),
        },
        metadata_json={
            **(_json_dict(metadata_json)),
            "objective87_commitment_outcome": True,
            "evaluated_at": now.isoformat(),
            "commitment_status_before": commitment_status,
        },
    )
    db.add(row)
    await db.flush()
    return row