from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.development_memory_service import (
    extract_development_patterns,
    list_development_patterns,
)
from core.execution_truth_service import summarize_execution_truth
from core.improvement_governance_service import list_improvement_backlog
from core.models import (
    CapabilityExecution,
    InputEvent,
    MemoryEntry,
    SpeechOutputAction,
    WorkspaceCrossDomainReasoningContext,
    WorkspaceImprovementBacklog,
    WorkspaceObservation,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
)


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


async def _stewardship_state(
    *, since: datetime, max_items: int, db: AsyncSession
) -> dict:
    states = (
        (
            await db.execute(
                select(WorkspaceStewardshipState)
                .where(WorkspaceStewardshipState.status == "active")
                .order_by(WorkspaceStewardshipState.id.desc())
                .limit(max_items)
            )
        )
        .scalars()
        .all()
    )
    cycles = (
        (
            await db.execute(
                select(WorkspaceStewardshipCycle)
                .where(WorkspaceStewardshipCycle.created_at >= since)
                .order_by(WorkspaceStewardshipCycle.id.desc())
                .limit(max_items * 3)
            )
        )
        .scalars()
        .all()
    )

    unstable_scope_count = 0
    inquiry_candidate_count = 0
    stability_scores: list[float] = []
    uncertainty_scores: list[float] = []
    drift_rates: list[float] = []
    scope_samples: list[dict] = []

    for cycle in cycles:
        metadata = cycle.metadata_json if isinstance(cycle.metadata_json, dict) else {}
        assessment = (
            metadata.get("assessment", {})
            if isinstance(metadata.get("assessment", {}), dict)
            else {}
        )
        post = (
            assessment.get("post", {})
            if isinstance(assessment.get("post", {}), dict)
            else {}
        )
        system_metrics = (
            post.get("system_metrics", {})
            if isinstance(post.get("system_metrics", {}), dict)
            else {}
        )
        scope_metrics = (
            post.get("scope_metrics", {})
            if isinstance(post.get("scope_metrics", {}), dict)
            else {}
        )
        inquiry_candidates = (
            post.get("inquiry_candidates", [])
            if isinstance(post.get("inquiry_candidates", []), list)
            else []
        )
        if bool(post.get("needs_intervention", False)):
            unstable_scope_count += 1
        inquiry_candidate_count += len(inquiry_candidates)
        stability_scores.append(_safe_float(system_metrics.get("stability_score", 0.0)))
        uncertainty_scores.append(
            _safe_float(system_metrics.get("uncertainty_score", 0.0))
        )
        drift_rates.append(_safe_float(system_metrics.get("drift_rate", 0.0)))

        managed_scope = str(metadata.get("managed_scope", "global")).strip() or "global"
        scope_samples.append(
            {
                "cycle_id": int(cycle.id),
                "stewardship_id": int(cycle.stewardship_id),
                "managed_scope": managed_scope,
                "needs_intervention": bool(post.get("needs_intervention", False)),
                "degraded_signal_count": len(
                    post.get("deviation_signals", [])
                    if isinstance(post.get("deviation_signals", []), list)
                    else []
                ),
                "key_object_count": len(
                    scope_metrics.get("key_objects", [])
                    if isinstance(scope_metrics.get("key_objects", []), list)
                    else []
                ),
            }
        )

    def _average(values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    return {
        "active_stewardship_count": len(states),
        "recent_cycle_count": len(cycles),
        "unstable_scope_count": unstable_scope_count,
        "inquiry_candidate_count": inquiry_candidate_count,
        "average_stability_score": round(_average(stability_scores), 6),
        "average_uncertainty_score": round(_average(uncertainty_scores), 6),
        "average_drift_rate": round(_average(drift_rates), 6),
        "managed_scopes": [
            {
                "stewardship_id": int(item.id),
                "managed_scope": item.managed_scope,
                "current_health": float(item.current_health or 0.0),
                "cycle_count": int(item.cycle_count or 0),
            }
            for item in states[:10]
        ],
        "recent_scope_samples": scope_samples[:10],
    }


async def _workspace_state(
    *, since: datetime, max_items: int, db: AsyncSession
) -> dict:
    rows = (
        (
            await db.execute(
                select(WorkspaceObservation)
                .where(WorkspaceObservation.last_seen_at >= since)
                .order_by(
                    WorkspaceObservation.last_seen_at.desc(),
                    WorkspaceObservation.id.desc(),
                )
                .limit(max_items)
            )
        )
        .scalars()
        .all()
    )

    zone_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    for row in rows:
        zone_counts[str(row.zone or "unknown")] += 1
        label_counts[str(row.label or "unknown")] += 1

    execution_rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.created_at >= since)
                .order_by(CapabilityExecution.id.desc())
                .limit(max_items)
            )
        )
        .scalars()
        .all()
    )
    execution_truth_summary = summarize_execution_truth(execution_rows)

    stewardship = await _stewardship_state(since=since, max_items=max_items, db=db)

    return {
        "observation_count": len(rows),
        "zones": [
            {"zone": key, "count": value} for key, value in zone_counts.most_common(5)
        ],
        "labels": [
            {"label": key, "count": value} for key, value in label_counts.most_common(5)
        ],
        "sample_observation_ids": [int(item.id) for item in rows[:10]],
        "execution_truth_summary": execution_truth_summary,
        "stewardship_summary": stewardship,
    }


