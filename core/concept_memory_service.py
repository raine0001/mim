from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    CapabilityExecution,
    WorkspaceConceptMemory,
    WorkspaceEnvironmentStrategy,
    WorkspaceImprovementProposal,
    WorkspaceInterruptionEvent,
    WorkspaceObservation,
)


CONCEPT_STATUSES = {"active", "acknowledged", "superseded"}


def _concept_confidence(evidence_count: int) -> float:
    return max(0.0, min(0.99, 0.4 + (0.06 * float(max(0, evidence_count)))))


def _zone_from_scan_row(row: CapabilityExecution) -> str:
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
    args = row.arguments_json if isinstance(row.arguments_json, dict) else {}
    observations = feedback.get("observations") if isinstance(feedback.get("observations"), list) else []
    if observations:
        for item in observations:
            if not isinstance(item, dict):
                continue
            zone = str(item.get("zone", "")).strip()
            if zone:
                return zone
    return str(args.get("scan_area") or "workspace").strip() or "workspace"


async def _rescan_success_candidates(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.created_at >= since)
            .where(CapabilityExecution.capability_name == "workspace_scan")
            .where(CapabilityExecution.status == "succeeded")
            .order_by(CapabilityExecution.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        zone = _zone_from_scan_row(row)
        counts[zone] = counts.get(zone, 0) + 1

    candidates: list[dict] = []
    for zone, count in counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "concept_type": "rescan_success_zone_pattern",
                "trigger_pattern": "repeated_rescan_success",
                "evidence_count": count,
                "affected_zones": [zone],
                "affected_objects": [],
                "affected_strategies": ["stabilize_zone", "preemptive_zone_stabilization"],
                "suggested_implications": [
                    f"Prioritize stabilize_zone strategy for zone {zone}",
                    "Increase planning confidence for refresh goals in this zone",
                ],
                "evidence_summary": f"Workspace scan succeeded {count} times in zone '{zone}' during lookback window",
                "metadata_json": {"pattern_scope": zone},
            }
        )
    return candidates


async def _interruption_candidates(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceInterruptionEvent)
            .where(WorkspaceInterruptionEvent.created_at >= since)
            .order_by(WorkspaceInterruptionEvent.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        zone = str(metadata.get("target_zone") or metadata.get("zone") or "workspace").strip() or "workspace"
        key = f"{row.interruption_type}:{zone}"
        counts[key] = counts.get(key, 0) + 1

    candidates: list[dict] = []
    for key, count in counts.items():
        if count < min_evidence_count:
            continue
        interruption_type, zone = key.split(":", 1)
        candidates.append(
            {
                "concept_type": "interruption_risk_path_pattern",
                "trigger_pattern": f"repeated_interruption:{interruption_type}",
                "evidence_count": count,
                "affected_zones": [zone],
                "affected_objects": [],
                "affected_strategies": ["restore_map_stability"],
                "suggested_implications": [
                    "Increase caution and pre-checks for affected zone paths",
                ],
                "evidence_summary": f"Interruption type '{interruption_type}' repeated {count} times near zone '{zone}'",
                "metadata_json": {"pattern_scope": zone, "interruption_type": interruption_type},
            }
        )
    return candidates


async def _low_value_proposal_candidates(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceImprovementProposal)
            .where(WorkspaceImprovementProposal.created_at >= since)
            .order_by(WorkspaceImprovementProposal.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        low_value = str(row.status or "") == "rejected" or float(row.confidence or 0.0) < 0.45
        if not low_value:
            continue
        ptype = str(row.proposal_type or "unknown")
        counts[ptype] = counts.get(ptype, 0) + 1

    candidates: list[dict] = []
    for proposal_type, count in counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "concept_type": "low_value_proposal_pattern",
                "trigger_pattern": f"repeated_low_value:{proposal_type}",
                "evidence_count": count,
                "affected_zones": [],
                "affected_objects": [],
                "affected_strategies": ["proposal_priority_adjustment"],
                "suggested_implications": [
                    f"Lower priority or tighten generation rules for proposal type '{proposal_type}'",
                ],
                "evidence_summary": f"Detected {count} low-value/rejected '{proposal_type}' proposals",
                "metadata_json": {"proposal_type": proposal_type},
            }
        )
    return candidates


