from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.maintenance_service import run_environment_maintenance_cycle
from core.models import (
    UserPreference,
    WorkspaceAutonomyBoundaryProfile,
    WorkspaceConceptMemory,
    WorkspaceDevelopmentPattern,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
    WorkspaceStrategyGoal,
    WorkspaceObservation,
)


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _default_target_state(*, stale_after_seconds: int) -> dict:
    return {
        "zone_freshness_seconds": int(max(60, stale_after_seconds)),
        "critical_object_confidence": 0.75,
        "max_unstable_regions": 0,
        "proactive_drift_monitoring": True,
    }


async def _latest_boundary(db: AsyncSession) -> WorkspaceAutonomyBoundaryProfile | None:
    return (
        await db.execute(
            select(WorkspaceAutonomyBoundaryProfile)
            .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def _recent_strategy_goals(*, lookback_hours: int, db: AsyncSession) -> list[WorkspaceStrategyGoal]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        await db.execute(
            select(WorkspaceStrategyGoal)
            .where(WorkspaceStrategyGoal.created_at >= since)
            .order_by(WorkspaceStrategyGoal.id.desc())
            .limit(200)
        )
    ).scalars().all()


async def _recent_concepts(*, lookback_hours: int, db: AsyncSession) -> list[WorkspaceConceptMemory]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        await db.execute(
            select(WorkspaceConceptMemory)
            .where(WorkspaceConceptMemory.created_at >= since)
            .where(WorkspaceConceptMemory.status == "active")
            .order_by(WorkspaceConceptMemory.id.desc())
            .limit(200)
        )
    ).scalars().all()


async def _recent_patterns(*, lookback_hours: int, db: AsyncSession) -> list[WorkspaceDevelopmentPattern]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        await db.execute(
            select(WorkspaceDevelopmentPattern)
            .where(WorkspaceDevelopmentPattern.last_seen_at >= since)
            .where(WorkspaceDevelopmentPattern.status == "active")
            .order_by(WorkspaceDevelopmentPattern.id.desc())
            .limit(200)
        )
    ).scalars().all()


async def _preference_weight(*, db: AsyncSession) -> float:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(
                UserPreference.preference_type.in_(
                    [
                        "stewardship_priority:default",
                        "prefer_auto_refresh_scans",
                        "prefer_minimal_interruption",
                    ]
                )
            )
            .order_by(UserPreference.last_updated.desc())
            .limit(20)
        )
    ).scalars().all()

    weight = 0.5
    for row in rows:
        ptype = str(row.preference_type or "").strip()
        value = row.value
        confidence = _bounded(float(row.confidence or 0.0))
        if ptype == "stewardship_priority:default":
            try:
                weight = _bounded(float(value))
            except Exception:
                pass
            continue
        if ptype == "prefer_auto_refresh_scans" and bool(value):
            weight = _bounded(weight + (0.25 * confidence))
        if ptype == "prefer_minimal_interruption" and bool(value):
            weight = _bounded(weight + (0.15 * confidence))
    return _bounded(weight)


async def _managed_scope_is_degraded(*, managed_scope: str, stale_after_seconds: int, db: AsyncSession) -> bool:
    scope = str(managed_scope or "").strip() or "global"
    if scope == "global":
        return True

    rows = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.lifecycle_status != "superseded")
            .where(WorkspaceObservation.zone == scope)
            .order_by(WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc())
            .limit(1)
        )
    ).scalars().all()
    if not rows:
        return False

    latest = rows[0]
    now = datetime.now(timezone.utc)
    age_seconds = max(0.0, (now - latest.last_seen_at).total_seconds())
    return age_seconds > float(max(1, int(stale_after_seconds)))


