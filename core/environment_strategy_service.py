from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import CapabilityExecution, WorkspaceEnvironmentStrategy, WorkspaceObservation, WorkspaceTargetResolution
from core.preferences import DEFAULT_USER_ID, get_user_preference_value


STRATEGY_STATUSES = {
    "active",
    "stable",
    "blocked",
    "completed",
    "superseded",
}


def _strategy_from_condition(condition: dict, preference_context: dict) -> dict | None:
    condition_type = str(condition.get("condition_type", "")).strip().lower()
    scope = str(condition.get("target_scope", "workspace")).strip() or "workspace"
    severity = max(0.0, min(1.0, float(condition.get("severity", 0.5) or 0.0)))
    occurrences = max(1, int(condition.get("occurrence_count", 1) or 1))

    prefer_auto_refresh = bool(preference_context.get("prefer_auto_refresh_scans", False))
    prefer_minimal_interruptions = bool(preference_context.get("prefer_minimal_interruption", False))
    preferred_scan_zones = preference_context.get("preferred_scan_zones", []) if isinstance(preference_context.get("preferred_scan_zones", []), list) else []
    zone_preferred = scope in [str(item) for item in preferred_scan_zones]

    preference_adjustments = {
        "prefer_auto_refresh_scans": prefer_auto_refresh,
        "prefer_minimal_interruption": prefer_minimal_interruptions,
        "zone_preferred": zone_preferred,
    }

    if condition_type in {"stale_scans", "zone_stale", "scan_staleness"}:
        adjusted_influence = max(0.45, severity)
        adjusted_priority = "high" if severity >= 0.7 else "normal"
        if prefer_auto_refresh:
            adjusted_influence = min(1.0, adjusted_influence + 0.12)
            if severity >= 0.55:
                adjusted_priority = "high"
        if zone_preferred:
            adjusted_influence = min(1.0, adjusted_influence + 0.1)

        return {
            "strategy_type": "stabilize_zone",
            "target_scope": scope,
            "priority": adjusted_priority,
            "success_criteria": "zone freshness consistently below threshold",
            "status_reason": "repeated stale scans detected",
            "contributing_goal_keys_json": [f"refresh:{scope}", f"rescan:{scope}"],
            "influence_weight": adjusted_influence,
            "evidence_json": {
                "condition_type": condition_type,
                "severity": severity,
                "occurrence_count": occurrences,
            },
            "metadata_json": {
                "influences_goal_types": ["workspace_refresh", "rescan", "observation_update"],
                "preference_adjustments": preference_adjustments,
                "avoid_speech_output": prefer_minimal_interruptions,
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
                "preference_adjustments": preference_adjustments,
                "avoid_speech_output": prefer_minimal_interruptions,
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
                "preference_adjustments": preference_adjustments,
                "avoid_speech_output": prefer_minimal_interruptions,
            },
        }

    if condition_type in {"routine_zone_pattern", "routine_scan_zone"}:
        adjusted_influence = max(0.5, severity)
        if zone_preferred or prefer_auto_refresh:
            adjusted_influence = min(1.0, adjusted_influence + 0.12)
        return {
            "strategy_type": "preemptive_zone_stabilization",
            "target_scope": scope,
            "priority": "high" if severity >= 0.65 or zone_preferred else "normal",
            "success_criteria": "routine zone remains ready before expected usage window",
            "status_reason": "routine pattern indicates recurring zone instability",
            "contributing_goal_keys_json": [f"refresh:{scope}", f"observe:{scope}"],
            "influence_weight": adjusted_influence,
            "evidence_json": {
                "condition_type": condition_type,
                "severity": severity,
                "occurrence_count": occurrences,
                "routine_window": condition.get("routine_window", ""),
            },
            "metadata_json": {
                "influences_goal_types": ["workspace_refresh", "rescan", "observation_update"],
                "preference_adjustments": preference_adjustments,
                "avoid_speech_output": prefer_minimal_interruptions,
                "routine_generated": True,
            },
        }

    return None


async def _load_preference_context(*, db: AsyncSession, user_id: str) -> dict:
    preferred_scan_zones = await get_user_preference_value(
        db=db,
        preference_type="preferred_scan_zones",
        user_id=user_id,
    )
    prefer_auto_refresh = await get_user_preference_value(
        db=db,
        preference_type="prefer_auto_refresh_scans",
        user_id=user_id,
    )
    prefer_minimal_interruption = await get_user_preference_value(
        db=db,
        preference_type="prefer_minimal_interruption",
        user_id=user_id,
    )

    return {
        "preferred_scan_zones": preferred_scan_zones if isinstance(preferred_scan_zones, list) else [],
        "prefer_auto_refresh_scans": bool(prefer_auto_refresh),
        "prefer_minimal_interruption": bool(prefer_minimal_interruption),
    }


async def generate_environment_strategies(
    *,
    actor: str,
    source: str,
    observed_conditions: list[dict],
    min_severity: float,
    max_strategies: int,
    metadata_json: dict,
    db: AsyncSession,
    user_id: str = DEFAULT_USER_ID,
) -> list[WorkspaceEnvironmentStrategy]:
    preference_context = await _load_preference_context(db=db, user_id=user_id)

    min_required_severity = float(min_severity)
    if bool(preference_context.get("prefer_auto_refresh_scans", False)):
        min_required_severity = max(0.0, min_required_severity - 0.15)

    candidates: list[dict] = []
    for condition in observed_conditions:
        severity = float(condition.get("severity", 0.0) or 0.0)
        if severity < min_required_severity:
            continue
        mapped = _strategy_from_condition(condition, preference_context)
        if mapped:
            candidates.append(mapped)

    created: list[WorkspaceEnvironmentStrategy] = []
    for candidate in candidates:
        if len(created) >= max(1, int(max_strategies)):
            break
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
                "user_id": user_id,
                "preference_context": preference_context,
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


