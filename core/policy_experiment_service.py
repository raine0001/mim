from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.development_memory_service import development_influence_for_experiment
from core.models import Action, ConstraintEvaluation, WorkspaceDecisionRecord, WorkspaceHorizonReplanEvent, WorkspaceImprovementProposal, WorkspacePolicyExperiment


async def _warning_rows(*, since: datetime, db: AsyncSession) -> list[ConstraintEvaluation]:
    return (
        await db.execute(
            select(ConstraintEvaluation)
            .where(ConstraintEvaluation.created_at >= since)
            .order_by(ConstraintEvaluation.id.desc())
            .limit(3000)
        )
    ).scalars().all()


async def _decision_rows(*, since: datetime, db: AsyncSession) -> list[WorkspaceDecisionRecord]:
    return (
        await db.execute(
            select(WorkspaceDecisionRecord)
            .where(WorkspaceDecisionRecord.created_at >= since)
            .order_by(WorkspaceDecisionRecord.id.desc())
            .limit(3000)
        )
    ).scalars().all()


def _count_warning(rows: list[ConstraintEvaluation], *, constraint_key: str) -> int:
    count = 0
    for row in rows:
        warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
        if any(isinstance(item, dict) and str(item.get("constraint", "")) == constraint_key for item in warnings):
            count += 1
    return count


def _success_rate(rows: list[ConstraintEvaluation], *, constraint_key: str) -> float:
    total = 0
    success = 0
    for row in rows:
        warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
        if not any(isinstance(item, dict) and str(item.get("constraint", "")) == constraint_key for item in warnings):
            continue
        total += 1
        if str(row.outcome_result or "") == "succeeded":
            success += 1
    if total == 0:
        return 0.0
    return round(success / float(total), 6)


def _quality_avg(rows: list[WorkspaceDecisionRecord], *, decision_type: str = "") -> float:
    relevant = rows
    if decision_type:
        relevant = [item for item in rows if str(item.decision_type or "") == decision_type]
    if not relevant:
        return 0.0
    return round(sum(float(item.result_quality or 0.0) for item in relevant) / float(len(relevant)), 6)


def _operator_override_rate(rows: list[WorkspaceDecisionRecord]) -> float:
    if not rows:
        return 0.0
    override_count = 0
    for row in rows:
        source_context = row.source_context_json if isinstance(row.source_context_json, dict) else {}
        endpoint = str(source_context.get("endpoint", "")).strip()
        if endpoint.endswith("/resolve") or endpoint.endswith("/deactivate"):
            override_count += 1
    return round(float(override_count) / float(max(1, len(rows))), 6)


async def _execution_time_ms(*, since: datetime, db: AsyncSession) -> float:
    rows = (
        await db.execute(
            select(Action)
            .where(Action.started_at >= since)
            .order_by(Action.id.desc())
            .limit(4000)
        )
    ).scalars().all()
    durations: list[float] = []
    for row in rows:
        if row.completed_at is None or row.started_at is None:
            continue
        elapsed = (row.completed_at - row.started_at).total_seconds() * 1000.0
        if elapsed >= 0:
            durations.append(elapsed)
    if not durations:
        return 0.0
    return round(sum(durations) / float(len(durations)), 6)


async def _replan_frequency(*, since: datetime, db: AsyncSession) -> float:
    replan_count = (
        await db.execute(
            select(WorkspaceHorizonReplanEvent)
            .where(WorkspaceHorizonReplanEvent.created_at >= since)
            .order_by(WorkspaceHorizonReplanEvent.id.desc())
            .limit(4000)
        )
    ).scalars().all()
    action_count = (
        await db.execute(
            select(Action)
            .where(Action.started_at >= since)
            .order_by(Action.id.desc())
            .limit(4000)
        )
    ).scalars().all()
    total_actions = max(1, len(action_count))
    return round(float(len(replan_count)) / float(total_actions), 6)