async def _recovery_strategy_candidates(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceEnvironmentStrategy)
            .where(WorkspaceEnvironmentStrategy.created_at >= since)
            .where(WorkspaceEnvironmentStrategy.current_status == "stable")
            .order_by(WorkspaceEnvironmentStrategy.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row.strategy_type}:{row.target_scope}"
        counts[key] = counts.get(key, 0) + 1

    candidates: list[dict] = []
    for key, count in counts.items():
        if count < min_evidence_count:
            continue
        strategy_type, scope = key.split(":", 1)
        candidates.append(
            {
                "concept_type": "recovery_strategy_success_pattern",
                "trigger_pattern": f"repeated_stable_strategy:{strategy_type}",
                "evidence_count": count,
                "affected_zones": [scope],
                "affected_objects": [],
                "affected_strategies": [strategy_type],
                "suggested_implications": [
                    f"Increase influence weight for strategy '{strategy_type}' in scope '{scope}'",
                ],
                "evidence_summary": f"Strategy '{strategy_type}' reached stable state {count} times in scope '{scope}'",
                "metadata_json": {"strategy_type": strategy_type, "pattern_scope": scope},
            }
        )
    return candidates


async def _object_drift_candidates(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.created_at >= since)
            .order_by(WorkspaceObservation.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    label_zones: dict[str, set[str]] = {}
    label_counts: dict[str, int] = {}
    for row in rows:
        label = str(row.label or "").strip()
        zone = str(row.zone or "").strip()
        if not label or not zone:
            continue
        label_zones.setdefault(label, set()).add(zone)
        label_counts[label] = label_counts.get(label, 0) + 1

    candidates: list[dict] = []
    for label, zones in label_zones.items():
        count = label_counts.get(label, 0)
        if count < min_evidence_count or len(zones) < 2:
            continue
        zone_list = sorted(list(zones))
        candidates.append(
            {
                "concept_type": "object_drift_adjacent_zone_pattern",
                "trigger_pattern": "repeated_object_zone_drift",
                "evidence_count": count,
                "affected_zones": zone_list,
                "affected_objects": [label],
                "affected_strategies": ["refresh_object_certainty", "restore_map_stability"],
                "suggested_implications": [
                    f"Object '{label}' frequently changes zones; prioritize identity refresh before directed actions",
                ],
                "evidence_summary": f"Object label '{label}' observed {count} times across zones {zone_list}",
                "metadata_json": {"object_label": label},
            }
        )
    return candidates


async def _find_existing_concept(
    *,
    concept_type: str,
    trigger_pattern: str,
    affected_zones: list[str],
    db: AsyncSession,
) -> WorkspaceConceptMemory | None:
    rows = (
        await db.execute(
            select(WorkspaceConceptMemory)
            .where(WorkspaceConceptMemory.concept_type == concept_type)
            .where(WorkspaceConceptMemory.trigger_pattern == trigger_pattern)
            .where(WorkspaceConceptMemory.status.in_(["active", "acknowledged"]))
            .order_by(WorkspaceConceptMemory.id.desc())
            .limit(200)
        )
    ).scalars().all()
    zone_set = {str(item).strip() for item in affected_zones if str(item).strip()}
    for row in rows:
        row_zones = set(row.affected_zones_json if isinstance(row.affected_zones_json, list) else [])
        if row_zones == zone_set:
            return row
    return None


async def extract_concepts(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_evidence_count: int,
    max_concepts: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceConceptMemory]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    threshold = max(2, int(min_evidence_count))

    candidates: list[dict] = []
    candidates.extend(await _rescan_success_candidates(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _interruption_candidates(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _low_value_proposal_candidates(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _recovery_strategy_candidates(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _object_drift_candidates(since=since, min_evidence_count=threshold, db=db))

    changed: list[WorkspaceConceptMemory] = []
    for candidate in sorted(candidates, key=lambda item: int(item.get("evidence_count", 0)), reverse=True):
        if len(changed) >= max(1, min(100, int(max_concepts))):
            break

        concept_type = str(candidate.get("concept_type", "pattern")).strip() or "pattern"
        trigger_pattern = str(candidate.get("trigger_pattern", "repeated_pattern")).strip() or "repeated_pattern"
        affected_zones = candidate.get("affected_zones", []) if isinstance(candidate.get("affected_zones", []), list) else []
        evidence_count = max(0, int(candidate.get("evidence_count", 0)))

        existing = await _find_existing_concept(
            concept_type=concept_type,
            trigger_pattern=trigger_pattern,
            affected_zones=affected_zones,
            db=db,
        )
        if existing:
            existing.evidence_count = int(existing.evidence_count or 0) + evidence_count
            existing.confidence = _concept_confidence(int(existing.evidence_count or 0))
            existing.evidence_summary = str(candidate.get("evidence_summary", existing.evidence_summary))
            existing.affected_objects_json = candidate.get("affected_objects", []) if isinstance(candidate.get("affected_objects", []), list) else []
            existing.affected_strategies_json = candidate.get("affected_strategies", []) if isinstance(candidate.get("affected_strategies", []), list) else []
            existing.suggested_implications_json = candidate.get("suggested_implications", []) if isinstance(candidate.get("suggested_implications", []), list) else []
            existing.metadata_json = {
                **(existing.metadata_json if isinstance(existing.metadata_json, dict) else {}),
                **(candidate.get("metadata_json", {}) if isinstance(candidate.get("metadata_json", {}), dict) else {}),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "last_extracted_at": datetime.now(timezone.utc).isoformat(),
            }
            changed.append(existing)
            continue

        row = WorkspaceConceptMemory(
            source=source,
            actor=actor,
            concept_type=concept_type,
            trigger_pattern=trigger_pattern,
            evidence_count=evidence_count,
            confidence=_concept_confidence(evidence_count),
            affected_zones_json=affected_zones,
            affected_objects_json=candidate.get("affected_objects", []) if isinstance(candidate.get("affected_objects", []), list) else [],
            affected_strategies_json=candidate.get("affected_strategies", []) if isinstance(candidate.get("affected_strategies", []), list) else [],
            suggested_implications_json=candidate.get("suggested_implications", []) if isinstance(candidate.get("suggested_implications", []), list) else [],
            evidence_summary=str(candidate.get("evidence_summary", "")),
            status="active",
            metadata_json={
                **(candidate.get("metadata_json", {}) if isinstance(candidate.get("metadata_json", {}), dict) else {}),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        db.add(row)
        changed.append(row)

    await db.flush()
    return changed


async def list_concepts(
    *,
    db: AsyncSession,
    status: str = "",
    concept_type: str = "",
    limit: int = 50,
) -> list[WorkspaceConceptMemory]:
    rows = (
        await db.execute(
            select(WorkspaceConceptMemory)
            .order_by(WorkspaceConceptMemory.confidence.desc(), WorkspaceConceptMemory.id.desc())
        )
    ).scalars().all()
    if status:
        requested = status.strip().lower()
        if requested in CONCEPT_STATUSES:
            rows = [item for item in rows if str(item.status).strip().lower() == requested]
    if concept_type:
        requested_type = concept_type.strip().lower()
        rows = [item for item in rows if str(item.concept_type).strip().lower() == requested_type]
    return rows[: max(1, min(500, int(limit)))]


async def get_concept(*, concept_id: int, db: AsyncSession) -> WorkspaceConceptMemory | None:
    return (
        await db.execute(
            select(WorkspaceConceptMemory).where(WorkspaceConceptMemory.id == concept_id)
        )
    ).scalars().first()


async def acknowledge_concept(
    *,
    row: WorkspaceConceptMemory,
    actor: str,
    reason: str,
    metadata_json: dict,
) -> WorkspaceConceptMemory:
    row.status = "acknowledged"
    row.acknowledged_by = actor
    row.acknowledged_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "acknowledge_reason": reason,
    }
    return row


async def concept_influence_for_candidate(
    *,
    target_scope: str,
    strategy_type: str,
    db: AsyncSession,
) -> dict:
    scope = str(target_scope or "workspace").strip() or "workspace"
    rows = (
        await db.execute(
            select(WorkspaceConceptMemory)
            .where(WorkspaceConceptMemory.status.in_(["active", "acknowledged"]))
            .order_by(WorkspaceConceptMemory.confidence.desc(), WorkspaceConceptMemory.id.desc())
            .limit(100)
        )
    ).scalars().all()

    matched: list[WorkspaceConceptMemory] = []
    for row in rows:
        zones = row.affected_zones_json if isinstance(row.affected_zones_json, list) else []
        strategies = row.affected_strategies_json if isinstance(row.affected_strategies_json, list) else []
        zone_match = not zones or scope in [str(item) for item in zones]
        strategy_match = not strategies or strategy_type in [str(item) for item in strategies]
        if zone_match and strategy_match:
            matched.append(row)

    if not matched:
        return {"applied": False, "boost": 0.0, "concept_ids": [], "reason": "no_matching_concepts"}

    top = matched[:3]
    boost = min(0.25, sum(float(item.confidence or 0.0) for item in top) * 0.05)
    return {
        "applied": True,
        "boost": round(boost, 6),
        "concept_ids": [int(item.id) for item in top],
        "reason": "concept_pattern_match",
    }


async def concept_influence_for_goal(*, goal: dict, db: AsyncSession) -> dict:
    metadata = goal.get("metadata_json", {}) if isinstance(goal.get("metadata_json", {}), dict) else {}
    scope = str(metadata.get("scope") or metadata.get("zone") or "workspace").strip() or "workspace"
    rows = await list_concepts(db=db, status="", concept_type="", limit=100)
    matched = [
        row
        for row in rows
        if not isinstance(row.affected_zones_json, list)
        or not row.affected_zones_json
        or scope in [str(item) for item in row.affected_zones_json]
    ]
    if not matched:
        return {"applied": False, "boost": 0.0, "concept_ids": [], "reason": "no_matching_concepts"}
    top = matched[:3]
    boost = min(0.2, sum(float(item.confidence or 0.0) for item in top) * 0.04)
    return {
        "applied": True,
        "boost": round(boost, 6),
        "concept_ids": [int(item.id) for item in top],
        "reason": "concept_goal_pattern_match",
    }


async def concept_influence_for_proposal(*, related_zone: str, proposal_type: str, db: AsyncSession) -> dict:
    scope = str(related_zone or "workspace").strip() or "workspace"
    rows = await list_concepts(db=db, status="", concept_type="", limit=100)
    matched = []
    for row in rows:
        if str(row.concept_type or "") != "low_value_proposal_pattern":
            continue
        zones = row.affected_zones_json if isinstance(row.affected_zones_json, list) else []
        implications = row.suggested_implications_json if isinstance(row.suggested_implications_json, list) else []
        zone_match = not zones or scope in [str(item) for item in zones]
        type_match = any(proposal_type in str(item) for item in implications)
        if zone_match and type_match:
            matched.append(row)
    if not matched:
        return {"applied": False, "boost": 0.0, "concept_ids": [], "reason": "no_matching_concepts"}
    top = matched[:3]
    boost = min(0.15, sum(float(item.confidence or 0.0) for item in top) * 0.03)
    return {
        "applied": True,
        "boost": round(boost, 6),
        "concept_ids": [int(item.id) for item in top],
        "reason": "concept_proposal_pattern_match",
    }


async def concept_ids_for_component(*, affected_component: str, db: AsyncSession) -> list[int]:
    text = str(affected_component or "").strip().lower()
    if not text:
        return []
    rows = await list_concepts(db=db, status="", concept_type="", limit=150)
    matched: list[int] = []
    for row in rows:
        haystacks = [
            str(row.trigger_pattern or "").lower(),
            str(row.evidence_summary or "").lower(),
            " ".join(str(item).lower() for item in (row.affected_zones_json if isinstance(row.affected_zones_json, list) else [])),
            " ".join(str(item).lower() for item in (row.affected_strategies_json if isinstance(row.affected_strategies_json, list) else [])),
        ]
        if any(text in item or item in text for item in haystacks if item):
            matched.append(int(row.id))
    return matched[:10]


def to_concept_out(row: WorkspaceConceptMemory) -> dict:
    return {
        "concept_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "concept_type": row.concept_type,
        "trigger_pattern": row.trigger_pattern,
        "evidence_count": int(row.evidence_count or 0),
        "confidence": float(row.confidence or 0.0),
        "affected_zones": row.affected_zones_json if isinstance(row.affected_zones_json, list) else [],
        "affected_objects": row.affected_objects_json if isinstance(row.affected_objects_json, list) else [],
        "affected_strategies": row.affected_strategies_json if isinstance(row.affected_strategies_json, list) else [],
        "suggested_implications": row.suggested_implications_json if isinstance(row.suggested_implications_json, list) else [],
        "evidence_summary": row.evidence_summary,
        "status": row.status,
        "acknowledged_by": row.acknowledged_by,
        "acknowledged_at": row.acknowledged_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
