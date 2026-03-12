from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_boundary_service import evaluate_adaptive_autonomy_boundaries, get_autonomy_boundary_profile, list_autonomy_boundary_profiles, to_autonomy_boundary_profile_out
from core.db import get_db
from core.journal import write_journal
from core.schemas import AdaptiveAutonomyBoundaryEvaluateRequest

router = APIRouter()


@router.post("/autonomy/boundaries/recompute")
async def recompute_autonomy_boundaries(
    payload: AdaptiveAutonomyBoundaryEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await evaluate_adaptive_autonomy_boundaries(
        actor=payload.actor,
        source=payload.source,
        scope=payload.scope,
        lookback_hours=payload.lookback_hours,
        min_samples=payload.min_samples,
        apply_recommended_boundaries=payload.apply_recommended_boundaries,
        hard_ceiling_overrides=payload.hard_ceiling_overrides,
        evidence_inputs_override=payload.evidence_inputs_override,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="autonomy_boundaries_recomputed",
        target_type="workspace_autonomy_boundary_profile",
        target_id=str(row.id),
        summary=f"Recomputed autonomy boundary {row.id}",
        metadata_json={
            "source": payload.source,
            "scope": payload.scope,
            "lookback_hours": payload.lookback_hours,
            "min_samples": payload.min_samples,
            "apply_recommended_boundaries": payload.apply_recommended_boundaries,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"boundary": to_autonomy_boundary_profile_out(row)}


@router.get("/autonomy/boundaries")
async def list_autonomy_boundaries(
    scope: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_autonomy_boundary_profiles(db=db, status=status, limit=limit)
    if scope.strip():
        requested = scope.strip().lower()
        rows = [row for row in rows if str(row.scope or "").strip().lower() == requested]
    return {"boundaries": [to_autonomy_boundary_profile_out(item) for item in rows]}


@router.get("/autonomy/boundaries/{boundary_id}")
async def get_autonomy_boundary(
    boundary_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_autonomy_boundary_profile(profile_id=boundary_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="autonomy_boundary_not_found")
    return {"boundary": to_autonomy_boundary_profile_out(row)}
