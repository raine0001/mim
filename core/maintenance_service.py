from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.decision_record_service import record_decision
from core.environment_strategy_service import generate_environment_strategies, resolve_environment_strategy, to_environment_strategy_out
from core.models import MemoryEntry, WorkspaceEnvironmentStrategy, WorkspaceMaintenanceAction, WorkspaceMaintenanceRun, WorkspaceObservation


async def _detect_stale_zones(*, stale_after_seconds: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.lifecycle_status != "superseded")
            .order_by(WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc())
            .limit(5000)
        )
    ).scalars().all()

    by_zone_latest: dict[str, datetime] = {}
    for row in rows:
        zone = str(row.zone or "").strip() or "workspace"
        latest = by_zone_latest.get(zone)
        if latest is None or row.last_seen_at > latest:
            by_zone_latest[zone] = row.last_seen_at

    now = datetime.now(timezone.utc)
    degraded: list[dict] = []
    for zone, last_seen in by_zone_latest.items():
        age_seconds = max(0.0, (now - last_seen).total_seconds())
        if age_seconds <= float(stale_after_seconds):
            continue
        severity = max(0.4, min(1.0, age_seconds / float(max(stale_after_seconds, 1))))
        degraded.append(
            {
                "signal_type": "stale_zone_detected",
                "target_scope": zone,
                "age_seconds": round(age_seconds, 3),
                "severity": round(severity, 6),
            }
        )
    return degraded


