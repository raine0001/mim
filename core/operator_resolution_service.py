from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceOperatorResolutionCommitment


ACTIVE_COMMITMENT_STATUSES = {"active"}


def scope_value(raw: object) -> str:
    return str(raw or "").strip()


def normalize_scope(raw: object) -> str:
    value = scope_value(raw)
    return value or "global"


def commitment_is_expired(
    row: WorkspaceOperatorResolutionCommitment,
    *,
    now: datetime | None = None,
) -> bool:
    expiry = row.expires_at
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc) <= (now or datetime.now(timezone.utc))


def sync_commitment_expiration(
    row: WorkspaceOperatorResolutionCommitment,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    if str(row.status or "").strip() in ACTIVE_COMMITMENT_STATUSES and commitment_is_expired(
        row,
        now=current,
    ):
        row.status = "expired"
        return True
    return False


def commitment_is_active(
    row: WorkspaceOperatorResolutionCommitment | None,
    *,
    now: datetime | None = None,
) -> bool:
    if row is None:
        return False
    if str(row.status or "").strip() not in ACTIVE_COMMITMENT_STATUSES:
        return False
    return not commitment_is_expired(row, now=now)


def commitment_downstream_effects(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> dict:
    if row is None:
        return {}
    return row.downstream_effects_json if isinstance(row.downstream_effects_json, dict) else {}


def commitment_snapshot(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> dict:
    if row is None:
        return {}
    active = commitment_is_active(row)
    expired = str(row.status or "").strip() == "expired" or commitment_is_expired(row)
    return {
        "commitment_id": int(row.id),
        "managed_scope": scope_value(row.managed_scope),
        "commitment_family": scope_value(row.commitment_family),
        "decision_type": scope_value(row.decision_type),
        "status": scope_value(row.status),
        "reason": str(row.reason or "").strip(),
        "authority_level": scope_value(row.authority_level),
        "confidence": round(float(row.confidence or 0.0), 6),
        "expires_at": (
            row.expires_at.isoformat()
            if getattr(row, "expires_at", None) is not None
            else None
        ),
        "superseded_by_commitment_id": row.superseded_by_commitment_id,
        "downstream_effects": commitment_downstream_effects(row),
        "active": active,
        "expired": expired,
        "effect_active": active and bool(commitment_downstream_effects(row)),
    }


def choose_operator_resolution_commitment(
    rows: list[WorkspaceOperatorResolutionCommitment],
    *,
    scope: str,
) -> WorkspaceOperatorResolutionCommitment | None:
    normalized_scope = scope_value(scope)
    if not rows:
        return None
    if normalized_scope:
        scoped = [row for row in rows if scope_value(row.managed_scope) == normalized_scope]
        active_scoped = [row for row in scoped if commitment_is_active(row)]
        if active_scoped:
            return active_scoped[0]
        if scoped:
            return scoped[0]
    active_rows = [row for row in rows if commitment_is_active(row)]
    if active_rows:
        return active_rows[0]
    return rows[0]


def commitment_matches_filters(
    row: WorkspaceOperatorResolutionCommitment,
    *,
    families: Iterable[str] | None = None,
    decision_types: Iterable[str] | None = None,
    require_downstream_effects: Iterable[str] | None = None,
) -> bool:
    family_set = {scope_value(item).lower() for item in (families or []) if scope_value(item)}
    if family_set and scope_value(row.commitment_family).lower() not in family_set:
        return False
    decision_set = {scope_value(item) for item in (decision_types or []) if scope_value(item)}
    if decision_set and scope_value(row.decision_type) not in decision_set:
        return False
    effects = commitment_downstream_effects(row)
    required_effects = [scope_value(item) for item in (require_downstream_effects or []) if scope_value(item)]
    if required_effects and not all(bool(effects.get(key, False)) for key in required_effects):
        return False
    return True


async def list_recent_operator_resolution_commitments(
    *,
    db: AsyncSession,
    limit: int = 40,
) -> list[WorkspaceOperatorResolutionCommitment]:
    return (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitment)
                .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )


async def latest_active_operator_resolution_commitment(
    *,
    scope: str,
    db: AsyncSession,
    families: Iterable[str] | None = None,
    decision_types: Iterable[str] | None = None,
    require_downstream_effects: Iterable[str] | None = None,
    limit: int = 20,
) -> WorkspaceOperatorResolutionCommitment | None:
    normalized_scope = normalize_scope(scope)
    rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitment)
                .where(WorkspaceOperatorResolutionCommitment.managed_scope == normalized_scope)
                .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )
    current = datetime.now(timezone.utc)
    for row in rows:
        if sync_commitment_expiration(row, now=current):
            continue
        if not commitment_is_active(row, now=current):
            continue
        if not commitment_matches_filters(
            row,
            families=families,
            decision_types=decision_types,
            require_downstream_effects=require_downstream_effects,
        ):
            continue
        return row
    return None


def commitment_requested_autonomy_level(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> str:
    if row is None:
        return ""
    downstream_effects = commitment_downstream_effects(row)
    requested_level = scope_value(downstream_effects.get("autonomy_level"))
    decision_type = scope_value(row.decision_type)
    if not requested_level and decision_type == "lower_autonomy_for_scope":
        requested_level = "operator_required"
    if not requested_level and decision_type in {"require_additional_evidence", "defer_action"}:
        requested_level = scope_value(downstream_effects.get("autonomy_level_cap")) or "operator_required"
    return requested_level


def commitment_effect_labels(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> list[str]:
    if row is None:
        return []
    effects = commitment_downstream_effects(row)
    labels: list[str] = []
    if bool(effects.get("suppress_duplicate_inquiry", False)):
        labels.append("inquiry_suppression")
    if scope_value(effects.get("autonomy_level")) or scope_value(effects.get("autonomy_level_cap")):
        labels.append("autonomy_posture")
    if scope_value(effects.get("maintenance_mode")):
        labels.append("maintenance_shaping")
    if bool(effects.get("stewardship_defer_actions", False)) or scope_value(effects.get("stewardship_mode")):
        labels.append("stewardship_shaping")
    if bool(effects.get("strategy_priority_delta", 0.0)) or scope_value(effects.get("strategy_priority_mode")):
        labels.append("strategy_scoring")
    if not labels and commitment_downstream_effects(row):
        labels.append("downstream_effect")
    return labels