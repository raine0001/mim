from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.journal import write_journal
from core.models import WorkspaceOperatorResolutionCommitment


ACTIVE_COMMITMENT_STATUSES = {"active"}
RECOVERY_COMMITMENT_POLICY_SOURCE = "execution_recovery_commitment"
GENERIC_COMMITMENT_POLICY_SOURCE = "operator_commitment"


def scope_value(raw: object) -> str:
    return str(raw or "").strip()


def normalize_scope(raw: object) -> str:
    value = scope_value(raw)
    return value or "global"


def scope_hierarchy(scope: object) -> list[str]:
    normalized = normalize_scope(scope)
    if normalized == "global":
        return ["global"]
    segments = [segment for segment in normalized.split("/") if segment]
    if not segments:
        return ["global"]
    hierarchy: list[str] = []
    for length in range(len(segments), 0, -1):
        candidate = "/".join(segments[:length])
        if candidate and candidate not in hierarchy:
            hierarchy.append(candidate)
    hierarchy.append("global")
    return hierarchy


def scope_match_kind(*, commitment_scope: object, requested_scope: object) -> str:
    commitment_value = normalize_scope(commitment_scope)
    requested_value = normalize_scope(requested_scope)
    if commitment_value == requested_value:
        return "exact"
    if commitment_value == "global":
        return "global"
    if requested_value.startswith(f"{commitment_value}/"):
        return "inherited"
    return "unmatched"


def scope_match_distance(*, commitment_scope: object, requested_scope: object) -> int:
    match_kind = scope_match_kind(
        commitment_scope=commitment_scope,
        requested_scope=requested_scope,
    )
    if match_kind == "exact":
        return 0
    if match_kind == "global":
        return 999
    if match_kind != "inherited":
        return 10_000
    commitment_segments = [segment for segment in normalize_scope(commitment_scope).split("/") if segment]
    requested_segments = [segment for segment in normalize_scope(requested_scope).split("/") if segment]
    return max(0, len(requested_segments) - len(commitment_segments))


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


