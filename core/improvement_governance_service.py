from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.improvement_service import generate_improvement_proposals, list_improvement_proposals
from core.models import (
    UserPreference,
    WorkspaceImprovementBacklog,
    WorkspaceImprovementProposal,
    WorkspaceImprovementRecommendation,
)
from core.policy_experiment_service import run_policy_experiment


BACKLOG_STATUSES = {
    "proposed",
    "queued",
    "experimenting",
    "evaluating",
    "recommended",
    "approved",
    "rejected",
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _risk_score_from_summary(risk_summary: str) -> float:
    lowered = str(risk_summary or "").lower()
    if "low_risk" in lowered:
        return 0.2
    if "high_risk" in lowered:
        return 0.85
    return 0.55


def _risk_level_from_score(risk_score: float) -> str:
    if risk_score >= 0.75:
        return "high"
    if risk_score <= 0.35:
        return "low"
    return "medium"


def _extract_evidence_count(proposal: WorkspaceImprovementProposal) -> int:
    evidence = proposal.evidence_json if isinstance(proposal.evidence_json, dict) else {}
    count = int(evidence.get("count", 0) or 0)
    if count > 0:
        return count

    sample_keys = [
        "sample_evaluation_ids",
        "sample_decision_ids",
        "sample_replan_event_ids",
        "sample_action_ids",
        "sample_strategy_ids",
    ]
    for key in sample_keys:
        values = evidence.get(key, [])
        if isinstance(values, list) and values:
            return len(values)

    return max(1, int(round(float(proposal.confidence or 0.0) * 10.0)))


def _affected_capabilities_for_component(affected_component: str) -> list[str]:
    component = str(affected_component or "").lower()
    if component.startswith("constraint:"):
        return ["constraint_weight_learning", "constraint_evaluation_engine"]
    if component.startswith("horizon_planning:"):
        return ["long_horizon_planning", "policy_based_autonomous_priority_selection"]
    if component.startswith("environment_strategy:"):
        return ["environment_strategy_formation", "human_preference_strategy_integration"]
    if component.startswith("action:"):
        return ["closed_loop_autonomous_task_execution", "autonomous_task_execution_policies"]
    if component.startswith("workspace_autonomy_policy"):
        return ["autonomous_task_execution_policies", "closed_loop_autonomous_task_execution"]
    return ["self_improvement_proposal_engine"]


async def _operator_preference_weight(*, proposal_type: str, db: AsyncSession) -> float:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.in_([
                f"improvement_priority:{proposal_type}",
                "improvement_priority:default",
            ]))
            .order_by(UserPreference.last_updated.desc())
            .limit(10)
        )
    ).scalars().all()

    for row in rows:
        value = row.value
        if isinstance(value, (int, float)):
            return _bounded(float(value))
        if isinstance(value, dict):
            if isinstance(value.get("weight"), (int, float)):
                return _bounded(float(value.get("weight")))
            if isinstance(value.get("priority_weight"), (int, float)):
                return _bounded(float(value.get("priority_weight")))
    return 0.5


def _impact_estimate(proposal: WorkspaceImprovementProposal) -> float:
    base = float(proposal.confidence or 0.0)
    proposal_type = str(proposal.proposal_type or "")
    if proposal_type in {"policy_adjustment", "priority_rule_refinement"}:
        base += 0.08
    if proposal_type in {"routine_strategy_refinement", "capability_workflow_improvement"}:
        base += 0.04
    return _bounded(base)


def _evidence_strength(*, evidence_count: int, confidence: float) -> float:
    count_component = _bounded(float(evidence_count) / 10.0)
    confidence_component = _bounded(float(confidence or 0.0))
    return _bounded((count_component * 0.6) + (confidence_component * 0.4))


def _priority_score(
    *,
    impact_estimate: float,
    evidence_strength: float,
    risk_score: float,
    affected_capability_count: int,
    operator_preference_weight: float,
) -> float:
    cap_factor = _bounded(float(affected_capability_count) / 5.0)
    score = (
        (impact_estimate * 0.35)
        + (evidence_strength * 0.25)
        + ((1.0 - risk_score) * 0.2)
        + (cap_factor * 0.1)
        + (operator_preference_weight * 0.1)
    )
    return _bounded(score)


def _governance_decision(*, priority_score: float, evidence_count: int, risk_score: float) -> str:
    if risk_score >= 0.85 and priority_score < 0.6:
        return "reject_improvement"
    if risk_score >= 0.75:
        return "request_operator_review"
    if priority_score >= 0.72 and evidence_count >= 2:
        return "auto_experiment"
    if priority_score >= 0.45:
        return "request_operator_review"
    return "defer_improvement"


async def _existing_backlog_for_proposal(*, proposal_id: int, db: AsyncSession) -> WorkspaceImprovementBacklog | None:
    return (
        await db.execute(
            select(WorkspaceImprovementBacklog)
            .where(WorkspaceImprovementBacklog.proposal_id == proposal_id)
            .order_by(WorkspaceImprovementBacklog.id.desc())
        )
    ).scalars().first()


