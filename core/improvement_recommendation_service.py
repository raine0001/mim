from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.improvement_service import generate_improvement_proposals, get_improvement_proposal, list_improvement_artifacts_for_proposal, list_improvement_proposals, to_improvement_artifact_out
from core.models import WorkspaceImprovementArtifact, WorkspaceImprovementRecommendation
from core.policy_experiment_service import run_policy_experiment


RECOMMENDATION_STATUSES = {"proposed", "approved", "rejected", "superseded"}


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


async def _existing_open_recommendation(*, proposal_id: int, db: AsyncSession) -> WorkspaceImprovementRecommendation | None:
    return (
        await db.execute(
            select(WorkspaceImprovementRecommendation)
            .where(WorkspaceImprovementRecommendation.proposal_id == proposal_id)
            .where(WorkspaceImprovementRecommendation.status == "proposed")
            .order_by(WorkspaceImprovementRecommendation.id.desc())
        )
    ).scalars().first()


async def generate_improvement_recommendations(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_occurrence_count: int,
    max_recommendations: int,
    include_existing_open_proposals: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceImprovementRecommendation]:
    generated_proposals = await generate_improvement_proposals(
        actor=actor,
        source=source,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        max_proposals=max(max_recommendations * 2, 5),
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective54_trigger": True,
        },
        db=db,
    )

    proposal_rows = list(generated_proposals)
    if include_existing_open_proposals:
        existing_open = await list_improvement_proposals(db=db, status="proposed", proposal_type="", limit=200)
        by_id = {int(item.id): item for item in proposal_rows}
        for item in existing_open:
            by_id[int(item.id)] = item
        proposal_rows = list(by_id.values())

    proposal_rows = sorted(proposal_rows, key=lambda item: float(item.confidence or 0.0), reverse=True)

    created: list[WorkspaceImprovementRecommendation] = []
    for proposal in proposal_rows:
        if len(created) >= max(1, min(100, int(max_recommendations))):
            break

        duplicate = await _existing_open_recommendation(proposal_id=int(proposal.id), db=db)
        if duplicate:
            continue

        experiment = await run_policy_experiment(
            actor=actor,
            source=source,
            proposal_id=int(proposal.id),
            experiment_type="",
            lookback_hours=lookback_hours,
            sandbox_mode="shadow_evaluation",
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective54_orchestration": True,
            },
            db=db,
        )

        comparison = experiment.comparison_json if isinstance(experiment.comparison_json, dict) else {}
        recommendation_type = str(experiment.recommendation or "revise")

        row = WorkspaceImprovementRecommendation(
            source=source,
            actor=actor,
            proposal_id=int(proposal.id),
            experiment_id=int(experiment.id),
            recommendation_type=recommendation_type,
            recommendation_summary=_recommendation_summary(
                recommendation=recommendation_type,
                comparison=comparison,
            ),
            baseline_metrics_json=experiment.baseline_metrics_json if isinstance(experiment.baseline_metrics_json, dict) else {},
            experimental_metrics_json=experiment.experimental_metrics_json if isinstance(experiment.experimental_metrics_json, dict) else {},
            comparison_json=comparison,
            status="proposed",
            metadata_json={
                "triggered_from_development_pattern": bool(
                    (proposal.metadata_json or {}).get("related_development_pattern_ids", [])
                    if isinstance(proposal.metadata_json, dict)
                    else []
                ),
                "proposal_trigger_pattern": proposal.trigger_pattern,
                "objective54_orchestration": True,
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(row)
        created.append(row)

    await db.flush()
    return created


async def list_improvement_recommendations(
    *,
    db: AsyncSession,
    status: str = "",
    recommendation_type: str = "",
    limit: int = 50,
) -> list[WorkspaceImprovementRecommendation]:
    rows = (
        await db.execute(
            select(WorkspaceImprovementRecommendation)
            .order_by(WorkspaceImprovementRecommendation.id.desc())
        )
    ).scalars().all()
    if status:
        requested = status.strip().lower()
        if requested in RECOMMENDATION_STATUSES:
            rows = [item for item in rows if str(item.status).strip().lower() == requested]
    if recommendation_type:
        requested_type = recommendation_type.strip().lower()
        rows = [item for item in rows if str(item.recommendation_type).strip().lower() == requested_type]
    return rows[: max(1, min(500, int(limit)))]


async def get_improvement_recommendation(*, recommendation_id: int, db: AsyncSession) -> WorkspaceImprovementRecommendation | None:
    return (
        await db.execute(
            select(WorkspaceImprovementRecommendation).where(WorkspaceImprovementRecommendation.id == recommendation_id)
        )
    ).scalars().first()


async def _latest_artifact_for_recommendation(*, row: WorkspaceImprovementRecommendation, db: AsyncSession) -> WorkspaceImprovementArtifact | None:
    proposal = await get_improvement_proposal(proposal_id=int(row.proposal_id), db=db)
    if not proposal:
        return None
    artifacts = await list_improvement_artifacts_for_proposal(proposal_id=proposal.id, db=db)
    return artifacts[0] if artifacts else None


async def approve_improvement_recommendation(
    *,
    row: WorkspaceImprovementRecommendation,
    actor: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceImprovementArtifact:
    if row.status != "proposed":
        raise ValueError("recommendation_not_open")

    artifact = WorkspaceImprovementArtifact(
        proposal_id=int(row.proposal_id),
        artifact_type="promotion_recommendation",
        status="pending_review",
        candidate_payload_json={
            "recommendation_id": int(row.id),
            "experiment_id": int(row.experiment_id),
            "recommendation_type": row.recommendation_type,
            "recommendation_summary": row.recommendation_summary,
            "baseline_metrics": row.baseline_metrics_json if isinstance(row.baseline_metrics_json, dict) else {},
            "experimental_metrics": row.experimental_metrics_json if isinstance(row.experimental_metrics_json, dict) else {},
            "comparison": row.comparison_json if isinstance(row.comparison_json, dict) else {},
        },
        metadata_json={
            "approved_by": actor,
            "approve_reason": reason,
            "objective54_gated_promotion": True,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
    )
    db.add(artifact)

    row.status = "approved"
    row.review_reason = reason or row.review_reason
    row.reviewed_by = actor
    row.reviewed_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "approved_by": actor,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    await db.flush()
    return artifact


async def reject_improvement_recommendation(
    *,
    row: WorkspaceImprovementRecommendation,
    actor: str,
    reason: str,
    metadata_json: dict,
) -> WorkspaceImprovementRecommendation:
    if row.status != "proposed":
        raise ValueError("recommendation_not_open")

    row.status = "rejected"
    row.review_reason = reason or row.review_reason
    row.reviewed_by = actor
    row.reviewed_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "rejected_by": actor,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    return row


def to_improvement_recommendation_out(
    row: WorkspaceImprovementRecommendation,
    *,
    latest_artifact: WorkspaceImprovementArtifact | None = None,
) -> dict:
    return {
        "recommendation_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "proposal_id": int(row.proposal_id),
        "experiment_id": int(row.experiment_id),
        "recommendation_type": row.recommendation_type,
        "recommendation_summary": row.recommendation_summary,
        "baseline_metrics": row.baseline_metrics_json if isinstance(row.baseline_metrics_json, dict) else {},
        "experimental_metrics": row.experimental_metrics_json if isinstance(row.experimental_metrics_json, dict) else {},
        "comparison": row.comparison_json if isinstance(row.comparison_json, dict) else {},
        "status": row.status,
        "review_reason": row.review_reason,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
        "latest_artifact": to_improvement_artifact_out(latest_artifact) if latest_artifact else None,
    }


async def to_improvement_recommendation_out_resolved(*, row: WorkspaceImprovementRecommendation, db: AsyncSession) -> dict:
    artifact = await _latest_artifact_for_recommendation(row=row, db=db)
    return to_improvement_recommendation_out(row, latest_artifact=artifact)