async def _communication_state(
    *, since: datetime, max_items: int, db: AsyncSession
) -> dict:
    inputs = (
        (
            await db.execute(
                select(InputEvent)
                .where(InputEvent.created_at >= since)
                .order_by(InputEvent.id.desc())
                .limit(max_items)
            )
        )
        .scalars()
        .all()
    )
    outputs = (
        (
            await db.execute(
                select(SpeechOutputAction)
                .where(SpeechOutputAction.created_at >= since)
                .order_by(SpeechOutputAction.id.desc())
                .limit(max_items)
            )
        )
        .scalars()
        .all()
    )

    source_counts: Counter[str] = Counter(
        str(item.source or "unknown") for item in inputs
    )
    intent_counts: Counter[str] = Counter(
        str(item.parsed_intent or "unknown") for item in inputs
    )

    return {
        "input_event_count": len(inputs),
        "output_event_count": len(outputs),
        "input_sources": [
            {"source": key, "count": value}
            for key, value in source_counts.most_common(6)
        ],
        "intents": [
            {"intent": key, "count": value}
            for key, value in intent_counts.most_common(6)
        ],
        "sample_input_event_ids": [int(item.id) for item in inputs[:10]],
        "sample_output_ids": [int(item.id) for item in outputs[:10]],
    }


async def _external_information_state(
    *, since: datetime, max_items: int, db: AsyncSession
) -> dict:
    rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.created_at >= since)
                .order_by(MemoryEntry.id.desc())
                .limit(max_items * 3)
            )
        )
        .scalars()
        .all()
    )

    external_rows = [
        item
        for item in rows
        if str(item.memory_class or "").strip().lower().startswith("external")
    ][:max_items]

    return {
        "external_item_count": len(external_rows),
        "items": [
            {
                "memory_id": int(item.id),
                "memory_class": item.memory_class,
                "summary": item.summary,
                "content": item.content,
                "metadata_json": item.metadata_json
                if isinstance(item.metadata_json, dict)
                else {},
            }
            for item in external_rows
        ],
    }


async def _development_state(
    *, lookback_hours: int, max_items: int, db: AsyncSession
) -> dict:
    await extract_development_patterns(
        actor="workspace",
        source="objective53",
        lookback_hours=lookback_hours,
        min_evidence_count=2,
        max_patterns=max_items,
        metadata_json={"objective56_refresh": True},
        db=db,
    )

    rows = await list_development_patterns(
        db=db, status="active", pattern_type="", limit=max_items
    )
    return {
        "pattern_count": len(rows),
        "patterns": [
            {
                "pattern_id": int(item.id),
                "pattern_type": item.pattern_type,
                "affected_component": item.affected_component,
                "evidence_count": int(item.evidence_count or 0),
                "confidence": float(item.confidence or 0.0),
            }
            for item in rows
        ],
    }