async def _latest_recommendation_for_proposal(*, proposal_id: int, db: AsyncSession) -> WorkspaceImprovementRecommendation | None:
    return (
        await db.execute(
            select(WorkspaceImprovementRecommendation)
            .where(WorkspaceImprovementRecommendation.proposal_id == proposal_id)
            .order_by(WorkspaceImprovementRecommendation.id.desc())
        )
    ).scalars().first()


def _recommendation_summary(*, recommendation: str, comparison: dict) -> str:
    score = float(comparison.get("improvement_score", 0.0) or 0.0)
    success_delta = float(comparison.get("success_rate_delta", 0.0) or 0.0)
    replan_delta = float(comparison.get("replan_frequency_delta", 0.0) or 0.0)
    override_delta = float(comparison.get("operator_override_rate_delta", 0.0) or 0.0)
    return (
        f"Recommendation={recommendation}; improvement_score={score:.4f}; "
        f"success_delta={success_delta:.4f}; replan_delta={replan_delta:.4f}; "
        f"operator_override_delta={override_delta:.4f}"
    )


async def refresh_improvement_backlog(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_occurrence_count: int,
    max_items: int,
    auto_experiment_limit: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceImprovementBacklog]:
    await generate_improvement_proposals(
        actor=actor,
        source=source,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        max_proposals=max(5, int(max_items) * 2),
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective55_backlog_refresh": True,
        },
        db=db,
    )

    proposals = await list_improvement_proposals(db=db, status="", proposal_type="", limit=500)

    refreshed_rows: list[WorkspaceImprovementBacklog] = []
    for proposal in proposals:
        proposal_id = int(proposal.id)
        evidence_count = _extract_evidence_count(proposal)
        risk_score = _risk_score_from_summary(proposal.risk_summary)
        risk_level = _risk_level_from_score(risk_score)
        capabilities = _affected_capabilities_for_component(proposal.affected_component)
        impact_estimate = _impact_estimate(proposal)
        evidence_strength = _evidence_strength(
            evidence_count=evidence_count,
            confidence=float(proposal.confidence or 0.0),
        )
        pref_weight = await _operator_preference_weight(proposal_type=proposal.proposal_type, db=db)
        priority = _priority_score(
            impact_estimate=impact_estimate,
            evidence_strength=evidence_strength,
            risk_score=risk_score,
            affected_capability_count=len(capabilities),
            operator_preference_weight=pref_weight,
        )
        decision = _governance_decision(
            priority_score=priority,
            evidence_count=evidence_count,
            risk_score=risk_score,
        )

        row = await _existing_backlog_for_proposal(proposal_id=proposal_id, db=db)
        if not row:
            row = WorkspaceImprovementBacklog(
                source=source,
                actor=actor,
                proposal_id=proposal_id,
                status="proposed",
            )
            db.add(row)

        row.proposal_type = proposal.proposal_type
        row.priority_score = priority
        row.impact_estimate = impact_estimate
        row.evidence_strength = evidence_strength
        row.risk_level = risk_level
        row.risk_score = risk_score
        row.affected_capabilities = capabilities
        row.operator_preference_weight = pref_weight
        row.evidence_count = evidence_count
        row.governance_decision = decision
        row.evidence_summary = proposal.evidence_summary
        row.risk_summary = proposal.risk_summary
        row.ranking_reason = (
            f"priority={priority:.3f} from impact={impact_estimate:.3f}, evidence={evidence_strength:.3f}, "
            f"risk_penalty={risk_score:.3f}, capabilities={len(capabilities)}, operator_pref={pref_weight:.3f}"
        )
        row.reasoning_json = {
            "impact_estimate": impact_estimate,
            "evidence_strength": evidence_strength,
            "risk_level": risk_level,
            "risk_score": risk_score,
            "affected_capabilities": capabilities,
            "operator_preference_weight": pref_weight,
            "governance_policy": {
                "auto_experiment_if": "priority_score>=0.72 and evidence_count>=2 and risk_level!=high",
                "operator_review_if": "priority_score>=0.45 or risk_level==high",
                "defer_if": "priority_score<0.45",
                "reject_if": "risk_level==high and priority_score<0.60",
            },
        }
        row.metadata_json = {
            **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

        if proposal.status == "rejected":
            row.status = "rejected"
        elif row.status not in {"experimenting", "evaluating", "recommended", "approved", "rejected"}:
            if decision == "reject_improvement":
                row.status = "rejected"
            else:
                row.status = "queued"

        refreshed_rows.append(row)

    await db.flush()

    eligible = [
        row
        for row in refreshed_rows
        if row.status == "queued" and row.governance_decision == "auto_experiment"
    ]
    eligible.sort(key=lambda item: float(item.priority_score or 0.0), reverse=True)

    for row in eligible[: max(0, int(auto_experiment_limit))]:
        proposal = (
            await db.execute(
                select(WorkspaceImprovementProposal).where(WorkspaceImprovementProposal.id == row.proposal_id)
            )
        ).scalars().first()
        if not proposal:
            continue

        row.status = "experimenting"
        await db.flush()

        experiment = await run_policy_experiment(
            actor=actor,
            source=source,
            proposal_id=int(proposal.id),
            experiment_type="",
            lookback_hours=lookback_hours,
            sandbox_mode="shadow_evaluation",
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective55_auto_experiment": True,
            },
            db=db,
        )

        row.status = "evaluating"
        await db.flush()

        recommendation = await _latest_recommendation_for_proposal(proposal_id=int(proposal.id), db=db)
        comparison = experiment.comparison_json if isinstance(experiment.comparison_json, dict) else {}
        if not recommendation:
            recommendation = WorkspaceImprovementRecommendation(
                source=source,
                actor=actor,
                proposal_id=int(proposal.id),
                experiment_id=int(experiment.id),
                recommendation_type=str(experiment.recommendation or "revise"),
                recommendation_summary=_recommendation_summary(
                    recommendation=str(experiment.recommendation or "revise"),
                    comparison=comparison,
                ),
                baseline_metrics_json=experiment.baseline_metrics_json if isinstance(experiment.baseline_metrics_json, dict) else {},
                experimental_metrics_json=experiment.experimental_metrics_json if isinstance(experiment.experimental_metrics_json, dict) else {},
                comparison_json=comparison,
                status="proposed",
                metadata_json={
                    "objective55_backlog_generated": True,
                    "proposal_trigger_pattern": proposal.trigger_pattern,
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                },
            )
            db.add(recommendation)
            await db.flush()

        row.recommendation_id = int(recommendation.id)
        row.status = "recommended"

    for row in refreshed_rows:
        if row.recommendation_id is None:
            continue
        rec = (
            await db.execute(
                select(WorkspaceImprovementRecommendation).where(WorkspaceImprovementRecommendation.id == row.recommendation_id)
            )
        ).scalars().first()
        if not rec:
            continue
        rec_status = str(rec.status or "").lower()
        if rec_status == "approved":
            row.status = "approved"
        elif rec_status == "rejected":
            row.status = "rejected"
        elif rec_status == "proposed":
            row.status = "recommended"

    await db.flush()

    listed = (
        await db.execute(
            select(WorkspaceImprovementBacklog).order_by(WorkspaceImprovementBacklog.priority_score.desc(), WorkspaceImprovementBacklog.id.desc())
        )
    ).scalars().all()
    return listed[: max(1, min(500, int(max_items)))]