async def _get_or_create_stewardship(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    target_environment_state: dict,
    maintenance_priority: str,
    db: AsyncSession,
) -> WorkspaceStewardshipState:
    existing = (
        await db.execute(
            select(WorkspaceStewardshipState)
            .where(WorkspaceStewardshipState.managed_scope == managed_scope)
            .where(WorkspaceStewardshipState.status == "active")
            .order_by(WorkspaceStewardshipState.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing:
        return existing

    row = WorkspaceStewardshipState(
        source=source,
        actor=actor,
        status="active",
        target_environment_state_json=target_environment_state if isinstance(target_environment_state, dict) else {},
        managed_scope=managed_scope,
        maintenance_priority=maintenance_priority,
        current_health=1.0,
        cycle_count=0,
        linked_strategy_goal_ids_json=[],
        linked_maintenance_run_ids_json=[],
        linked_strategy_types_json=[],
        metadata_json={"objective60_stewardship": True},
    )
    db.add(row)
    await db.flush()
    return row


async def run_stewardship_cycle(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    stale_after_seconds: int,
    lookback_hours: int,
    max_strategies: int,
    max_actions: int,
    auto_execute: bool,
    force_degraded: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceStewardshipState, WorkspaceStewardshipCycle, dict]:
    strategy_goals = await _recent_strategy_goals(lookback_hours=lookback_hours, db=db)
    concepts = await _recent_concepts(lookback_hours=lookback_hours, db=db)
    patterns = await _recent_patterns(lookback_hours=lookback_hours, db=db)
    boundary = await _latest_boundary(db=db)
    preference_weight = await _preference_weight(db=db)

    target_environment_state = _default_target_state(stale_after_seconds=stale_after_seconds)
    stewardship = await _get_or_create_stewardship(
        actor=actor,
        source=source,
        managed_scope=managed_scope,
        target_environment_state=target_environment_state,
        maintenance_priority="high" if preference_weight >= 0.65 else "normal",
        db=db,
    )

    autonomy_level = str(boundary.current_level if boundary else "operator_required")
    boundary_confidence = _bounded(float(boundary.confidence or 0.0)) if boundary else 0.0
    allow_auto_execution = bool(auto_execute)
    if autonomy_level in {"manual_only", "operator_required"} and boundary_confidence >= 0.5:
        allow_auto_execution = False

    maintenance_run = None
    actions_executed = 0
    degraded_signals: list[dict] = []
    scope_degraded = await _managed_scope_is_degraded(managed_scope=managed_scope, stale_after_seconds=stale_after_seconds, db=db)
    should_run_correction = bool(force_degraded or scope_degraded)

    if should_run_correction:
        run, actions, _strategies, _memory_count = await run_environment_maintenance_cycle(
            actor=actor,
            source=source,
            stale_after_seconds=stale_after_seconds,
            max_strategies=max_strategies,
            max_actions=max_actions,
            auto_execute=allow_auto_execution,
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective60_stewardship": True,
                "managed_scope": managed_scope,
            },
            db=db,
        )
        maintenance_run = run
        run_signals = run.detected_signals_json if isinstance(run.detected_signals_json, list) else []
        if str(managed_scope or "").strip() and str(managed_scope).strip() != "global":
            target_scope = str(managed_scope).strip()
            run_signals = [
                item
                for item in run_signals
                if isinstance(item, dict) and str(item.get("target_scope", "")).strip() == target_scope
            ]
        degraded_signals = run_signals
        actions_executed = len(actions) if degraded_signals else 0

    if force_degraded and not degraded_signals:
        degraded_signals = [
            {
                "signal_type": "forced_stewardship_degraded",
                "target_scope": managed_scope,
                "severity": 0.8,
                "age_seconds": float(stale_after_seconds * 2),
            }
        ]

    degradation_score = _bounded(float(len(degraded_signals)) / 5.0)
    pre_health = _bounded(1.0 - degradation_score)

    improvement = 0.0
    if actions_executed > 0:
        improvement = _bounded(0.15 + (0.1 * min(actions_executed, 3)))
    elif not degraded_signals:
        improvement = 0.02

    post_health = _bounded(pre_health + improvement)
    next_cycle = datetime.now(timezone.utc) + timedelta(minutes=(30 if degraded_signals else 90))

    linked_goal_ids = [int(item.id) for item in strategy_goals[:25]]
    linked_strategy_types = sorted({str(item.strategy_type or "").strip() for item in strategy_goals if str(item.strategy_type or "").strip()})

    stewardship.current_health = post_health
    stewardship.last_cycle_at = datetime.now(timezone.utc)
    stewardship.next_cycle_at = next_cycle
    stewardship.cycle_count = int(stewardship.cycle_count or 0) + 1
    stewardship.linked_strategy_goal_ids_json = linked_goal_ids
    stewardship.linked_strategy_types_json = linked_strategy_types
    if maintenance_run:
        prior_run_ids = stewardship.linked_maintenance_run_ids_json if isinstance(stewardship.linked_maintenance_run_ids_json, list) else []
        stewardship.linked_maintenance_run_ids_json = [*prior_run_ids, int(maintenance_run.id)][-50:]
    stewardship.linked_autonomy_boundary_id = int(boundary.id) if boundary else None
    stewardship.last_decision_summary = (
        "executed_corrective_actions"
        if actions_executed > 0
        else ("monitor_only_stable_state" if not degraded_signals else "defer_to_operator_boundary")
    )
    stewardship.metadata_json = {
        **(stewardship.metadata_json if isinstance(stewardship.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "objective60_stewardship": True,
    }

    cycle = WorkspaceStewardshipCycle(
        stewardship_id=int(stewardship.id),
        source=source,
        actor=actor,
        pre_health=pre_health,
        post_health=post_health,
        improvement_delta=_bounded(post_health - pre_health, lo=-1.0, hi=1.0),
        degraded_signals_json=degraded_signals,
        selected_actions_json=(
            [
                {
                    "action_type": "maintenance_cycle",
                    "maintenance_run_id": int(maintenance_run.id),
                    "actions_executed": int(actions_executed),
                    "auto_execute": bool(allow_auto_execution),
                }
            ]
            if maintenance_run
            else []
        ),
        decision_json={
            "allow_auto_execution": bool(allow_auto_execution),
            "scope_degraded": bool(scope_degraded),
            "should_run_correction": bool(should_run_correction),
            "autonomy_level": autonomy_level,
            "boundary_confidence": boundary_confidence,
            "decision": stewardship.last_decision_summary,
        },
        integration_evidence_json={
            "strategy_goal_ids": linked_goal_ids,
            "strategy_types": linked_strategy_types,
            "concept_count": len(concepts),
            "development_pattern_count": len(patterns),
            "autonomy_boundary_id": int(boundary.id) if boundary else None,
            "operator_preference_weight": preference_weight,
        },
        maintenance_run_id=(int(maintenance_run.id) if maintenance_run else None),
        improved=post_health >= pre_health,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective60_stewardship": True,
            "managed_scope": managed_scope,
        },
    )
    db.add(cycle)
    await db.flush()

    summary = {
        "degraded_signal_count": len(degraded_signals),
        "actions_executed": int(actions_executed),
        "pre_health": pre_health,
        "post_health": post_health,
        "autonomy_level": autonomy_level,
        "allow_auto_execution": bool(allow_auto_execution),
        "integrations": {
            "strategy_goals": len(strategy_goals),
            "concept_memory": len(concepts),
            "development_patterns": len(patterns),
            "autonomy_boundary": bool(boundary),
            "operator_preference_weight": preference_weight,
        },
    }
    return stewardship, cycle, summary


async def list_stewardship_states(*, managed_scope: str, limit: int, db: AsyncSession) -> list[WorkspaceStewardshipState]:
    stmt = select(WorkspaceStewardshipState).order_by(WorkspaceStewardshipState.id.desc())
    if str(managed_scope or "").strip():
        stmt = stmt.where(WorkspaceStewardshipState.managed_scope == str(managed_scope).strip())
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()
    return rows


async def get_stewardship_state(*, stewardship_id: int, db: AsyncSession) -> WorkspaceStewardshipState | None:
    return (
        await db.execute(
            select(WorkspaceStewardshipState)
            .where(WorkspaceStewardshipState.id == stewardship_id)
            .limit(1)
        )
    ).scalars().first()


async def list_stewardship_history(*, stewardship_id: int | None, limit: int, db: AsyncSession) -> list[WorkspaceStewardshipCycle]:
    stmt = select(WorkspaceStewardshipCycle).order_by(WorkspaceStewardshipCycle.id.desc())
    if stewardship_id is not None:
        stmt = stmt.where(WorkspaceStewardshipCycle.stewardship_id == int(stewardship_id))
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()
    return rows


def to_stewardship_out(row: WorkspaceStewardshipState) -> dict:
    return {
        "stewardship_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "target_environment_state": row.target_environment_state_json if isinstance(row.target_environment_state_json, dict) else {},
        "managed_scope": row.managed_scope,
        "maintenance_priority": row.maintenance_priority,
        "current_health": float(row.current_health or 0.0),
        "last_cycle": row.last_cycle_at,
        "next_cycle": row.next_cycle_at,
        "cycle_count": int(row.cycle_count or 0),
        "linked_strategy_goal_ids": row.linked_strategy_goal_ids_json if isinstance(row.linked_strategy_goal_ids_json, list) else [],
        "linked_maintenance_run_ids": row.linked_maintenance_run_ids_json if isinstance(row.linked_maintenance_run_ids_json, list) else [],
        "linked_strategy_types": row.linked_strategy_types_json if isinstance(row.linked_strategy_types_json, list) else [],
        "linked_autonomy_boundary_id": row.linked_autonomy_boundary_id,
        "last_decision_summary": row.last_decision_summary,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_stewardship_cycle_out(row: WorkspaceStewardshipCycle) -> dict:
    return {
        "cycle_id": int(row.id),
        "stewardship_id": int(row.stewardship_id),
        "source": row.source,
        "actor": row.actor,
        "pre_health": float(row.pre_health or 0.0),
        "post_health": float(row.post_health or 0.0),
        "improvement_delta": float(row.improvement_delta or 0.0),
        "degraded_signals": row.degraded_signals_json if isinstance(row.degraded_signals_json, list) else [],
        "selected_actions": row.selected_actions_json if isinstance(row.selected_actions_json, list) else [],
        "decision": row.decision_json if isinstance(row.decision_json, dict) else {},
        "integration_evidence": row.integration_evidence_json if isinstance(row.integration_evidence_json, dict) else {},
        "maintenance_run_id": row.maintenance_run_id,
        "improved": bool(row.improved),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
