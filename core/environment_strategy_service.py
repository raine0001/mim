from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceEnvironmentStrategy


STRATEGY_STATUSES = {
    "active",
    "stable",
    "blocked",
    "completed",
    "superseded",
}


def _strategy_from_condition(condition: dict) -> dict | None:
    condition_type = str(condition.get("condition_type", "")).strip().lower()
    scope = str(condition.get("target_scope", "workspace")).strip() or "workspace"
    severity = max(0.0, min(1.0, float(condition.get("severity", 0.5) or 0.0)))
    occurrences = max(1, int(condition.get("occurrence_count", 1) or 1))

    if condition_type in {"stale_scans", "zone_stale", "scan_staleness"}:
        return {
            "strategy_type": "stabilize_zone",
            "target_scope": scope,
            "priority": "high" if severity >= 0.7 else "normal",
            "success_criteria": "zone freshness consistently below threshold",
            "status_reason": "repeated stale scans detected",
            "contributing_goal_keys_json": [f"refresh:{scope}", f"rescan:{scope}"],
            "influence_weight": max(0.45, severity),
            "evidence_json": {
                "condition_type": condition_type,
                "severity": severity,
                "occurrence_count": occurrences,
            },
            "metadata_json": {
                "influences_goal_types": ["workspace_refresh", "rescan", "observation_update"],
            },
        }

    if condition_type in {"identity_degradation", "object_uncertainty", "identity_drift"}:
        return {
            "strategy_type": "refresh_object_certainty",
            "target_scope": scope,
            "priority": "high" if severity >= 0.75 else "normal",
            "success_criteria": "target object confidence and identity consistency restored",
            "status_reason": "object identity certainty degrading",
            "contributing_goal_keys_json": ["rescan_target", "reconcile_identity"],
            "influence_weight": max(0.5, severity),
            "evidence_json": {
                "condition_type": condition_type,
                "severity": severity,
                "occurrence_count": occurrences,
            },
            "metadata_json": {
                "influences_goal_types": ["target_reacquire", "workspace_refresh", "directed_reach"],
            },
        }

    if condition_type in {"map_drift_replans", "future_drift_replans", "map_instability"}:
        return {
            "strategy_type": "restore_map_stability",
            "target_scope": scope,
            "priority": "high",
            "success_criteria": "replan rate from map drift remains below threshold",
            "status_reason": "repeated replans caused by map drift",
            "contributing_goal_keys_json": ["refresh_map", "validate_workspace_state"],
            "influence_weight": max(0.6, severity),
            "evidence_json": {
                "condition_type": condition_type,
                "severity": severity,
                "occurrence_count": occurrences,
            },
            "metadata_json": {
                "influences_goal_types": ["workspace_refresh", "map_stabilization", "rescan"],
            },
        }

    return None


async def generate_environment_strategies(
    *,
    actor: str,
    source: str,
    observed_conditions: list[dict],
    min_severity: float,
    max_strategies: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceEnvironmentStrategy]:
    candidates: list[dict] = []
    for condition in observed_conditions:
        severity = float(condition.get("severity", 0.0) or 0.0)
        if severity < min_severity:
            continue
        mapped = _strategy_from_condition(condition)
        if mapped:
            candidates.append(mapped)

    created: list[WorkspaceEnvironmentStrategy] = []
    for candidate in candidates[: max_strategies]:
        strategy_type = str(candidate.get("strategy_type", "")).strip()
        target_scope = str(candidate.get("target_scope", "workspace")).strip() or "workspace"

        existing = (
            await db.execute(
                select(WorkspaceEnvironmentStrategy)
                .where(WorkspaceEnvironmentStrategy.strategy_type == strategy_type)
                .where(WorkspaceEnvironmentStrategy.target_scope == target_scope)
                .where(WorkspaceEnvironmentStrategy.current_status.in_(["active", "blocked", "stable"]))
                .order_by(WorkspaceEnvironmentStrategy.id.desc())
            )
        ).scalars().first()
        if existing:
            continue

        row = WorkspaceEnvironmentStrategy(
            source=source,
            strategy_type=strategy_type,
            target_scope=target_scope,
            priority=str(candidate.get("priority", "normal")),
            current_status="active",
            success_criteria=str(candidate.get("success_criteria", "")),
            contributing_goal_keys_json=candidate.get("contributing_goal_keys_json", []),
            contributing_checkpoint_keys_json=candidate.get("contributing_checkpoint_keys_json", []),
            status_reason=str(candidate.get("status_reason", "")),
            evidence_json=candidate.get("evidence_json", {}),
            influence_weight=max(0.0, min(1.0, float(candidate.get("influence_weight", 0.5) or 0.0))),
            metadata_json={
                **(candidate.get("metadata_json", {}) if isinstance(candidate.get("metadata_json", {}), dict) else {}),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "actor": actor,
                "source": source,
            },
        )
        db.add(row)
        created.append(row)

    await db.flush()
    return created