async def list_improvement_backlog(
    *,
    db: AsyncSession,
    status: str = "",
    risk_level: str = "",
    limit: int = 50,
) -> list[WorkspaceImprovementBacklog]:
    rows = (
        await db.execute(
            select(WorkspaceImprovementBacklog).order_by(WorkspaceImprovementBacklog.priority_score.desc(), WorkspaceImprovementBacklog.id.desc())
        )
    ).scalars().all()
    filtered = rows
    if status:
        requested = status.strip().lower()
        if requested in BACKLOG_STATUSES:
            filtered = [item for item in filtered if str(item.status).strip().lower() == requested]
    if risk_level:
        requested_risk = risk_level.strip().lower()
        filtered = [item for item in filtered if str(item.risk_level).strip().lower() == requested_risk]
    return filtered[: max(1, min(500, int(limit)))]


async def get_improvement_backlog_item(*, backlog_id: int, db: AsyncSession) -> WorkspaceImprovementBacklog | None:
    return (
        await db.execute(
            select(WorkspaceImprovementBacklog).where(WorkspaceImprovementBacklog.id == backlog_id)
        )
    ).scalars().first()


def to_improvement_backlog_out(row: WorkspaceImprovementBacklog) -> dict:
    return {
        "improvement_id": row.id,
        "proposal_id": int(row.proposal_id),
        "recommendation_id": int(row.recommendation_id) if row.recommendation_id is not None else None,
        "priority_score": float(row.priority_score or 0.0),
        "proposal_type": row.proposal_type,
        "evidence_count": int(row.evidence_count or 0),
        "risk_level": row.risk_level,
        "impact_estimate": float(row.impact_estimate or 0.0),
        "evidence_strength": float(row.evidence_strength or 0.0),
        "affected_capabilities": row.affected_capabilities if isinstance(row.affected_capabilities, list) else [],
        "operator_preference_weight": float(row.operator_preference_weight or 0.0),
        "governance_decision": row.governance_decision,
        "status": row.status,
        "why_ranked": row.ranking_reason,
        "evidence_summary": row.evidence_summary,
        "risk_summary": row.risk_summary,
        "reasoning": row.reasoning_json if isinstance(row.reasoning_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
