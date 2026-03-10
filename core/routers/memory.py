from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import MemoryEntry
from core.schemas import MemoryCreate

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