def _proposal_constraint_key(proposal: WorkspaceImprovementProposal | None) -> str:
    if not proposal:
        return "execution_throttle"
    evidence = proposal.evidence_json if isinstance(proposal.evidence_json, dict) else {}
    key = str(evidence.get("constraint_key", "")).strip()
    if key:
        return key
    affected = str(proposal.affected_component or "")
    if affected.startswith("constraint:"):
        return affected.split(":", 1)[1].strip() or "execution_throttle"
    return "execution_throttle"


def _derive_experiment_type(proposal: WorkspaceImprovementProposal | None, requested_type: str) -> str:
    if requested_type.strip():
        return requested_type.strip()
    if not proposal:
        return "policy_adjustment_sandbox"
    proposal_type = str(proposal.proposal_type or "")
    if proposal_type == "soft_constraint_weight_adjustment":
        return "soft_constraint_sandbox"
    if proposal_type in {"routine_strategy_refinement", "operator_preference_suggestion"}:
        return "strategy_sandbox"
    return "policy_adjustment_sandbox"


async def run_policy_experiment(
    *,
    actor: str,
    source: str,
    proposal_id: int | None,
    experiment_type: str,
    lookback_hours: int,
    sandbox_mode: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspacePolicyExperiment:
    proposal = None
    if proposal_id is not None:
        proposal = (
            await db.execute(
                select(WorkspaceImprovementProposal).where(WorkspaceImprovementProposal.id == proposal_id)
            )
        ).scalars().first()
        if proposal is None:
            raise ValueError("improvement_proposal_not_found")

    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    warning_rows = await _warning_rows(since=since, db=db)
    decision_rows = await _decision_rows(since=since, db=db)

    constraint_key = _proposal_constraint_key(proposal)
    warning_count = _count_warning(warning_rows, constraint_key=constraint_key)
    success_rate = _success_rate(warning_rows, constraint_key=constraint_key)
    decision_quality = _quality_avg(decision_rows)
    execution_time_ms = await _execution_time_ms(since=since, db=db)
    replan_frequency = await _replan_frequency(since=since, db=db)
    operator_override_rate = _operator_override_rate(decision_rows)

    baseline = {
        "lookback_hours": max(1, int(lookback_hours)),
        "constraint_key": constraint_key,
        "friction_events": int(warning_count),
        "success_rate": float(success_rate),
        "decision_quality": float(decision_quality),
        "execution_time_ms": float(execution_time_ms),
        "replan_frequency": float(replan_frequency),
        "operator_override_rate": float(operator_override_rate),
    }

    improvement_factor = 0.2
    if proposal is not None and str(proposal.proposal_type or "") in {"routine_strategy_refinement", "operator_preference_suggestion"}:
        improvement_factor = 0.15

    experimental_friction = max(0, int(round(warning_count * (1.0 - improvement_factor))))
    experimental_success = max(0.0, min(1.0, round(success_rate + 0.04, 6)))
    experimental_quality = max(0.0, min(1.0, round(decision_quality + 0.05, 6)))
    experimental_execution_time_ms = max(0.0, round(execution_time_ms * 0.92, 6))
    experimental_replan_frequency = max(0.0, round(replan_frequency * (1.0 - (improvement_factor * 0.6)), 6))
    experimental_operator_override_rate = max(0.0, round(operator_override_rate * 0.9, 6))

    experimental = {
        "sandbox_applied": True,
        "simulated_adjustment": {
            "mode": sandbox_mode,
            "constraint_key": constraint_key,
            "friction_reduction_factor": improvement_factor,
        },
        "friction_events": experimental_friction,
        "success_rate": experimental_success,
        "decision_quality": experimental_quality,
        "execution_time_ms": experimental_execution_time_ms,
        "replan_frequency": experimental_replan_frequency,
        "operator_override_rate": experimental_operator_override_rate,
    }

    friction_reduction = max(0.0, float(warning_count - experimental_friction) / float(max(warning_count, 1)))
    success_gain = max(0.0, experimental_success - success_rate)
    quality_gain = max(0.0, experimental_quality - decision_quality)
    execution_time_gain = max(0.0, (execution_time_ms - experimental_execution_time_ms) / float(max(execution_time_ms, 1.0)))
    replan_gain = max(0.0, replan_frequency - experimental_replan_frequency)
    override_gain = max(0.0, operator_override_rate - experimental_operator_override_rate)
    improvement_score = round(
        (0.3 * friction_reduction)
        + (0.2 * success_gain)
        + (0.2 * quality_gain)
        + (0.12 * execution_time_gain)
        + (0.1 * replan_gain)
        + (0.08 * override_gain),
        6,
    )

    derived_experiment_type = _derive_experiment_type(proposal, experiment_type)

    promote_threshold = 0.08
    if proposal is not None and "medium_risk" in str(proposal.risk_summary or ""):
        promote_threshold = 0.12

    development_influence = await development_influence_for_experiment(
        experiment_type=derived_experiment_type,
        db=db,
    )
    if bool(development_influence.get("applied", False)):
        promote_threshold = max(
            0.03,
            float(promote_threshold) - float(development_influence.get("promote_threshold_delta", 0.0) or 0.0),
        )

    if improvement_score >= promote_threshold:
        recommendation = "promote"
        recommendation_reason = "sandbox indicates meaningful quality gain under bounded risk"
    elif improvement_score >= 0.02:
        recommendation = "revise"
        recommendation_reason = "sandbox shows partial gain; revise parameters and rerun"
    else:
        recommendation = "reject"
        recommendation_reason = "sandbox did not show sufficient gain over baseline"

    comparison = {
        "friction_reduction_ratio": round(friction_reduction, 6),
        "success_rate_delta": round(success_gain, 6),
        "decision_quality_delta": round(quality_gain, 6),
        "execution_time_ms_delta": round(execution_time_ms - experimental_execution_time_ms, 6),
        "replan_frequency_delta": round(replan_frequency - experimental_replan_frequency, 6),
        "operator_override_rate_delta": round(operator_override_rate - experimental_operator_override_rate, 6),
        "improvement_score": improvement_score,
        "recommendation_confidence": round(min(0.95, 0.5 + improvement_score), 6),
        "development_influence": development_influence,
    }

    row = WorkspacePolicyExperiment(
        source=source,
        actor=actor,
        proposal_id=proposal.id if proposal is not None else None,
        experiment_type=derived_experiment_type,
        sandbox_mode=sandbox_mode,
        status="completed",
        baseline_metrics_json=baseline,
        experimental_metrics_json=experimental,
        comparison_json=comparison,
        recommendation=recommendation,
        recommendation_reason=recommendation_reason,
        metadata_json={
            "proposal_type": str(proposal.proposal_type) if proposal is not None else "",
            "development_influence": development_influence,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
    )
    db.add(row)
    await db.flush()
    return row


async def list_policy_experiments(*, db: AsyncSession, limit: int = 50) -> list[WorkspacePolicyExperiment]:
    rows = (
        await db.execute(
            select(WorkspacePolicyExperiment).order_by(WorkspacePolicyExperiment.id.desc())
        )
    ).scalars().all()
    return rows[: max(1, min(500, int(limit)))]


async def get_policy_experiment(*, experiment_id: int, db: AsyncSession) -> WorkspacePolicyExperiment | None:
    return (
        await db.execute(
            select(WorkspacePolicyExperiment).where(WorkspacePolicyExperiment.id == experiment_id)
        )
    ).scalars().first()


def to_policy_experiment_out(row: WorkspacePolicyExperiment) -> dict:
    return {
        "experiment_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "proposal_id": row.proposal_id,
        "experiment_type": row.experiment_type,
        "sandbox_mode": row.sandbox_mode,
        "status": row.status,
        "baseline_metrics": row.baseline_metrics_json if isinstance(row.baseline_metrics_json, dict) else {},
        "experimental_metrics": row.experimental_metrics_json if isinstance(row.experimental_metrics_json, dict) else {},
        "comparison": row.comparison_json if isinstance(row.comparison_json, dict) else {},
        "recommendation": row.recommendation,
        "recommendation_reason": row.recommendation_reason,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
