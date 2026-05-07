from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    ConstraintEvaluation,
    WorkspaceDecisionRecord,
    WorkspaceDevelopmentPattern,
    WorkspaceEnvironmentStrategy,
    WorkspaceHorizonReplanEvent,
    WorkspaceImprovementProposal,
    WorkspacePolicyExperiment,
)


DEVELOPMENT_PATTERN_STATUSES = {"active", "acknowledged", "superseded"}


def _pattern_confidence(evidence_count: int) -> float:
    return max(0.0, min(0.99, 0.42 + (0.055 * float(max(0, evidence_count)))))


async def _existing_pattern(
    *,
    pattern_type: str,
    affected_component: str,
    db: AsyncSession,
) -> WorkspaceDevelopmentPattern | None:
    return (
        await db.execute(
            select(WorkspaceDevelopmentPattern)
            .where(WorkspaceDevelopmentPattern.pattern_type == pattern_type)
            .where(WorkspaceDevelopmentPattern.affected_component == affected_component)
            .where(WorkspaceDevelopmentPattern.status.in_(["active", "acknowledged"]))
            .order_by(WorkspaceDevelopmentPattern.id.desc())
        )
    ).scalars().first()


async def _upsert_pattern(
    *,
    source: str,
    actor: str,
    pattern_type: str,
    affected_component: str,
    evidence_count: int,
    evidence_summary: str,
    metadata_json: dict,
    observed_at: datetime,
    db: AsyncSession,
) -> WorkspaceDevelopmentPattern:
    existing = await _existing_pattern(
        pattern_type=pattern_type,
        affected_component=affected_component,
        db=db,
    )
    if existing:
        existing.evidence_count = int(existing.evidence_count or 0) + int(max(0, evidence_count))
        existing.confidence = _pattern_confidence(int(existing.evidence_count or 0))
        existing.last_seen_at = observed_at
        existing.evidence_summary = evidence_summary or existing.evidence_summary
        existing.metadata_json = {
            **(existing.metadata_json if isinstance(existing.metadata_json, dict) else {}),
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return existing

    row = WorkspaceDevelopmentPattern(
        source=source,
        actor=actor,
        pattern_type=pattern_type,
        evidence_count=max(0, int(evidence_count)),
        confidence=_pattern_confidence(max(0, int(evidence_count))),
        affected_component=affected_component,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        evidence_summary=evidence_summary,
        status="active",
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    return row


async def _strategy_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceEnvironmentStrategy)
            .where(WorkspaceEnvironmentStrategy.created_at >= since)
            .order_by(WorkspaceEnvironmentStrategy.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    success_counts: dict[str, int] = {}
    stall_counts: dict[str, int] = {}
    for row in rows:
        component = f"environment_strategy:{str(row.strategy_type or 'strategy').strip() or 'strategy'}"
        status = str(row.current_status or "").strip().lower()
        if status in {"stable", "completed"}:
            success_counts[component] = success_counts.get(component, 0) + 1
        if status in {"blocked", "superseded"}:
            stall_counts[component] = stall_counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in success_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "strategy_repeatedly_successful",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Strategy component '{component}' reached stable/completed status {count} times",
                "metadata_json": {"source_table": "workspace_environment_strategies", "signal": "success"},
                "observed_at": datetime.now(timezone.utc),
            }
        )

    for component, count in stall_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "strategy_underperforming",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Strategy component '{component}' stalled/superseded {count} times",
                "metadata_json": {"source_table": "workspace_environment_strategies", "signal": "stall"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def _constraint_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(ConstraintEvaluation)
            .where(ConstraintEvaluation.created_at >= since)
            .order_by(ConstraintEvaluation.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    for row in rows:
        warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
        if str(row.outcome_result or "") != "succeeded":
            continue
        if float(row.outcome_quality or 0.0) < 0.7:
            continue
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            key = str(warning.get("constraint", "")).strip()
            if not key:
                continue
            component = f"constraint:{key}"
            counts[component] = counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "constraint_threshold_too_high",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Constraint '{component}' warned {count} times while outcomes still succeeded",
                "metadata_json": {"source_table": "constraint_evaluations", "signal": "soft_friction_success"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def _experiment_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspacePolicyExperiment)
            .where(WorkspacePolicyExperiment.created_at >= since)
            .order_by(WorkspacePolicyExperiment.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    promote_counts: dict[str, int] = {}
    for row in rows:
        if str(row.recommendation or "") != "promote":
            continue
        etype = str(row.experiment_type or "policy_adjustment_sandbox").strip() or "policy_adjustment_sandbox"
        component = f"policy_experiment:{etype}"
        promote_counts[component] = promote_counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in promote_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "experiment_consistently_successful",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Experiment component '{component}' produced promote recommendations {count} times",
                "metadata_json": {"source_table": "workspace_policy_experiments", "signal": "promote_recommendation"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def _proposal_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceImprovementProposal)
            .where(WorkspaceImprovementProposal.created_at >= since)
            .order_by(WorkspaceImprovementProposal.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    low_value_counts: dict[str, int] = {}
    for row in rows:
        low_value = str(row.status or "") == "rejected" or float(row.confidence or 0.0) < 0.45
        if not low_value:
            continue
        ptype = str(row.proposal_type or "unknown").strip() or "unknown"
        component = f"proposal_type:{ptype}"
        low_value_counts[component] = low_value_counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in low_value_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "proposal_type_low_value",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Proposal component '{component}' was rejected/low-value {count} times",
                "metadata_json": {"source_table": "workspace_improvement_proposals", "signal": "low_value"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def _zone_friction_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    replan_rows = (
        await db.execute(
            select(WorkspaceHorizonReplanEvent)
            .where(WorkspaceHorizonReplanEvent.created_at >= since)
            .order_by(WorkspaceHorizonReplanEvent.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    zone_counts: dict[str, int] = {}
    for row in replan_rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        zone = str(metadata.get("zone") or metadata.get("target_zone") or "workspace").strip() or "workspace"
        component = f"zone:{zone}"
        zone_counts[component] = zone_counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in zone_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "zone_recurring_friction",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Zone friction detected for '{component}' across {count} replan events",
                "metadata_json": {"source_table": "workspace_horizon_replan_events", "signal": "replan_friction"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def _operator_override_patterns(*, since: datetime, min_evidence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceDecisionRecord)
            .where(WorkspaceDecisionRecord.created_at >= since)
            .order_by(WorkspaceDecisionRecord.id.desc())
            .limit(4000)
        )
    ).scalars().all()

    override_counts: dict[str, int] = {}
    for row in rows:
        context = row.source_context_json if isinstance(row.source_context_json, dict) else {}
        endpoint = str(context.get("endpoint", "")).strip()
        if not (endpoint.endswith("/resolve") or endpoint.endswith("/deactivate")):
            continue
        dtype = str(row.decision_type or "decision").strip() or "decision"
        component = f"operator_override:{dtype}"
        override_counts[component] = override_counts.get(component, 0) + 1

    candidates: list[dict] = []
    for component, count in override_counts.items():
        if count < min_evidence_count:
            continue
        candidates.append(
            {
                "pattern_type": "operator_override_frequent",
                "affected_component": component,
                "evidence_count": count,
                "evidence_summary": f"Operator overrides for '{component}' occurred {count} times",
                "metadata_json": {"source_table": "workspace_decision_records", "signal": "override"},
                "observed_at": datetime.now(timezone.utc),
            }
        )
    return candidates


async def extract_development_patterns(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_evidence_count: int,
    max_patterns: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceDevelopmentPattern]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    threshold = max(2, int(min_evidence_count))

    candidates: list[dict] = []
    candidates.extend(await _strategy_patterns(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _constraint_patterns(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _experiment_patterns(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _proposal_patterns(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _zone_friction_patterns(since=since, min_evidence_count=threshold, db=db))
    candidates.extend(await _operator_override_patterns(since=since, min_evidence_count=threshold, db=db))

    changed: list[WorkspaceDevelopmentPattern] = []
    for candidate in sorted(candidates, key=lambda item: int(item.get("evidence_count", 0)), reverse=True):
        if len(changed) >= max(1, min(200, int(max_patterns))):
            break

        row = await _upsert_pattern(
            source=source,
            actor=actor,
            pattern_type=str(candidate.get("pattern_type", "development_pattern")),
            affected_component=str(candidate.get("affected_component", "system")),
            evidence_count=int(candidate.get("evidence_count", 0)),
            evidence_summary=str(candidate.get("evidence_summary", "")),
            metadata_json={
                **(candidate.get("metadata_json", {}) if isinstance(candidate.get("metadata_json", {}), dict) else {}),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "lookback_hours": max(1, int(lookback_hours)),
            },
            observed_at=candidate.get("observed_at") if isinstance(candidate.get("observed_at"), datetime) else datetime.now(timezone.utc),
            db=db,
        )
        changed.append(row)

    await db.flush()
    return changed


async def list_development_patterns(
    *,
    db: AsyncSession,
    status: str = "",
    pattern_type: str = "",
    limit: int = 50,
) -> list[WorkspaceDevelopmentPattern]:
    rows = (
        await db.execute(
            select(WorkspaceDevelopmentPattern)
            .order_by(WorkspaceDevelopmentPattern.confidence.desc(), WorkspaceDevelopmentPattern.id.desc())
        )
    ).scalars().all()
    if status:
        requested = status.strip().lower()
        if requested in DEVELOPMENT_PATTERN_STATUSES:
            rows = [item for item in rows if str(item.status).strip().lower() == requested]
    if pattern_type:
        requested_type = pattern_type.strip().lower()
        rows = [item for item in rows if str(item.pattern_type).strip().lower() == requested_type]
    return rows[: max(1, min(500, int(limit)))]


async def get_development_pattern(*, pattern_id: int, db: AsyncSession) -> WorkspaceDevelopmentPattern | None:
    return (
        await db.execute(
            select(WorkspaceDevelopmentPattern).where(WorkspaceDevelopmentPattern.id == pattern_id)
        )
    ).scalars().first()


async def development_pattern_ids_for_component(*, affected_component: str, db: AsyncSession) -> list[int]:
    text = str(affected_component or "").strip().lower()
    if not text:
        return []
    rows = await list_development_patterns(db=db, status="", pattern_type="", limit=200)
    matched: list[int] = []
    for row in rows:
        haystacks = [
            str(row.affected_component or "").strip().lower(),
            str(row.pattern_type or "").strip().lower(),
            str(row.evidence_summary or "").strip().lower(),
        ]
        if any(text in item or item in text for item in haystacks if item):
            matched.append(int(row.id))
    return matched[:12]


async def development_influence_for_strategy(*, strategy_type: str, db: AsyncSession) -> dict:
    component = f"environment_strategy:{str(strategy_type or '').strip() or 'strategy'}"
    rows = await list_development_patterns(db=db, status="", pattern_type="", limit=250)

    penalties = [
        row
        for row in rows
        if str(row.pattern_type or "") == "strategy_underperforming"
        and str(row.affected_component or "") == component
    ]
    boosts = [
        row
        for row in rows
        if str(row.pattern_type or "") == "strategy_repeatedly_successful"
        and str(row.affected_component or "") == component
    ]

    penalty = min(0.2, sum(float(item.confidence or 0.0) for item in penalties[:3]) * 0.03)
    boost = min(0.2, sum(float(item.confidence or 0.0) for item in boosts[:3]) * 0.03)
    net = round(boost - penalty, 6)
    if abs(net) <= 0.000001:
        return {"applied": False, "weight_delta": 0.0, "pattern_ids": [], "reason": "no_matching_development_patterns"}

    ids = [int(item.id) for item in boosts[:3]] + [int(item.id) for item in penalties[:3]]
    return {
        "applied": True,
        "weight_delta": net,
        "pattern_ids": ids,
        "reason": "development_pattern_strategy_signal",
    }


async def development_influence_for_experiment(*, experiment_type: str, db: AsyncSession) -> dict:
    component = f"policy_experiment:{str(experiment_type or 'policy_adjustment_sandbox').strip() or 'policy_adjustment_sandbox'}"
    rows = await list_development_patterns(db=db, status="", pattern_type="", limit=200)
    matched = [
        row
        for row in rows
        if str(row.pattern_type or "") == "experiment_consistently_successful"
        and str(row.affected_component or "") == component
    ]
    if not matched:
        return {"applied": False, "promote_threshold_delta": 0.0, "pattern_ids": [], "reason": "no_matching_development_patterns"}

    delta = min(0.05, sum(float(item.confidence or 0.0) for item in matched[:3]) * 0.01)
    return {
        "applied": True,
        "promote_threshold_delta": round(delta, 6),
        "pattern_ids": [int(item.id) for item in matched[:3]],
        "reason": "development_pattern_experiment_success_signal",
    }


def to_development_pattern_out(row: WorkspaceDevelopmentPattern) -> dict:
    return {
        "pattern_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "pattern_type": row.pattern_type,
        "evidence_count": int(row.evidence_count or 0),
        "confidence": float(row.confidence or 0.0),
        "affected_component": row.affected_component,
        "first_seen": row.first_seen_at,
        "last_seen": row.last_seen_at,
        "evidence_summary": row.evidence_summary,
        "status": row.status,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