async def _self_improvement_state(
    *, since: datetime, max_items: int, db: AsyncSession
) -> dict:
    del since
    rows = await list_improvement_backlog(
        db=db,
        status="",
        risk_level="",
        limit=max_items,
    )

    status_counts: Counter[str] = Counter(
        str(item.status or "unknown") for item in rows
    )
    return {
        "backlog_item_count": len(rows),
        "status_counts": [
            {"status": key, "count": value}
            for key, value in status_counts.most_common(8)
        ],
        "top_items": [
            {
                "improvement_id": int(item.id),
                "proposal_id": int(item.proposal_id),
                "priority_score": float(item.priority_score or 0.0),
                "risk_level": item.risk_level,
                "governance_decision": item.governance_decision,
                "status": item.status,
            }
            for item in rows[:10]
        ],
    }


def _reasoning_summary(
    *,
    workspace: dict,
    communication: dict,
    external: dict,
    development: dict,
    self_improvement: dict,
) -> tuple[str, dict, float]:
    links: list[str] = []
    stewardship = (
        workspace.get("stewardship_summary", {})
        if isinstance(workspace.get("stewardship_summary", {}), dict)
        else {}
    )
    execution_truth_summary = (
        workspace.get("execution_truth_summary", {})
        if isinstance(workspace.get("execution_truth_summary", {}), dict)
        else {}
    )
    if (
        int(workspace.get("observation_count", 0)) > 0
        and int(communication.get("input_event_count", 0)) > 0
    ):
        links.append(
            "Communication inputs can be grounded with current workspace observations."
        )
    if (
        int(external.get("external_item_count", 0)) > 0
        and int(development.get("pattern_count", 0)) > 0
    ):
        links.append(
            "External information can be interpreted through active developmental patterns."
        )
    if (
        int(self_improvement.get("backlog_item_count", 0)) > 0
        and int(development.get("pattern_count", 0)) > 0
    ):
        links.append(
            "Self-improvement backlog can be prioritized using developmental memory signals."
        )
    if (
        int(communication.get("input_event_count", 0)) > 0
        and int(self_improvement.get("backlog_item_count", 0)) > 0
    ):
        links.append(
            "Communication demand can influence governance of improvement experimentation."
        )
    if int(stewardship.get("unstable_scope_count", 0)) > 0:
        links.append(
            "Stewardship instability should influence task timing and dependency management."
        )
    if int(stewardship.get("inquiry_candidate_count", 0)) > 0:
        links.append(
            "Stewardship-generated inquiry candidates can clarify degraded environment assumptions."
        )
    if int(execution_truth_summary.get("deviation_signal_count", 0)) > 0:
        links.append(
            "Execution-truth deviations should influence planning assumptions, runtime trust, and operator guidance."
        )

    domains_present = 0
    domains_present += 1 if int(workspace.get("observation_count", 0)) > 0 else 0
    domains_present += 1 if int(communication.get("input_event_count", 0)) > 0 else 0
    domains_present += 1 if int(external.get("external_item_count", 0)) > 0 else 0
    domains_present += 1 if int(development.get("pattern_count", 0)) > 0 else 0
    domains_present += (
        1 if int(self_improvement.get("backlog_item_count", 0)) > 0 else 0
    )
    domains_present += (
        1
        if int(stewardship.get("active_stewardship_count", 0)) > 0
        or int(stewardship.get("recent_cycle_count", 0)) > 0
        else 0
    )
    domains_present += (
        1 if int(execution_truth_summary.get("execution_count", 0)) > 0 else 0
    )

    confidence = max(0.0, min(1.0, domains_present / 7.0))
    summary = (
        f"Cross-domain context merged {domains_present}/7 domains; "
        f"workspace_obs={workspace.get('observation_count', 0)}, "
        f"comm_inputs={communication.get('input_event_count', 0)}, "
        f"external_items={external.get('external_item_count', 0)}, "
        f"development_patterns={development.get('pattern_count', 0)}, "
        f"improvement_backlog={self_improvement.get('backlog_item_count', 0)}, "
        f"stewardship_active={stewardship.get('active_stewardship_count', 0)}, "
        f"execution_truth={execution_truth_summary.get('execution_count', 0)}"
    )
    return (
        summary,
        {
            "cross_domain_links": links,
            "execution_truth_influence": execution_truth_summary,
        },
        confidence,
    )


