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
from core.models import Actor, MemoryEntry, UserPreference
from core.schemas import ConceptAcknowledgeRequest, ConceptExtractRequest, MemoryCreate

router = APIRouter()


def _memory_out(entry: MemoryEntry) -> dict:
    return {
        "id": entry.id,
        "memory_class": entry.memory_class,
        "content": entry.content,
        "summary": entry.summary,
        "metadata_json": entry.metadata_json,
        "created_at": entry.created_at,
    }


def _actor_out(
    actor: Actor, *, preferences: list[UserPreference], profile: MemoryEntry | None
) -> dict:
    identity = (
        actor.identity_metadata if isinstance(actor.identity_metadata, dict) else {}
    )
    return {
        "actor_name": actor.name,
        "role": actor.role,
        "display_name": str(identity.get("display_name", "")).strip() or actor.name,
        "aliases": [
            str(item).strip()
            for item in identity.get("aliases", [])
            if str(item).strip()
        ],
        "identity_metadata": identity,
        "preferences": [
            {
                "preference_type": row.preference_type,
                "value": row.value,
                "confidence": float(row.confidence),
                "source": row.source,
                "last_updated": row.last_updated,
            }
            for row in preferences
        ],
        "profile_memory": _memory_out(profile) if profile is not None else None,
        "created_at": actor.created_at,
    }


@router.post("")
async def create_memory(
    payload: MemoryCreate, db: AsyncSession = Depends(get_db)
) -> dict:
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
    rows = (
        (await db.execute(select(MemoryEntry).order_by(MemoryEntry.id.desc())))
        .scalars()
        .all()
    )
    return [_memory_out(item) for item in rows]


@router.get("/people")
async def list_people_memory(db: AsyncSession = Depends(get_db)) -> dict:
    actors = (await db.execute(select(Actor).order_by(Actor.id.asc()))).scalars().all()
    preferences = (
        (await db.execute(select(UserPreference).order_by(UserPreference.id.asc())))
        .scalars()
        .all()
    )
    profiles = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "person_profile")
                .order_by(MemoryEntry.id.desc())
            )
        )
        .scalars()
        .all()
    )
    profile_by_actor: dict[str, MemoryEntry] = {}
    for row in profiles:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        actor_name = str(meta.get("actor_name", "")).strip()
        if actor_name and actor_name not in profile_by_actor:
            profile_by_actor[actor_name] = row

    return {
        "people": [
            _actor_out(
                actor,
                preferences=[row for row in preferences if row.user_id == actor.name],
                profile=profile_by_actor.get(actor.name),
            )
            for actor in actors
        ]
    }


@router.get("/people/{actor_name}")
async def get_person_memory(
    actor_name: str, db: AsyncSession = Depends(get_db)
) -> dict:
    actor = (
        (await db.execute(select(Actor).where(Actor.name == actor_name)))
        .scalars()
        .first()
    )
    if actor is None:
        return {"person": None}

    preferences = (
        (
            await db.execute(
                select(UserPreference)
                .where(UserPreference.user_id == actor.name)
                .order_by(UserPreference.id.asc())
            )
        )
        .scalars()
        .all()
    )
    profiles = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "person_profile")
                .order_by(MemoryEntry.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    profile = next(
        (
            row
            for row in profiles
            if str((row.metadata_json or {}).get("actor_name", "")).strip()
            == actor.name
        ),
        None,
    )
    recent_memories = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(
                    MemoryEntry.memory_class.in_(
                        ["person_preference", "conversation_turn"]
                    )
                )
                .order_by(MemoryEntry.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    actor_memories = []
    for row in recent_memories:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("actor_name", "")).strip() != actor.name:
            continue
        actor_memories.append(_memory_out(row))

    return {
        "person": _actor_out(actor, preferences=preferences, profile=profile),
        "recent_memories": actor_memories,
    }


@router.get("/conversations")
async def list_conversation_memory(
    limit: int = 20, db: AsyncSession = Depends(get_db)
) -> dict:
    rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "conversation_session")
                .order_by(MemoryEntry.id.desc())
                .limit(max(1, min(100, int(limit))))
            )
        )
        .scalars()
        .all()
    )
    return {"conversations": [_memory_out(item) for item in rows]}


@router.get("/conversations/{session_id}")
async def get_conversation_memory(
    session_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    summaries = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "conversation_session")
                .order_by(MemoryEntry.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    summary = next(
        (
            row
            for row in summaries
            if str((row.metadata_json or {}).get("session_id", "")).strip()
            == session_id
        ),
        None,
    )

    turns = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "conversation_turn")
                .order_by(MemoryEntry.id.asc())
            )
        )
        .scalars()
        .all()
    )
    session_turns = []
    for row in turns:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("session_id", "")).strip() != session_id:
            continue
        session_turns.append(
            {
                **_memory_out(row),
                "speaker": str(meta.get("speaker", "")).strip(),
                "actor_name": str(meta.get("actor_name", "")).strip(),
                "display_name": str(meta.get("display_name", "")).strip(),
                "conversation_topic": str(meta.get("conversation_topic", "")).strip(),
            }
        )

    return {
        "conversation": _memory_out(summary) if summary is not None else None,
        "turns": session_turns,
    }


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
    rows = await list_concepts(
        db=db, status=status, concept_type=concept_type, limit=limit
    )
    return {
        "concepts": [to_concept_out(item) for item in rows],
    }


@router.get("/concepts/{concept_id}")
async def get_concept_endpoint(
    concept_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
async def get_development_pattern_endpoint(
    pattern_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await get_development_pattern(pattern_id=pattern_id, db=db)
    if not row:
        return {"development_pattern": None}
    return {
        "development_pattern": to_development_pattern_out(row),
    }
