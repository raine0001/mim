from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.preferences import DEFAULT_USER_ID, get_user_preference_payload, list_user_preferences, upsert_user_preference
from core.schemas import UserPreferenceOut, UserPreferenceUpsertRequest

router = APIRouter(tags=["preferences"])


@router.get("/preferences")
async def get_preferences(
    user_id: str = DEFAULT_USER_ID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    preferences = await list_user_preferences(db=db, user_id=user_id)
    return {
        "user_id": user_id,
        "preferences": [UserPreferenceOut(**item).model_dump() for item in preferences],
    }


@router.get("/preferences/{preference_type}")
async def get_preference_by_type(
    preference_type: str,
    user_id: str = DEFAULT_USER_ID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    payload = await get_user_preference_payload(db=db, preference_type=preference_type, user_id=user_id)
    return UserPreferenceOut(**payload).model_dump()


@router.post("/preferences")
async def upsert_preference(
    payload: UserPreferenceUpsertRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await upsert_user_preference(
        db=db,
        user_id=payload.user_id,
        preference_type=payload.preference_type,
        value=payload.value,
        confidence=payload.confidence,
        source=payload.source,
    )
    await write_journal(
        db,
        actor=payload.user_id,
        action="user_preference_upsert",
        target_type="user_preference",
        target_id=f"{payload.user_id}:{payload.preference_type}",
        summary=f"Updated preference {payload.preference_type}",
        metadata_json={
            "confidence": float(row.confidence),
            "source": row.source,
        },
    )
    await db.commit()
    await db.refresh(row)
    return UserPreferenceOut(
        user_id=row.user_id,
        preference_type=row.preference_type,
        value=row.value,
        confidence=float(row.confidence),
        source=row.source,
        last_updated=row.last_updated,
        is_default=False,
    ).model_dump()
