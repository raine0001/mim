from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.concept_memory_service import (
    acknowledge_concept,
    extract_concepts,
    get_concept,
    list_concepts,
    to_concept_out,
)
from core.development_memory_service import (
    extract_development_patterns,
    get_development_pattern,
    list_development_patterns,
    to_development_pattern_out,
)
from core.db import get_db
from core.models import MemoryEntry
from core.schemas import ConceptAcknowledgeRequest, ConceptExtractRequest, MemoryCreate

router = APIRouter()


@router.post("")
async def create_memory(payload: MemoryCreate, db: AsyncSession = Depends(get_db)) -> dict:
    entry = MemoryEntry(
        memory_class=payload.memory_class,
        content=payload.content,
        summary=payload.summary,
        metadata_json=payload.metadata_json,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return {
        "id": entry.id,
        "memory_class": entry.memory_class,
        "content": entry.content,
        "summary": entry.summary,
        "metadata_json": entry.metadata_json,
        "created_at": entry.created_at,
    }


@router.get("")
async def list_memory(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(MemoryEntry).order_by(MemoryEntry.id.desc()))).scalars().all()
    return [
        {
            "id": item.id,
            "memory_class": item.memory_class,
            "content": item.content,
            "summary": item.summary,
            "metadata_json": item.metadata_json,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.post("/concepts/extract")
async def extract_concepts_endpoint(
    payload: ConceptExtractRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await extract_concepts(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        min_evidence_count=payload.min_evidence_count,
        max_concepts=payload.max_concepts,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await db.commit()
    return {
        "concepts_generated": len(rows),
        "concepts": [to_concept_out(item) for item in rows],
    }


@router.get("/concepts")
async def list_concepts_endpoint(
    status: str = "",
    concept_type: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_concepts(db=db, status=status, concept_type=concept_type, limit=limit)
    return {
        "concepts": [to_concept_out(item) for item in rows],
    }


@router.get("/concepts/{concept_id}")
async def get_concept_endpoint(concept_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await get_concept(concept_id=concept_id, db=db)
    if not row:
        return {"concept": None}
    return {
        "concept": to_concept_out(row),
    }


@router.post("/concepts/{concept_id}/acknowledge")
async def acknowledge_concept_endpoint(
    concept_id: int,
    payload: ConceptAcknowledgeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_concept(concept_id=concept_id, db=db)
    if not row:
        return {"updated": False, "concept": None}

    await acknowledge_concept(
        row=row,
        actor=payload.actor,
        reason=payload.reason,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    return {
        "updated": True,
        "concept": to_concept_out(row),
    }


@router.get("/development-patterns")
async def list_development_patterns_endpoint(
    status: str = "",
    pattern_type: str = "",
    limit: int = 50,
    refresh: bool = True,
    lookback_hours: int = 168,
    min_evidence_count: int = 2,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if refresh:
        await extract_development_patterns(
            actor="workspace",
            source="objective53",
            lookback_hours=max(1, min(720, int(lookback_hours))),
            min_evidence_count=max(2, min(500, int(min_evidence_count))),
            max_patterns=max(10, min(500, int(limit) * 5)),
            metadata_json={"refresh": True},
            db=db,
        )
        await db.commit()

    rows = await list_development_patterns(
        db=db,
        status=status,
        pattern_type=pattern_type,
        limit=limit,
    )
    return {
        "development_patterns": [to_development_pattern_out(item) for item in rows],
    }


@router.get("/development-patterns/{pattern_id}")
async def get_development_pattern_endpoint(pattern_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await get_development_pattern(pattern_id=pattern_id, db=db)
    if not row:
        return {"development_pattern": None}
    return {
        "development_pattern": to_development_pattern_out(row),
    }