async def generate_environment_strategies_from_routines(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_occurrence_count: int,
    max_strategies: int,
    metadata_json: dict,
    db: AsyncSession,
    user_id: str = DEFAULT_USER_ID,
) -> list[WorkspaceEnvironmentStrategy]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))

    observation_rows = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.observed_at >= since)
            .order_by(WorkspaceObservation.id.desc())
            .limit(5000)
        )
    ).scalars().all()

    zone_counts: dict[str, int] = {}
    for row in observation_rows:
        zone = str(row.zone or "").strip()
        if not zone:
            continue
        zone_counts[zone] = zone_counts.get(zone, 0) + 1

    recent_scan_rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.capability_name == "workspace_scan")
            .where(CapabilityExecution.created_at >= since)
            .order_by(CapabilityExecution.id.desc())
            .limit(2000)
        )
    ).scalars().all()
    for row in recent_scan_rows:
        feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
        args = row.arguments_json if isinstance(row.arguments_json, dict) else {}
        observations = feedback.get("observations") if isinstance(feedback.get("observations"), list) else []
        if observations:
            for item in observations:
                if not isinstance(item, dict):
                    continue
                zone = str(item.get("zone") or args.get("scan_area") or "workspace").strip() or "workspace"
                zone_counts[zone] = zone_counts.get(zone, 0) + 1
            continue
        zone = str(args.get("scan_area") or "").strip()
        if zone:
            zone_counts[zone] = zone_counts.get(zone, 0) + 1

    target_rows = (
        await db.execute(
            select(
                WorkspaceTargetResolution.requested_zone,
                func.count(WorkspaceTargetResolution.id),
            )
            .where(WorkspaceTargetResolution.created_at >= since)
            .group_by(WorkspaceTargetResolution.requested_zone)
            .order_by(func.count(WorkspaceTargetResolution.id).desc())
            .limit(30)
        )
    ).all()

    conditions: list[dict] = []
    for zone, count in sorted(zone_counts.items(), key=lambda item: item[1], reverse=True):
        if count < max(2, min_occurrence_count):
            continue
        severity = max(0.4, min(1.0, float(count) / float(max(min_occurrence_count, 10))))
        conditions.append(
            {
                "condition_type": "routine_scan_zone",
                "target_scope": zone,
                "severity": severity,
                "occurrence_count": count,
                "routine_window": f"last_{lookback_hours}h",
            }
        )

    for zone, count in target_rows:
        normalized_zone = str(zone or "").strip() or "workspace"
        if int(count or 0) < max(2, min_occurrence_count):
            continue
        severity = max(0.45, min(1.0, float(count) / float(max(min_occurrence_count, 10))))
        conditions.append(
            {
                "condition_type": "routine_zone_pattern",
                "target_scope": normalized_zone,
                "severity": severity,
                "occurrence_count": int(count or 0),
                "routine_window": f"targets_last_{lookback_hours}h",
            }
        )

    return await generate_environment_strategies(
        actor=actor,
        source=source,
        observed_conditions=conditions,
        min_severity=0.4,
        max_strategies=max_strategies,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "routine_generation": True,
            "lookback_hours": lookback_hours,
            "min_occurrence_count": min_occurrence_count,
        },
        db=db,
        user_id=user_id,
    )


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

    preference_adjustments = (
        strategy.metadata_json.get("preference_adjustments", {})
        if isinstance(strategy.metadata_json, dict)
        else {}
    )
    if isinstance(preference_adjustments, dict) and bool(preference_adjustments.get("zone_preferred", False)):
        score += 0.12
        reason_parts.append("preference_zone_boost")
    if isinstance(preference_adjustments, dict) and bool(preference_adjustments.get("prefer_auto_refresh_scans", False)):
        if goal_type in {"workspace_refresh", "rescan", "observation_update"}:
            score += 0.1
            reason_parts.append("preference_auto_refresh")

    score *= max(0.0, min(1.0, float(strategy.influence_weight)))
    priority_multiplier = {
        "critical": 1.2,
        "high": 1.1,
        "normal": 1.0,
        "low": 0.85,
    }.get(str(strategy.priority or "normal").strip().lower(), 1.0)
    score *= priority_multiplier
    if priority_multiplier > 1.0:
        reason_parts.append("strategy_priority_boost")
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
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    preference_adjustments = metadata.get("preference_adjustments", {}) if isinstance(metadata.get("preference_adjustments", {}), dict) else {}
    priority_base = {
        "critical": 1.0,
        "high": 0.85,
        "normal": 0.6,
        "low": 0.35,
    }
    priority_weight = round(
        float(priority_base.get(str(row.priority).strip().lower(), 0.6)) * float(row.influence_weight),
        6,
    )

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
        "strategy_reason": row.status_reason,
        "evidence": row.evidence_json if isinstance(row.evidence_json, dict) else {},
        "environment_signals": row.evidence_json if isinstance(row.evidence_json, dict) else {},
        "preference_adjustments": preference_adjustments,
        "priority_weight": priority_weight,
        "influence_weight": float(row.influence_weight),
        "influenced_plan_ids": row.influenced_plan_ids_json if isinstance(row.influenced_plan_ids_json, list) else [],
        "metadata_json": metadata,
        "created_at": row.created_at,
    }
