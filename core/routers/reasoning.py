from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import (
    build_cross_domain_reasoning_context,
    get_cross_domain_reasoning_context,
    list_cross_domain_reasoning_contexts,
    to_cross_domain_reasoning_out,
)
from core.db import get_db
from core.journal import write_journal
from core.schemas import CrossDomainReasoningBuildRequest

router = APIRouter()


@router.post("/reasoning/context/build")
async def build_cross_domain_reasoning_context_endpoint(
    payload: CrossDomainReasoningBuildRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await build_cross_domain_reasoning_context(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        max_items_per_domain=payload.max_items_per_domain,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="cross_domain_reasoning_context_built",
        target_type="workspace_cross_domain_reasoning_context",
        target_id=str(row.id),
        summary=f"Built cross-domain reasoning context {row.id}",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "max_items_per_domain": payload.max_items_per_domain,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "context": to_cross_domain_reasoning_out(row),
    }


@router.get("/reasoning/context")
async def list_cross_domain_reasoning_contexts_endpoint(
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_cross_domain_reasoning_contexts(db=db, status=status, limit=limit)
    return {
        "contexts": [to_cross_domain_reasoning_out(item) for item in rows],
    }


@router.get("/reasoning/context/{context_id}")
async def get_cross_domain_reasoning_context_endpoint(
    context_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_cross_domain_reasoning_context(context_id=context_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="cross_domain_reasoning_context_not_found")
    return {
        "context": to_cross_domain_reasoning_out(row),
    }