async def run_environment_maintenance_cycle(
    *,
    actor: str,
    source: str,
    stale_after_seconds: int,
    max_strategies: int,
    max_actions: int,
    auto_execute: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceMaintenanceRun, list[WorkspaceMaintenanceAction], list[WorkspaceEnvironmentStrategy], int]:
    degraded = await _detect_stale_zones(stale_after_seconds=stale_after_seconds, db=db)

    conditions = [
        {
            "condition_type": "stale_scans",
            "target_scope": item["target_scope"],
            "severity": item["severity"],
            "occurrence_count": 1,
        }
        for item in degraded
    ]

    strategies = await generate_environment_strategies(
        actor=actor,
        source=source,
        observed_conditions=conditions,
        min_severity=0.0,
        max_strategies=max_strategies,
        metadata_json={
            "maintenance_cycle": True,
            "stale_after_seconds": stale_after_seconds,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
        user_id="operator",
    )

    run = WorkspaceMaintenanceRun(
        source=source,
        actor=actor,
        status="completed",
        detected_signals_json=degraded,
        created_strategy_ids_json=[int(item.id) for item in strategies],
        executed_action_ids_json=[],
        maintenance_outcomes_json={},
        stabilized=False,
        metadata_json={
            "auto_execute": bool(auto_execute),
            "max_actions": int(max_actions),
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
    )
    db.add(run)
    await db.flush()

    actions: list[WorkspaceMaintenanceAction] = []
    memory_entries_created = 0

    if auto_execute:
        now = datetime.now(timezone.utc)
        for strategy in strategies[: max(1, int(max_actions))]:
            zone = str(strategy.target_scope or "workspace").strip() or "workspace"

            action = WorkspaceMaintenanceAction(
                run_id=run.id,
                strategy_id=strategy.id,
                action_type="auto_execute_rescan",
                target_scope=zone,
                safety_mode="scan_only",
                status="succeeded",
                reason="maintenance_rescan_executed",
                details_json={
                    "strategy_id": strategy.id,
                    "strategy_type": strategy.strategy_type,
                    "zone": zone,
                },
            )
            db.add(action)
            await db.flush()
            actions.append(action)

            observation = WorkspaceObservation(
                observed_at=now,
                zone=zone,
                label=f"maintenance-refresh:{zone}",
                confidence=0.82,
                source="maintenance",
                execution_id=None,
                lifecycle_status="active",
                first_seen_at=now,
                last_seen_at=now,
                observation_count=1,
                metadata_json={
                    "maintenance_run_id": run.id,
                    "maintenance_action_id": action.id,
                    "maintenance_generated": True,
                },
            )
            db.add(observation)

            memory = MemoryEntry(
                memory_class="maintenance_outcome",
                content=f"Auto maintenance rescan executed for zone {zone}",
                summary="workspace maintenance cycle correction",
                metadata_json={
                    "maintenance_run_id": run.id,
                    "maintenance_action_id": action.id,
                    "zone": zone,
                    "strategy_id": strategy.id,
                },
            )
            db.add(memory)
            memory_entries_created += 1

            await record_decision(
                decision_type="maintenance_action",
                source_context={
                    "source": source,
                    "endpoint": "/maintenance/cycle",
                },
                relevant_state={
                    "signal": "stale_zone_detected",
                    "target_scope": zone,
                },
                preferences_applied={},
                constraints_applied=[{"constraint": "scan_only_safety_mode", "hard": False}],
                strategies_applied=[{"strategy_id": strategy.id, "strategy_type": strategy.strategy_type}],
                options_considered=[
                    {"option": "auto_execute_rescan", "target_scope": zone},
                    {"option": "defer_to_operator", "target_scope": zone},
                ],
                selected_option={
                    "option": "auto_execute_rescan",
                    "target_scope": zone,
                    "maintenance_action_id": action.id,
                },
                decision_reason="stale_zone_maintenance_rescan",
                confidence=0.8,
                result_quality=0.82,
                resulting_goal_or_plan_id=f"maintenance_run:{run.id}",
                metadata_json={
                    "maintenance_run_id": run.id,
                    "maintenance_action_id": action.id,
                },
                db=db,
            )

            await resolve_environment_strategy(
                row=strategy,
                status="stable",
                reason="maintenance_cycle_rescan_completed",
                metadata_json={
                    "maintenance_run_id": run.id,
                    "maintenance_action_id": action.id,
                },
            )

    run.executed_action_ids_json = [int(item.id) for item in actions]
    run.stabilized = bool(actions) or not bool(degraded)
    run.maintenance_outcomes_json = {
        "degraded_signal_count": len(degraded),
        "strategies_created": len(strategies),
        "actions_executed": len(actions),
        "memory_entries_created": memory_entries_created,
        "stabilized_zones": [str(item.target_scope or "workspace") for item in actions],
    }

    await db.flush()
    return run, actions, strategies, memory_entries_created


async def list_maintenance_runs(*, db: AsyncSession, limit: int = 50) -> list[WorkspaceMaintenanceRun]:
    rows = (
        await db.execute(
            select(WorkspaceMaintenanceRun).order_by(WorkspaceMaintenanceRun.id.desc())
        )
    ).scalars().all()
    return rows[: max(1, min(500, int(limit)))]


async def get_maintenance_run(*, run_id: int, db: AsyncSession) -> WorkspaceMaintenanceRun | None:
    return (
        await db.execute(
            select(WorkspaceMaintenanceRun).where(WorkspaceMaintenanceRun.id == run_id)
        )
    ).scalars().first()


async def list_maintenance_actions_for_run(*, run_id: int, db: AsyncSession) -> list[WorkspaceMaintenanceAction]:
    return (
        await db.execute(
            select(WorkspaceMaintenanceAction)
            .where(WorkspaceMaintenanceAction.run_id == run_id)
            .order_by(WorkspaceMaintenanceAction.id.asc())
        )
    ).scalars().all()


def to_maintenance_action_out(row: WorkspaceMaintenanceAction) -> dict:
    return {
        "action_id": row.id,
        "run_id": row.run_id,
        "strategy_id": row.strategy_id,
        "action_type": row.action_type,
        "target_scope": row.target_scope,
        "safety_mode": row.safety_mode,
        "status": row.status,
        "reason": row.reason,
        "details_json": row.details_json if isinstance(row.details_json, dict) else {},
        "created_at": row.created_at,
    }


def to_maintenance_run_out(row: WorkspaceMaintenanceRun, *, actions: list[WorkspaceMaintenanceAction] | None = None, strategies: list[WorkspaceEnvironmentStrategy] | None = None) -> dict:
    return {
        "run_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "detected_signals": row.detected_signals_json if isinstance(row.detected_signals_json, list) else [],
        "created_strategy_ids": row.created_strategy_ids_json if isinstance(row.created_strategy_ids_json, list) else [],
        "executed_action_ids": row.executed_action_ids_json if isinstance(row.executed_action_ids_json, list) else [],
        "maintenance_outcomes": row.maintenance_outcomes_json if isinstance(row.maintenance_outcomes_json, dict) else {},
        "stabilized": bool(row.stabilized),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
        "actions": [to_maintenance_action_out(item) for item in (actions or [])],
        "strategies": [to_environment_strategy_out(item) for item in (strategies or [])],
    }