async def build_cross_domain_reasoning_context(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_items_per_domain: int,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceCrossDomainReasoningContext:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    max_items = max(1, min(200, int(max_items_per_domain)))

    workspace = await _workspace_state(since=since, max_items=max_items, db=db)
    communication = await _communication_state(since=since, max_items=max_items, db=db)
    external = await _external_information_state(
        since=since, max_items=max_items, db=db
    )
    development = await _development_state(
        lookback_hours=max(1, int(lookback_hours)), max_items=max_items, db=db
    )
    self_improvement = await _self_improvement_state(
        since=since, max_items=max_items, db=db
    )

    summary, reasoning, confidence = _reasoning_summary(
        workspace=workspace,
        communication=communication,
        external=external,
        development=development,
        self_improvement=self_improvement,
    )

    row = WorkspaceCrossDomainReasoningContext(
        source=source,
        actor=actor,
        lookback_hours=max(1, int(lookback_hours)),
        workspace_state_json=workspace,
        communication_state_json=communication,
        external_information_json=external,
        development_state_json=development,
        self_improvement_state_json=self_improvement,
        reasoning_summary=summary,
        reasoning_json=reasoning,
        confidence=confidence,
        status="active",
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective56_cross_domain_reasoning": True,
        },
    )
    db.add(row)
    await db.flush()
    return row


async def list_cross_domain_reasoning_contexts(
    *,
    db: AsyncSession,
    status: str = "",
    limit: int = 50,
) -> list[WorkspaceCrossDomainReasoningContext]:
    rows = (
        (
            await db.execute(
                select(WorkspaceCrossDomainReasoningContext).order_by(
                    WorkspaceCrossDomainReasoningContext.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    filtered = rows
    if status:
        requested = status.strip().lower()
        filtered = [
            item for item in filtered if str(item.status).strip().lower() == requested
        ]
    return filtered[: max(1, min(500, int(limit)))]


async def get_cross_domain_reasoning_context(
    *, context_id: int, db: AsyncSession
) -> WorkspaceCrossDomainReasoningContext | None:
    return (
        (
            await db.execute(
                select(WorkspaceCrossDomainReasoningContext).where(
                    WorkspaceCrossDomainReasoningContext.id == context_id
                )
            )
        )
        .scalars()
        .first()
    )


def to_cross_domain_reasoning_out(row: WorkspaceCrossDomainReasoningContext) -> dict:
    return {
        "context_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "lookback_hours": int(row.lookback_hours or 0),
        "workspace_state": row.workspace_state_json
        if isinstance(row.workspace_state_json, dict)
        else {},
        "communication_state": row.communication_state_json
        if isinstance(row.communication_state_json, dict)
        else {},
        "external_information": row.external_information_json
        if isinstance(row.external_information_json, dict)
        else {},
        "development_state": row.development_state_json
        if isinstance(row.development_state_json, dict)
        else {},
        "self_improvement_state": row.self_improvement_state_json
        if isinstance(row.self_improvement_state_json, dict)
        else {},
        "reasoning_summary": row.reasoning_summary,
        "reasoning": row.reasoning_json if isinstance(row.reasoning_json, dict) else {},
        "confidence": float(row.confidence or 0.0),
        "status": row.status,
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "created_at": row.created_at,
    }