def commitment_is_recovery_policy_tuning_derived(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> bool:
    if row is None:
        return False
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    recommendation = (
        row.recommendation_snapshot_json
        if isinstance(row.recommendation_snapshot_json, dict)
        else {}
    )
    provenance = row.provenance_json if isinstance(row.provenance_json, dict) else {}
    downstream_effects = commitment_downstream_effects(row)
    if bool(metadata.get("objective121_recovery_policy_commitment", False)):
        return True
    if str(recommendation.get("source") or "").strip() == "execution_recovery_policy_tuning":
        return True
    if str(provenance.get("policy_action") or "").strip():
        return True
    return bool(str(downstream_effects.get("recovery_policy_action") or "").strip())


def commitment_policy_source(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> str:
    if commitment_is_recovery_policy_tuning_derived(row):
        return RECOVERY_COMMITMENT_POLICY_SOURCE
    return GENERIC_COMMITMENT_POLICY_SOURCE


def commitment_manual_reset(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> bool:
    if row is None:
        return False
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return bool(metadata.get("manual_reset", False))


def commitment_reapplication_source_id(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> int | None:
    if row is None:
        return None
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    provenance = row.provenance_json if isinstance(row.provenance_json, dict) else {}
    raw_value = metadata.get("reapplied_from_commitment_id") or provenance.get("reapplied_from_commitment_id")
    try:
        parsed = int(raw_value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def commitment_scope_application(
    row: WorkspaceOperatorResolutionCommitment | None,
    *,
    requested_scope: object,
) -> dict:
    if row is None:
        return {
            "match_type": "none",
            "distance": None,
            "applies": False,
            "commitment_scope": "",
            "requested_scope": normalize_scope(requested_scope),
        }
    commitment_scope = normalize_scope(row.managed_scope)
    requested = normalize_scope(requested_scope)
    match_type = scope_match_kind(
        commitment_scope=commitment_scope,
        requested_scope=requested,
    )
    return {
        "match_type": match_type,
        "distance": (
            None if match_type == "unmatched" else scope_match_distance(
                commitment_scope=commitment_scope,
                requested_scope=requested,
            )
        ),
        "applies": match_type != "unmatched",
        "commitment_scope": commitment_scope,
        "requested_scope": requested,
    }


def recovery_commitment_lifecycle_state(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> str:
    if row is None:
        return "inactive"
    status = scope_value(row.status)
    if commitment_manual_reset(row):
        return "manually_reset"
    if commitment_reapplication_source_id(row):
        return "reapplied" if status == "active" else f"reapplied_{status or 'inactive'}"
    if commitment_is_expired(row):
        return "expired"
    return status or "inactive"


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
        "scope_application": commitment_scope_application(row, requested_scope=row.managed_scope),
        "commitment_family": scope_value(row.commitment_family),
        "decision_type": scope_value(row.decision_type),
        "status": scope_value(row.status),
        "lifecycle_state": recovery_commitment_lifecycle_state(row),
        "policy_source": commitment_policy_source(row),
        "reason": str(row.reason or "").strip(),
        "authority_level": scope_value(row.authority_level),
        "confidence": round(float(row.confidence or 0.0), 6),
        "expires_at": (
            row.expires_at.isoformat()
            if getattr(row, "expires_at", None) is not None
            else None
        ),
        "superseded_by_commitment_id": row.superseded_by_commitment_id,
        "reapplied_from_commitment_id": commitment_reapplication_source_id(row),
        "manual_reset": commitment_manual_reset(row),
        "downstream_effects": commitment_downstream_effects(row),
        "active": active,
        "expired": expired,
        "effect_active": active and bool(commitment_downstream_effects(row)),
    }


def normalize_commitment_family(*, commitment_family: str, decision_type: str) -> str:
    normalized = str(commitment_family or "").strip().lower()
    if normalized:
        return normalized
    decision = str(decision_type or "").strip().lower()
    family_map = {
        "approve_current_path": "path_disposition",
        "override_recommendation": "path_disposition",
        "defer_action": "action_timing",
        "require_additional_evidence": "evidence_gate",
        "lower_autonomy_for_scope": "autonomy_posture",
        "elevate_remediation_priority": "remediation_priority",
    }
    return family_map.get(decision, decision or "general")


def operator_resolution_commitment_out(
    row: WorkspaceOperatorResolutionCommitment,
) -> dict:
    snapshot = commitment_snapshot(row)
    return {
        **snapshot,
        "source": row.source,
        "created_by": row.created_by,
        "reason": row.reason,
        "recommendation_snapshot_json": (
            row.recommendation_snapshot_json
            if isinstance(row.recommendation_snapshot_json, dict)
            else {}
        ),
        "provenance_json": row.provenance_json if isinstance(row.provenance_json, dict) else {},
        "downstream_effects_json": (
            row.downstream_effects_json if isinstance(row.downstream_effects_json, dict) else {}
        ),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


async def create_operator_resolution_commitment_record(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    decision_type: str,
    reason: str,
    recommendation_snapshot_json: dict | None,
    authority_level: str,
    confidence: float,
    provenance_json: dict | None,
    downstream_effects_json: dict | None,
    metadata_json: dict | None,
    expires_at: datetime | None,
    db: AsyncSession,
) -> dict:
    scope = normalize_scope(managed_scope)
    normalized_decision = scope_value(decision_type)
    normalized_reason = str(reason or "").strip()
    normalized_authority = scope_value(authority_level) or "governance_override"
    recommendation_snapshot = (
        recommendation_snapshot_json if isinstance(recommendation_snapshot_json, dict) else {}
    )
    provenance = provenance_json if isinstance(provenance_json, dict) else {}
    downstream_effects = downstream_effects_json if isinstance(downstream_effects_json, dict) else {}
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    commitment_family = normalize_commitment_family(
        commitment_family=str(metadata.get("commitment_family") or "").strip(),
        decision_type=normalized_decision,
    )

    active_rows = (
        await db.execute(
            select(WorkspaceOperatorResolutionCommitment)
            .where(WorkspaceOperatorResolutionCommitment.managed_scope == scope)
            .where(WorkspaceOperatorResolutionCommitment.commitment_family == commitment_family)
            .where(WorkspaceOperatorResolutionCommitment.status == "active")
            .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
        )
    ).scalars().all()

    current = datetime.now(timezone.utc)
    filtered_active_rows: list[WorkspaceOperatorResolutionCommitment] = []
    for existing in active_rows:
        if sync_commitment_expiration(existing, now=current):
            continue
        if commitment_is_active(existing, now=current):
            filtered_active_rows.append(existing)

    for existing in filtered_active_rows:
        if (
            str(existing.decision_type or "") == normalized_decision
            and str(existing.reason or "") == normalized_reason
            and (
                existing.recommendation_snapshot_json
                if isinstance(existing.recommendation_snapshot_json, dict)
                else {}
            )
            == recommendation_snapshot
            and str(existing.authority_level or "") == normalized_authority
        ):
            return {
                "commitment": operator_resolution_commitment_out(existing),
                "duplicate_suppressed": True,
                "superseded_commitment_ids": [],
            }

    row = WorkspaceOperatorResolutionCommitment(
        source=str(source or "objective85").strip() or "objective85",
        created_by=str(actor or "operator").strip() or "operator",
        managed_scope=scope,
        commitment_family=commitment_family,
        decision_type=normalized_decision,
        status="active",
        reason=normalized_reason,
        recommendation_snapshot_json=recommendation_snapshot,
        authority_level=normalized_authority,
        confidence=float(confidence or 0.0),
        provenance_json=provenance,
        expires_at=expires_at,
        downstream_effects_json=downstream_effects,
        metadata_json=metadata,
    )
    db.add(row)
    await db.flush()

    superseded_ids: list[int] = []
    for existing in filtered_active_rows:
        existing.status = "superseded"
        existing.superseded_by_commitment_id = row.id
        superseded_ids.append(int(existing.id))

    await write_journal(
        db,
        actor=str(actor or "operator").strip() or "operator",
        action="operator_resolution_commitment_created",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(row.id),
        summary=f"Created operator resolution commitment {row.id} for {row.managed_scope}",
        metadata_json={
            "managed_scope": row.managed_scope,
            "decision_type": row.decision_type,
            "commitment_family": row.commitment_family,
            "superseded_commitment_ids": superseded_ids,
            **metadata,
        },
    )
    return {
        "commitment": operator_resolution_commitment_out(row),
        "duplicate_suppressed": False,
        "superseded_commitment_ids": superseded_ids,
    }


def choose_operator_resolution_commitment(
    rows: list[WorkspaceOperatorResolutionCommitment],
    *,
    scope: str,
) -> WorkspaceOperatorResolutionCommitment | None:
    normalized_scope = normalize_scope(scope)
    if not rows:
        return None
    if normalized_scope:
        for candidate_scope in scope_hierarchy(normalized_scope):
            scoped = [
                row for row in rows if normalize_scope(row.managed_scope) == candidate_scope
            ]
            active_scoped = [row for row in scoped if commitment_is_active(row)]
            if active_scoped:
                return active_scoped[0]
            if scoped:
                return scoped[0]
    active_rows = [row for row in rows if commitment_is_active(row)]
    if active_rows:
        return active_rows[0]
    return rows[0]


async def latest_resolution_commitment_for_scope(
    *,
    scope: str,
    db: AsyncSession,
    families: Iterable[str] | None = None,
    decision_types: Iterable[str] | None = None,
    require_downstream_effects: Iterable[str] | None = None,
    include_inherited: bool = False,
    require_active: bool = True,
    matcher=None,
    limit: int = 40,
) -> WorkspaceOperatorResolutionCommitment | None:
    scopes = [normalize_scope(scope)]
    if include_inherited:
        scopes = scope_hierarchy(scope)
    rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitment)
                .where(WorkspaceOperatorResolutionCommitment.managed_scope.in_(scopes))
                .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )
    current = datetime.now(timezone.utc)
    filtered: list[WorkspaceOperatorResolutionCommitment] = []
    for row in rows:
        if sync_commitment_expiration(row, now=current):
            continue
        if require_active and not commitment_is_active(row, now=current):
            continue
        if not commitment_matches_filters(
            row,
            families=families,
            decision_types=decision_types,
            require_downstream_effects=require_downstream_effects,
        ):
            continue
        if matcher is not None and not matcher(row):
            continue
        filtered.append(row)
    if not filtered:
        return None
    if include_inherited:
        filtered.sort(
            key=lambda row: (
                scope_match_distance(
                    commitment_scope=row.managed_scope,
                    requested_scope=scope,
                ),
                -int(row.id),
            )
        )
    return choose_operator_resolution_commitment(filtered, scope=scope)


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
    include_inherited: bool = False,
    limit: int = 20,
) -> WorkspaceOperatorResolutionCommitment | None:
    return await latest_resolution_commitment_for_scope(
        scope=scope,
        db=db,
        families=families,
        decision_types=decision_types,
        require_downstream_effects=require_downstream_effects,
        include_inherited=include_inherited,
        require_active=True,
        limit=limit,
    )


async def latest_recovery_policy_commitment(
    *,
    scope: str,
    db: AsyncSession,
    include_inherited: bool = False,
    require_active: bool = True,
    limit: int = 40,
) -> WorkspaceOperatorResolutionCommitment | None:
    return await latest_resolution_commitment_for_scope(
        scope=scope,
        db=db,
        include_inherited=include_inherited,
        require_active=require_active,
        matcher=commitment_is_recovery_policy_tuning_derived,
        limit=limit,
    )


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