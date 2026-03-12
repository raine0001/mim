from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.goal_strategy_service import (
    build_strategy_goals,
    get_strategy_goal,
    list_strategy_goals,
    list_strategy_goal_reviews,
    recompute_strategy_goal_persistence,
    review_strategy_goal,
    to_strategy_goal_review_out,
    to_strategy_goal_out,
)
from core.journal import write_journal
from core.schemas import StrategyGoalBuildRequest, StrategyGoalPersistenceRecomputeRequest, StrategyGoalReviewRequest

router = APIRouter()


@router.post("/strategy/goals/build")
async def build_strategy_goals_endpoint(
    payload: StrategyGoalBuildRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    context, rows, synthesis = await build_strategy_goals(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        max_items_per_domain=payload.max_items_per_domain,
        max_goals=payload.max_goals,
        min_context_confidence=payload.min_context_confidence,
        min_domains_required=payload.min_domains_required,
        min_cross_domain_links=payload.min_cross_domain_links,
        generate_horizon_plans=payload.generate_horizon_plans,
        generate_improvement_proposals=payload.generate_improvement_proposals,
        generate_maintenance_cycles=payload.generate_maintenance_cycles,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="strategy_goals_built",
        target_type="workspace_strategy_goal",
        target_id="strategy_goal_batch",
        summary=f"Built {len(rows)} strategy goal(s)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "max_goals": payload.max_goals,
            "min_context_confidence": payload.min_context_confidence,
            "min_domains_required": payload.min_domains_required,
            "min_cross_domain_links": payload.min_cross_domain_links,
            "gating_reasons": synthesis.get("gating_reasons", []),
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "generated": len(rows),
        "origin_context": context,
        "goals": [to_strategy_goal_out(item) for item in rows],
        "synthesis": synthesis,
    }


@router.get("/strategy/goals")
async def list_strategy_goals_endpoint(
    status: str = Query(default=""),
    strategy_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_strategy_goals(
        db=db,
        status=status,
        strategy_type=strategy_type,
        limit=limit,
    )
    return {
        "goals": [to_strategy_goal_out(item) for item in rows],
    }


@router.get("/strategy/goals/{strategy_goal_id}")
async def get_strategy_goal_endpoint(
    strategy_goal_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_strategy_goal(strategy_goal_id=strategy_goal_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="strategy_goal_not_found")
    return {
        "goal": to_strategy_goal_out(row),
    }


@router.post("/strategy/persistence/goals/recompute")
async def recompute_strategy_goal_persistence_endpoint(
    payload: StrategyGoalPersistenceRecomputeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows, summary = await recompute_strategy_goal_persistence(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        min_support_count=payload.min_support_count,
        min_persistence_confidence=payload.min_persistence_confidence,
        limit=payload.limit,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="strategy_goal_persistence_recomputed",
        target_type="workspace_strategy_goal",
        target_id="strategy_goal_persistence_batch",
        summary=f"Recomputed persistence for {len(rows)} strategy goal(s)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_support_count": payload.min_support_count,
            "min_persistence_confidence": payload.min_persistence_confidence,
            "summary": summary,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "updated": len(rows),
        "summary": summary,
        "goals": [to_strategy_goal_out(item) for item in rows],
    }


@router.get("/strategy/persistence/goals")
async def list_persistent_strategy_goals_endpoint(
    persistence_state: str = Query(default=""),
    review_status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_strategy_goals(db=db, limit=limit)
    filtered = rows
    if persistence_state.strip():
        requested_state = persistence_state.strip().lower()
        filtered = [item for item in filtered if str(item.persistence_state or "").strip().lower() == requested_state]
    if review_status.strip():
        requested_review = review_status.strip().lower()
        filtered = [item for item in filtered if str(item.review_status or "").strip().lower() == requested_review]
    return {
        "goals": [to_strategy_goal_out(item) for item in filtered[: max(1, min(500, int(limit))) ]],
    }


@router.post("/strategy/goals/{strategy_goal_id}/review")
async def review_strategy_goal_endpoint(
    strategy_goal_id: int,
    payload: StrategyGoalReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    goal, review = await review_strategy_goal(
        strategy_goal_id=strategy_goal_id,
        actor=payload.actor,
        decision=payload.decision,
        reason=payload.reason,
        evidence_json=payload.evidence_json,
        metadata_json=payload.metadata_json,
        db=db,
    )
    if not goal or not review:
        raise HTTPException(status_code=404, detail="strategy_goal_not_found")

    await write_journal(
        db,
        actor=payload.actor,
        action="strategy_goal_reviewed",
        target_type="workspace_strategy_goal",
        target_id=str(goal.id),
        summary=f"Reviewed strategy goal {goal.id} with decision {payload.decision}",
        metadata_json={
            "decision": payload.decision,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "goal": to_strategy_goal_out(goal),
        "review": to_strategy_goal_review_out(review),
    }


@router.get("/strategy/goals/{strategy_goal_id}/reviews")
async def list_strategy_goal_reviews_endpoint(
    strategy_goal_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    goal = await get_strategy_goal(strategy_goal_id=strategy_goal_id, db=db)
    if not goal:
        raise HTTPException(status_code=404, detail="strategy_goal_not_found")
    rows = await list_strategy_goal_reviews(strategy_goal_id=strategy_goal_id, limit=limit, db=db)
    return {
        "reviews": [to_strategy_goal_review_out(item) for item in rows],
    }