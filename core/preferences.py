from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import UserPreference

DEFAULT_USER_ID = "operator"

DEFAULT_PREFERENCES: dict[str, dict] = {
    "preferred_confirmation_threshold": {
        "value": 0.9,
        "confidence": 0.8,
        "source": "default",
    },
    "preferred_scan_zones": {
        "value": [],
        "confidence": 0.5,
        "source": "default",
    },
    "auto_exec_tolerance": {
        "value": 0.5,
        "confidence": 0.5,
        "source": "default",
    },
    "auto_exec_safe_tasks": {
        "value": False,
        "confidence": 0.5,
        "source": "default",
    },
    "notification_verbosity": {
        "value": "normal",
        "confidence": 0.8,
        "source": "default",
    },
    "action_approval_bias": {
        "value": {"approvals": 0, "rejections": 0, "overrides": 0},
        "confidence": 0.2,
        "source": "learning",
    },
    "collaboration_negotiation_patterns": {
        "value": {"version": "objective66-v1", "patterns": {}},
        "confidence": 0.0,
        "source": "learning",
    },
    "collaboration_negotiation_memory": {
        "value": {"version": "objective68-v1", "patterns": {}},
        "confidence": 0.0,
        "source": "learning",
    },
}


def _default_for(preference_type: str) -> dict:
    return DEFAULT_PREFERENCES.get(
        preference_type,
        {
            "value": None,
            "confidence": 0.0,
            "source": "unknown",
        },
    )


async def get_user_preference_row(
    *,
    db: AsyncSession,
    preference_type: str,
    user_id: str = DEFAULT_USER_ID,
) -> UserPreference | None:
    return (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .where(UserPreference.preference_type == preference_type)
        )
    ).scalars().first()


async def get_user_preference_value(
    *,
    db: AsyncSession,
    preference_type: str,
    user_id: str = DEFAULT_USER_ID,
):
    row = await get_user_preference_row(db=db, preference_type=preference_type, user_id=user_id)
    if row:
        return row.value
    return _default_for(preference_type).get("value")


async def get_user_preference_payload(
    *,
    db: AsyncSession,
    preference_type: str,
    user_id: str = DEFAULT_USER_ID,
) -> dict:
    row = await get_user_preference_row(db=db, preference_type=preference_type, user_id=user_id)
    if row:
        return {
            "user_id": row.user_id,
            "preference_type": row.preference_type,
            "value": row.value,
            "confidence": float(row.confidence),
            "source": row.source,
            "last_updated": row.last_updated,
            "is_default": False,
        }
    default = _default_for(preference_type)
    return {
        "user_id": user_id,
        "preference_type": preference_type,
        "value": default.get("value"),
        "confidence": float(default.get("confidence", 0.0)),
        "source": str(default.get("source", "default")),
        "last_updated": None,
        "is_default": True,
    }


async def list_user_preferences(
    *,
    db: AsyncSession,
    user_id: str = DEFAULT_USER_ID,
) -> list[dict]:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == user_id)
            .order_by(UserPreference.preference_type.asc())
        )
    ).scalars().all()

    seen = {row.preference_type for row in rows}
    payload = [
        {
            "user_id": row.user_id,
            "preference_type": row.preference_type,
            "value": row.value,
            "confidence": float(row.confidence),
            "source": row.source,
            "last_updated": row.last_updated,
            "is_default": False,
        }
        for row in rows
    ]

    for preference_type in sorted(DEFAULT_PREFERENCES.keys()):
        if preference_type in seen:
            continue
        default = _default_for(preference_type)
        payload.append(
            {
                "user_id": user_id,
                "preference_type": preference_type,
                "value": default.get("value"),
                "confidence": float(default.get("confidence", 0.0)),
                "source": str(default.get("source", "default")),
                "last_updated": None,
                "is_default": True,
            }
        )

    payload.sort(key=lambda item: str(item.get("preference_type", "")))
    return payload


async def upsert_user_preference(
    *,
    db: AsyncSession,
    preference_type: str,
    value,
    confidence: float,
    source: str,
    user_id: str = DEFAULT_USER_ID,
) -> UserPreference:
    row = await get_user_preference_row(db=db, preference_type=preference_type, user_id=user_id)
    now = datetime.now(timezone.utc)
    bounded_confidence = max(0.0, min(1.0, float(confidence)))

    if row is None:
        row = UserPreference(
            user_id=user_id,
            preference_type=preference_type,
            value=value,
            confidence=bounded_confidence,
            source=source,
            last_updated=now,
        )
        db.add(row)
        await db.flush()
        return row

    row.value = value
    row.confidence = bounded_confidence
    row.source = source
    row.last_updated = now
    await db.flush()
    return row


async def apply_learning_signal(
    *,
    db: AsyncSession,
    signal: str,
    user_id: str = DEFAULT_USER_ID,
) -> None:
    bias_payload = await get_user_preference_payload(db=db, preference_type="action_approval_bias", user_id=user_id)
    bias_value = bias_payload.get("value", {}) if isinstance(bias_payload.get("value", {}), dict) else {}
    approvals = int(bias_value.get("approvals", 0))
    rejections = int(bias_value.get("rejections", 0))
    overrides = int(bias_value.get("overrides", 0))

    if signal == "proposal_accept" or signal == "operator_approve":
        approvals += 1
    elif signal == "proposal_reject" or signal == "operator_reject":
        rejections += 1
    elif signal == "policy_override":
        overrides += 1

    total = approvals + rejections + overrides
    bias_confidence = min(1.0, 0.2 + (total / 30.0))

    await upsert_user_preference(
        db=db,
        preference_type="action_approval_bias",
        value={
            "approvals": approvals,
            "rejections": rejections,
            "overrides": overrides,
        },
        confidence=bias_confidence,
        source="learning",
        user_id=user_id,
    )

    tolerance_payload = await get_user_preference_payload(db=db, preference_type="auto_exec_tolerance", user_id=user_id)
    current_tolerance = float(tolerance_payload.get("value", 0.5) or 0.5)
    drift = ((approvals - rejections) * 0.01) - (overrides * 0.005)
    updated_tolerance = max(0.0, min(1.0, current_tolerance + drift))
    await upsert_user_preference(
        db=db,
        preference_type="auto_exec_tolerance",
        value=round(updated_tolerance, 3),
        confidence=min(1.0, 0.25 + (total / 35.0)),
        source="learning",
        user_id=user_id,
    )

    await upsert_user_preference(
        db=db,
        preference_type="auto_exec_safe_tasks",
        value=updated_tolerance >= 0.65,
        confidence=min(1.0, 0.25 + (total / 35.0)),
        source="learning",
        user_id=user_id,
    )