async def list_environment_strategies(
    *,
    db: AsyncSession,
    status: str = "",
    limit: int = 50,
) -> list[WorkspaceEnvironmentStrategy]:
    rows = (
        await db.execute(
            select(WorkspaceEnvironmentStrategy).order_by(WorkspaceEnvironmentStrategy.id.desc())
        )
    ).scalars().all()
    if status:
        requested = status.strip().lower()
        rows = [item for item in rows if str(item.current_status).strip().lower() == requested]
    return rows[: max(1, min(limit, 500))]


async def get_environment_strategy(*, strategy_id: int, db: AsyncSession) -> WorkspaceEnvironmentStrategy | None:
    return (
        await db.execute(
            select(WorkspaceEnvironmentStrategy).where(WorkspaceEnvironmentStrategy.id == strategy_id)
        )
    ).scalars().first()


async def resolve_environment_strategy(
    *,
    row: WorkspaceEnvironmentStrategy,
    status: str,
    reason: str,
    metadata_json: dict,
) -> WorkspaceEnvironmentStrategy:
    next_status = str(status or "stable").strip().lower()
    if next_status not in STRATEGY_STATUSES:
        next_status = "stable"
    row.current_status = next_status
    row.status_reason = reason or row.status_reason
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    return row


async def deactivate_environment_strategy(
    *,
    row: WorkspaceEnvironmentStrategy,
    reason: str,
    metadata_json: dict,
) -> WorkspaceEnvironmentStrategy:
    row.current_status = "superseded"
    row.status_reason = reason or "deactivated"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "deactivated": True,
    }
    return row


async def get_active_environment_strategies(*, db: AsyncSession, limit: int = 25) -> list[WorkspaceEnvironmentStrategy]:
    return (
        await db.execute(
            select(WorkspaceEnvironmentStrategy)
            .where(WorkspaceEnvironmentStrategy.current_status == "active")
            .order_by(WorkspaceEnvironmentStrategy.influence_weight.desc(), WorkspaceEnvironmentStrategy.id.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).scalars().all()


def strategy_influence_for_goal(*, goal: dict, strategy: WorkspaceEnvironmentStrategy) -> tuple[float, str]:
    goal_type = str(goal.get("goal_type", "")).strip()
    goal_key = str(goal.get("goal_key", "")).strip()
    goal_scope = str((goal.get("metadata_json", {}) or {}).get("scope", "")).strip()
    strategy_scope = str(strategy.target_scope or "workspace").strip()
    strategy_types = (
        strategy.metadata_json.get("influences_goal_types", [])
        if isinstance(strategy.metadata_json, dict)
        else []
    )
    if not isinstance(strategy_types, list):
        strategy_types = []

    score = 0.0
    reason_parts: list[str] = []
    if strategy_scope and strategy_scope != "workspace" and strategy_scope == goal_scope:
        score += 0.18
        reason_parts.append("scope_match")
    if goal_type and goal_type in [str(item) for item in strategy_types]:
        score += 0.22
        reason_parts.append("goal_type_match")
    strategy_goal_keys = (
        strategy.contributing_goal_keys_json
        if isinstance(strategy.contributing_goal_keys_json, list)
        else []
    )
    if goal_key and goal_key in [str(item) for item in strategy_goal_keys]:
        score += 0.24
        reason_parts.append("goal_key_match")
    if strategy_scope == "workspace":
        score += 0.08
        reason_parts.append("workspace_scope")

    score *= max(0.0, min(1.0, float(strategy.influence_weight)))
    return score, ",".join(reason_parts)


async def mark_strategy_influenced_plan(
    *,
    strategy: WorkspaceEnvironmentStrategy,
    plan_id: int,
) -> None:
    existing = strategy.influenced_plan_ids_json if isinstance(strategy.influenced_plan_ids_json, list) else []
    if plan_id not in existing:
        strategy.influenced_plan_ids_json = [*existing, plan_id]


def to_environment_strategy_out(row: WorkspaceEnvironmentStrategy) -> dict:
    return {
        "strategy_id": row.id,
        "source": row.source,
        "strategy_type": row.strategy_type,
        "target_scope": row.target_scope,
        "priority": row.priority,
        "current_status": row.current_status,
        "success_criteria": row.success_criteria,
        "contributing_goals": row.contributing_goal_keys_json if isinstance(row.contributing_goal_keys_json, list) else [],
        "contributing_checkpoints": row.contributing_checkpoint_keys_json if isinstance(row.contributing_checkpoint_keys_json, list) else [],
        "status_reason": row.status_reason,
        "evidence": row.evidence_json if isinstance(row.evidence_json, dict) else {},
        "influence_weight": float(row.influence_weight),
        "influenced_plan_ids": row.influenced_plan_ids_json if isinstance(row.influenced_plan_ids_json, list) else [],
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
