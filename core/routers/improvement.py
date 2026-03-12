from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.improvement_recommendation_service import (
    approve_improvement_recommendation,
    generate_improvement_recommendations,
    get_improvement_recommendation,
    list_improvement_recommendations,
    reject_improvement_recommendation,
    to_improvement_recommendation_out,
    to_improvement_recommendation_out_resolved,
)
from core.improvement_service import (
    accept_improvement_proposal,
    generate_improvement_proposals,
    get_improvement_proposal,
    list_improvement_artifacts_for_proposal,
    list_improvement_proposals,
    reject_improvement_proposal,
    to_improvement_artifact_out,
    to_improvement_proposal_out,
)
from core.journal import write_journal
from core.schemas import (
    ImprovementProposalGenerateRequest,
    ImprovementProposalReviewRequest,
    ImprovementRecommendationGenerateRequest,
    ImprovementRecommendationReviewRequest,
)

router = APIRouter()


@router.post("/improvement/proposals/generate")
async def generate_improvement_proposals_endpoint(
    payload: ImprovementProposalGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    created = await generate_improvement_proposals(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        min_occurrence_count=payload.min_occurrence_count,
        max_proposals=payload.max_proposals,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_proposals_generated",
        target_type="workspace_improvement_proposal",
        target_id="proposal_batch",
        summary=f"Generated {len(created)} improvement proposal(s)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_occurrence_count": payload.min_occurrence_count,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "generated": len(created),
        "proposals": [to_improvement_proposal_out(item) for item in created],
    }


@router.get("/improvement/proposals")
async def list_improvement_proposals_endpoint(
    status: str = Query(default=""),
    proposal_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_improvement_proposals(
        db=db,
        status=status,
        proposal_type=proposal_type,
        limit=limit,
    )
    return {
        "proposals": [to_improvement_proposal_out(item) for item in rows],
    }


@router.get("/improvement/proposals/{proposal_id}")
async def get_improvement_proposal_endpoint(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_improvement_proposal(proposal_id=proposal_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="improvement_proposal_not_found")
    artifacts = await list_improvement_artifacts_for_proposal(proposal_id=row.id, db=db)
    latest = artifacts[0] if artifacts else None
    return {
        "proposal": to_improvement_proposal_out(row, latest_artifact=latest),
    }


@router.post("/improvement/proposals/{proposal_id}/accept")
async def accept_improvement_proposal_endpoint(
    proposal_id: int,
    payload: ImprovementProposalReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await get_improvement_proposal(proposal_id=proposal_id, db=db)
    if not proposal:
        raise HTTPException(status_code=404, detail="improvement_proposal_not_found")

    try:
        artifact = await accept_improvement_proposal(
            proposal=proposal,
            actor=payload.actor,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "proposal_not_open":
            raise HTTPException(status_code=422, detail="improvement_proposal_not_open")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_proposal_accepted",
        target_type="workspace_improvement_proposal",
        target_id=str(proposal.id),
        summary=f"Accepted improvement proposal {proposal.id}",
        metadata_json={
            "artifact_id": artifact.id,
            "artifact_type": artifact.artifact_type,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "proposal": to_improvement_proposal_out(proposal, latest_artifact=artifact),
        "artifact": to_improvement_artifact_out(artifact),
    }


@router.post("/improvement/proposals/{proposal_id}/reject")
async def reject_improvement_proposal_endpoint(
    proposal_id: int,
    payload: ImprovementProposalReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await get_improvement_proposal(proposal_id=proposal_id, db=db)
    if not proposal:
        raise HTTPException(status_code=404, detail="improvement_proposal_not_found")

    try:
        await reject_improvement_proposal(
            proposal=proposal,
            actor=payload.actor,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
        )
    except ValueError as exc:
        if str(exc) == "proposal_not_open":
            raise HTTPException(status_code=422, detail="improvement_proposal_not_open")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_proposal_rejected",
        target_type="workspace_improvement_proposal",
        target_id=str(proposal.id),
        summary=f"Rejected improvement proposal {proposal.id}",
        metadata_json={
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "proposal": to_improvement_proposal_out(proposal),
    }


@router.post("/improvement/recommendations/generate")
async def generate_improvement_recommendations_endpoint(
    payload: ImprovementRecommendationGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    created = await generate_improvement_recommendations(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        min_occurrence_count=payload.min_occurrence_count,
        max_recommendations=payload.max_recommendations,
        include_existing_open_proposals=payload.include_existing_open_proposals,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_recommendations_generated",
        target_type="workspace_improvement_recommendation",
        target_id="recommendation_batch",
        summary=f"Generated {len(created)} improvement recommendation(s)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_occurrence_count": payload.min_occurrence_count,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "generated": len(created),
        "recommendations": [to_improvement_recommendation_out(item) for item in created],
    }


@router.get("/improvement/recommendations")
async def list_improvement_recommendations_endpoint(
    status: str = Query(default=""),
    recommendation_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_improvement_recommendations(
        db=db,
        status=status,
        recommendation_type=recommendation_type,
        limit=limit,
    )
    return {
        "recommendations": [await to_improvement_recommendation_out_resolved(row=item, db=db) for item in rows],
    }


@router.get("/improvement/recommendations/{recommendation_id}")
async def get_improvement_recommendation_endpoint(
    recommendation_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_improvement_recommendation(recommendation_id=recommendation_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="improvement_recommendation_not_found")
    return {
        "recommendation": await to_improvement_recommendation_out_resolved(row=row, db=db),
    }


@router.post("/improvement/recommendations/{recommendation_id}/approve")
async def approve_improvement_recommendation_endpoint(
    recommendation_id: int,
    payload: ImprovementRecommendationReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_improvement_recommendation(recommendation_id=recommendation_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="improvement_recommendation_not_found")

    try:
        artifact = await approve_improvement_recommendation(
            row=row,
            actor=payload.actor,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "recommendation_not_open":
            raise HTTPException(status_code=422, detail="improvement_recommendation_not_open")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_recommendation_approved",
        target_type="workspace_improvement_recommendation",
        target_id=str(row.id),
        summary=f"Approved improvement recommendation {row.id}",
        metadata_json={
            "artifact_id": artifact.id,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "recommendation": await to_improvement_recommendation_out_resolved(row=row, db=db),
    }


@router.post("/improvement/recommendations/{recommendation_id}/reject")
async def reject_improvement_recommendation_endpoint(
    recommendation_id: int,
    payload: ImprovementRecommendationReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_improvement_recommendation(recommendation_id=recommendation_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="improvement_recommendation_not_found")

    try:
        await reject_improvement_recommendation(
            row=row,
            actor=payload.actor,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
        )
    except ValueError as exc:
        if str(exc) == "recommendation_not_open":
            raise HTTPException(status_code=422, detail="improvement_recommendation_not_open")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="improvement_recommendation_rejected",
        target_type="workspace_improvement_recommendation",
        target_id=str(row.id),
        summary=f"Rejected improvement recommendation {row.id}",
        metadata_json={
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "recommendation": await to_improvement_recommendation_out_resolved(row=row, db=db),
    }
