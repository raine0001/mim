from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_governance_service import latest_execution_truth_governance_snapshot
from core.execution_truth_service import derive_execution_truth_signals
from core.improvement_governance_service import list_improvement_backlog
from core.maintenance_service import run_environment_maintenance_cycle
from core.operator_preference_convergence_service import (
    learned_preference_stewardship_weight_delta,
    latest_scope_learned_preference,
)
from core.policy_conflict_resolution_service import resolve_stewardship_policy_conflict
from core.proposal_arbitration_learning_service import workspace_proposal_arbitration_family_influence
from core.operator_resolution_service import commitment_downstream_effects, commitment_snapshot, latest_active_operator_resolution_commitment
from core.models import (
    CapabilityExecution,
    UserPreference,
    WorkspaceAutonomyBoundaryProfile,
    WorkspaceConceptMemory,
    WorkspaceDesiredEnvironmentState,
    WorkspaceDevelopmentPattern,
    WorkspaceObjectMemory,
    WorkspaceObservation,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
    WorkspaceStrategyGoal,
)


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object, *, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _safe_float(value: object, *, fallback: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _default_target_state(*, stale_after_seconds: int) -> dict:
    return {
        "zone_freshness_seconds": int(max(60, stale_after_seconds)),
        "critical_object_confidence": 0.75,
        "max_unstable_regions": 0,
        "max_degraded_zones": 0,
        "max_zone_uncertainty_score": 0.35,
        "max_object_uncertainty_score": 0.4,
        "max_zone_drift_rate": 0.35,
        "max_system_drift_rate": 0.3,
        "max_missing_key_objects": 0,
        "key_objects": [],
        "proactive_drift_monitoring": True,
        "intervention_policy": {
            "max_interventions_per_window": 3,
            "window_minutes": 60,
            "scope_cooldown_seconds": 600,
            "per_strategy_limit": 2,
        },
    }


def _normalized_target_state(
    *, stale_after_seconds: int, overrides: dict | None
) -> dict:
    target = {
        **_default_target_state(stale_after_seconds=stale_after_seconds),
        **(overrides if isinstance(overrides, dict) else {}),
    }
    target["zone_freshness_seconds"] = max(
        60,
        _safe_int(target.get("zone_freshness_seconds"), fallback=stale_after_seconds),
    )
    target["critical_object_confidence"] = _bounded(
        _safe_float(target.get("critical_object_confidence"), fallback=0.75)
    )
    target["max_unstable_regions"] = max(
        0, _safe_int(target.get("max_unstable_regions"), fallback=0)
    )
    target["max_degraded_zones"] = max(
        0, _safe_int(target.get("max_degraded_zones"), fallback=0)
    )
    target["max_zone_uncertainty_score"] = _bounded(
        _safe_float(target.get("max_zone_uncertainty_score"), fallback=0.35)
    )
    target["max_object_uncertainty_score"] = _bounded(
        _safe_float(target.get("max_object_uncertainty_score"), fallback=0.4)
    )
    target["max_zone_drift_rate"] = _bounded(
        _safe_float(target.get("max_zone_drift_rate"), fallback=0.35)
    )
    target["max_system_drift_rate"] = _bounded(
        _safe_float(target.get("max_system_drift_rate"), fallback=0.3)
    )
    target["max_missing_key_objects"] = max(
        0, _safe_int(target.get("max_missing_key_objects"), fallback=0)
    )
    target["key_objects"] = [
        str(item).strip()
        for item in (
            target.get("key_objects")
            if isinstance(target.get("key_objects"), list)
            else []
        )
        if str(item).strip()
    ]
    target["proactive_drift_monitoring"] = bool(
        target.get("proactive_drift_monitoring", True)
    )
    policy = (
        target.get("intervention_policy", {})
        if isinstance(target.get("intervention_policy", {}), dict)
        else {}
    )
    target["intervention_policy"] = {
        "max_interventions_per_window": max(
            1, _safe_int(policy.get("max_interventions_per_window"), fallback=3)
        ),
        "window_minutes": max(1, _safe_int(policy.get("window_minutes"), fallback=60)),
        "scope_cooldown_seconds": max(
            0, _safe_int(policy.get("scope_cooldown_seconds"), fallback=600)
        ),
        "per_strategy_limit": max(
            1, _safe_int(policy.get("per_strategy_limit"), fallback=2)
        ),
    }
    return target


def _scope_type(*, managed_scope: str) -> str:
    scope = str(managed_scope or "").strip() or "global"
    if scope == "global":
        return "workspace"
    return "zone"


def _executed_intervention(row: WorkspaceStewardshipCycle) -> bool:
    selected_actions = (
        row.selected_actions_json if isinstance(row.selected_actions_json, list) else []
    )
    for item in selected_actions:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("action_type", "")).strip() == "maintenance_cycle"
            and int(item.get("actions_executed", 0) or 0) > 0
        ):
            return True
    verification = (
        row.metadata_json if isinstance(row.metadata_json, dict) else {}
    ).get("verification", {})
    if (
        isinstance(verification, dict)
        and int(verification.get("actions_executed", 0) or 0) > 0
    ):
        return True
    return False


async def _get_or_create_desired_state(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    target_state: dict,
    priority_score: float,
    origin_strategy_goal_id: int | None,
    strategy_types: list[str],
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceDesiredEnvironmentState:
    existing = (
        (
            await db.execute(
                select(WorkspaceDesiredEnvironmentState)
                .where(WorkspaceDesiredEnvironmentState.scope_ref == managed_scope)
                .where(WorkspaceDesiredEnvironmentState.status == "active")
                .order_by(WorkspaceDesiredEnvironmentState.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    strategy_link = {
        "origin_strategy_goal_id": origin_strategy_goal_id,
        "strategy_types": strategy_types,
    }
    created_from = (
        f"strategy_goal:{origin_strategy_goal_id}"
        if origin_strategy_goal_id is not None
        else "stewardship_cycle"
    )
    if existing:
        existing.target_conditions_json = target_state
        existing.priority_score = _bounded(priority_score)
        existing.origin_strategy_goal_id = origin_strategy_goal_id
        existing.strategy_link_json = strategy_link
        existing.created_from = created_from
        existing.metadata_json = {
            **(
                existing.metadata_json
                if isinstance(existing.metadata_json, dict)
                else {}
            ),
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        }
        return existing

    row = WorkspaceDesiredEnvironmentState(
        source=source,
        actor=actor,
        status="active",
        scope_type=_scope_type(managed_scope=managed_scope),
        scope_ref=managed_scope,
        target_conditions_json=target_state,
        priority_score=_bounded(priority_score),
        origin_strategy_goal_id=origin_strategy_goal_id,
        strategy_link_json=strategy_link,
        created_from=created_from,
        metadata_json={**(metadata_json if isinstance(metadata_json, dict) else {})},
    )
    db.add(row)
    await db.flush()
    return row


async def _intervention_throttle_state(
    *,
    stewardship_id: int,
    managed_scope: str,
    target_state: dict,
    strategy_types: list[str],
    db: AsyncSession,
) -> dict:
    policy = (
        target_state.get("intervention_policy", {})
        if isinstance(target_state.get("intervention_policy", {}), dict)
        else {}
    )
    recent_cycles = await _recent_scope_cycles(
        stewardship_id=stewardship_id,
        managed_scope=managed_scope,
        db=db,
        limit=50,
    )
    now = datetime.now(timezone.utc)
    window_minutes = max(1, int(policy.get("window_minutes", 60) or 60))
    window_start = now - timedelta(minutes=window_minutes)
    executed_cycles = [row for row in recent_cycles if _executed_intervention(row)]
    executed_in_window = [
        row
        for row in executed_cycles
        if (_as_utc(row.created_at) or now) >= window_start
    ]
    last_executed = max(
        (
            _as_utc(row.created_at)
            for row in executed_cycles
            if _as_utc(row.created_at) is not None
        ),
        default=None,
    )
    cooldown_seconds = max(0, int(policy.get("scope_cooldown_seconds", 600) or 600))
    cooldown_active = False
    cooldown_remaining_seconds = 0
    if last_executed is not None and cooldown_seconds > 0:
        elapsed = max(0.0, (now - last_executed).total_seconds())
        cooldown_active = elapsed < cooldown_seconds
        if cooldown_active:
            cooldown_remaining_seconds = max(0, int(round(cooldown_seconds - elapsed)))

    current_strategy_types = {
        str(item).strip() for item in strategy_types if str(item).strip()
    }
    strategy_hits = 0
    for row in executed_in_window:
        integration = (
            row.integration_evidence_json
            if isinstance(row.integration_evidence_json, dict)
            else {}
        )
        prior_types = {
            str(item).strip()
            for item in (
                integration.get("strategy_types", [])
                if isinstance(integration.get("strategy_types", []), list)
                else []
            )
            if str(item).strip()
        }
        if current_strategy_types and current_strategy_types.intersection(prior_types):
            strategy_hits += 1

    reasons: list[str] = []
    max_interventions = max(1, int(policy.get("max_interventions_per_window", 3) or 3))
    per_strategy_limit = max(1, int(policy.get("per_strategy_limit", 2) or 2))
    if len(executed_in_window) >= max_interventions:
        reasons.append("max_interventions_per_window_exceeded")
    if cooldown_active:
        reasons.append("scope_cooldown_active")
    if strategy_hits >= per_strategy_limit:
        reasons.append("per_strategy_limit_exceeded")

    return {
        "allowed": not reasons,
        "reasons": reasons,
        "policy": {
            "max_interventions_per_window": max_interventions,
            "window_minutes": window_minutes,
            "scope_cooldown_seconds": cooldown_seconds,
            "per_strategy_limit": per_strategy_limit,
        },
        "executed_interventions_in_window": len(executed_in_window),
        "scope_cooldown_active": cooldown_active,
        "scope_cooldown_remaining_seconds": cooldown_remaining_seconds,
        "strategy_intervention_count": strategy_hits,
        "last_intervention_at": last_executed.isoformat()
        if last_executed is not None
        else None,
    }


def _scope_matches(*, managed_scope: str, zone: str) -> bool:
    scope = str(managed_scope or "").strip() or "global"
    if scope == "global":
        return True
    return str(zone or "").strip() == scope


def _execution_scope_refs(row: CapabilityExecution) -> set[str]:
    refs: set[str] = set()
    arguments = row.arguments_json if isinstance(row.arguments_json, dict) else {}
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
    correlation = (
        feedback.get("correlation_json", {})
        if isinstance(feedback.get("correlation_json", {}), dict)
        else {}
    )

    def _collect(value: object) -> None:
        text = str(value or "").strip()
        if text:
            refs.add(text)

    for payload in (arguments, feedback, correlation):
        if not isinstance(payload, dict):
            continue
        for key in ("managed_scope", "target_scope", "scope", "zone", "scan_area"):
            _collect(payload.get(key))

    observations = (
        feedback.get("observations", [])
        if isinstance(feedback.get("observations", []), list)
        else []
    )
    for item in observations:
        if not isinstance(item, dict):
            continue
        _collect(item.get("zone"))

    return refs


def _execution_scope_matches(*, managed_scope: str, row: CapabilityExecution) -> bool:
    scope = str(managed_scope or "").strip() or "global"
    if scope == "global":
        return True
    return scope in _execution_scope_refs(row)


async def _recent_execution_truth_rows(
    *, managed_scope: str, lookback_hours: int, db: AsyncSession
) -> list[CapabilityExecution]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.created_at >= since)
                .order_by(CapabilityExecution.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        row
        for row in rows
        if isinstance(row.execution_truth_json, dict)
        and str(row.execution_truth_json.get("contract", "")).strip()
        == "execution_truth_v1"
        and _execution_scope_matches(managed_scope=managed_scope, row=row)
    ]


async def _execution_truth_assessment(
    *, managed_scope: str, lookback_hours: int, db: AsyncSession
) -> dict:
    rows = await _recent_execution_truth_rows(
        managed_scope=managed_scope,
        lookback_hours=lookback_hours,
        db=db,
    )
    signals: list[dict] = []
    signal_types: list[str] = []
    seen_types: set[str] = set()

    for row in rows:
        truth = row.execution_truth_json if isinstance(row.execution_truth_json, dict) else {}
        scope_refs = sorted(_execution_scope_refs(row))
        for signal in derive_execution_truth_signals(truth):
            if not isinstance(signal, dict):
                continue
            signal_type = str(signal.get("signal_type", "")).strip()
            if not signal_type:
                continue
            if signal_type not in seen_types:
                seen_types.add(signal_type)
                signal_types.append(signal_type)
            original_target_scope = str(signal.get("target_scope", "")).strip()
            target_scope = str(managed_scope or "").strip()
            if not target_scope:
                target_scope = original_target_scope or (scope_refs[0] if scope_refs else "global")
            signals.append(
                {
                    **signal,
                    "execution_id": int(row.id),
                    "capability_name": str(row.capability_name or "").strip(),
                    "target_scope": target_scope,
                    "execution_target_scope": original_target_scope,
                    "scope_refs": scope_refs,
                }
            )

    return {
        "execution_count": len(rows),
        "signal_count": len(signals),
        "signal_types": signal_types,
        "signals": signals,
    }


async def _latest_boundary(db: AsyncSession) -> WorkspaceAutonomyBoundaryProfile | None:
    return (
        (
            await db.execute(
                select(WorkspaceAutonomyBoundaryProfile)
                .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _recent_strategy_goals(
    *, lookback_hours: int, db: AsyncSession
) -> list[WorkspaceStrategyGoal]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        (
            await db.execute(
                select(WorkspaceStrategyGoal)
                .where(WorkspaceStrategyGoal.created_at >= since)
                .order_by(WorkspaceStrategyGoal.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )


async def _recent_concepts(
    *, lookback_hours: int, db: AsyncSession
) -> list[WorkspaceConceptMemory]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        (
            await db.execute(
                select(WorkspaceConceptMemory)
                .where(WorkspaceConceptMemory.created_at >= since)
                .where(WorkspaceConceptMemory.status == "active")
                .order_by(WorkspaceConceptMemory.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )


async def _recent_patterns(
    *, lookback_hours: int, db: AsyncSession
) -> list[WorkspaceDevelopmentPattern]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    return (
        (
            await db.execute(
                select(WorkspaceDevelopmentPattern)
                .where(WorkspaceDevelopmentPattern.last_seen_at >= since)
                .where(WorkspaceDevelopmentPattern.status == "active")
                .order_by(WorkspaceDevelopmentPattern.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )


async def _preference_weight(*, db: AsyncSession, managed_scope: str = "global") -> float:
    rows = (
        (
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
        )
        .scalars()
        .all()
    )

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
    learned_preference = await latest_scope_learned_preference(
        managed_scope=managed_scope,
        db=db,
        operator_commitment=await latest_active_operator_resolution_commitment(
            scope=managed_scope,
            db=db,
        ),
    )
    weight = _bounded(weight + learned_preference_stewardship_weight_delta(preference=learned_preference))
    return _bounded(weight)


async def _load_scope_observations(
    *, managed_scope: str, db: AsyncSession
) -> list[WorkspaceObservation]:
    rows = (
        (
            await db.execute(
                select(WorkspaceObservation)
                .where(WorkspaceObservation.lifecycle_status != "superseded")
                .order_by(
                    WorkspaceObservation.last_seen_at.desc(),
                    WorkspaceObservation.id.desc(),
                )
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )
    return [
        row
        for row in rows
        if _scope_matches(managed_scope=managed_scope, zone=str(row.zone or "").strip())
    ]


async def _load_scope_objects(
    *, managed_scope: str, db: AsyncSession
) -> list[WorkspaceObjectMemory]:
    rows = (
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    return [
        row
        for row in rows
        if _scope_matches(managed_scope=managed_scope, zone=str(row.zone or "").strip())
    ]


async def _recent_scope_cycles(
    *,
    stewardship_id: int | None,
    managed_scope: str,
    db: AsyncSession,
    limit: int = 5,
) -> list[WorkspaceStewardshipCycle]:
    stmt = select(WorkspaceStewardshipCycle).order_by(
        WorkspaceStewardshipCycle.id.desc()
    )
    if stewardship_id is not None:
        stmt = stmt.where(
            WorkspaceStewardshipCycle.stewardship_id == int(stewardship_id)
        )
    rows = (await db.execute(stmt.limit(max(1, min(limit, 25))))).scalars().all()
    return [
        row
        for row in rows
        if str(
            (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                "managed_scope", ""
            )
        ).strip()
        in {"", str(managed_scope or "").strip()}
    ]


def _latest_observation_per_zone(
    rows: list[WorkspaceObservation],
) -> dict[str, WorkspaceObservation]:
    latest: dict[str, WorkspaceObservation] = {}
    for row in rows:
        zone = str(row.zone or "").strip() or "workspace"
        current = latest.get(zone)
        if current is None or _as_utc(row.last_seen_at) > _as_utc(current.last_seen_at):
            latest[zone] = row
    return latest


def _object_lookup(
    rows: list[WorkspaceObjectMemory],
) -> dict[str, WorkspaceObjectMemory]:
    lookup: dict[str, WorkspaceObjectMemory] = {}
    for row in rows:
        name = str(row.canonical_name or "").strip().lower()
        if name and name not in lookup:
            lookup[name] = row
    return lookup


def _object_drift_rate(row: WorkspaceObjectMemory) -> float:
    location_history = (
        row.location_history if isinstance(row.location_history, list) else []
    )
    movement_factor = min(1.0, max(0, len(location_history) - 1) / 4.0)
    status = str(row.status or "").strip().lower()
    status_factor = 0.0
    if status in {"missing", "stale"}:
        status_factor = 0.8
    elif status not in {"active", ""}:
        status_factor = 0.4
    return _bounded((movement_factor * 0.6) + (status_factor * 0.4))


def _object_metrics(
    *,
    row: WorkspaceObjectMemory,
    target_state: dict,
    now: datetime,
) -> dict:
    last_seen = _as_utc(row.last_seen_at) or now
    age_seconds = max(0.0, (now - last_seen).total_seconds())
    freshness_limit = float(target_state.get("zone_freshness_seconds", 900) or 900)
    confidence_threshold = float(
        target_state.get("critical_object_confidence", 0.75) or 0.75
    )
    age_penalty = _bounded(age_seconds / max(freshness_limit * 2.0, 1.0))
    confidence_gap = _bounded(
        max(0.0, confidence_threshold - float(row.confidence or 0.0))
        / max(confidence_threshold, 0.01)
    )
    status = str(row.status or "active").strip().lower()
    status_penalty = 0.0
    if status == "missing":
        status_penalty = 1.0
    elif status == "stale":
        status_penalty = 0.8
    elif status != "active":
        status_penalty = 0.45
    drift_rate = _object_drift_rate(row)
    uncertainty_score = _bounded(
        (confidence_gap * 0.45) + (age_penalty * 0.35) + (status_penalty * 0.2)
    )
    stability_score = _bounded(1.0 - ((uncertainty_score * 0.7) + (drift_rate * 0.3)))
    return {
        "object_memory_id": int(row.id),
        "canonical_name": row.canonical_name,
        "zone": row.zone,
        "status": row.status,
        "confidence": round(float(row.confidence or 0.0), 6),
        "age_seconds": round(age_seconds, 3),
        "stability_score": round(stability_score, 6),
        "uncertainty_score": round(uncertainty_score, 6),
        "drift_rate": round(drift_rate, 6),
        "is_known": bool(
            float(row.confidence or 0.0) >= confidence_threshold
            and status == "active"
            and age_seconds <= (freshness_limit * 2.0)
        ),
        "movement_history_count": len(row.location_history)
        if isinstance(row.location_history, list)
        else 0,
    }


def _zone_metrics(
    *,
    zone: str,
    latest_observation: WorkspaceObservation | None,
    zone_objects: list[WorkspaceObjectMemory],
    target_state: dict,
    now: datetime,
) -> dict:
    freshness_limit = float(target_state.get("zone_freshness_seconds", 900) or 900)
    if latest_observation is None:
        age_seconds = freshness_limit * 2.0
        avg_confidence = 0.0
    else:
        last_seen = _as_utc(latest_observation.last_seen_at) or now
        age_seconds = max(0.0, (now - last_seen).total_seconds())
        avg_confidence = float(latest_observation.confidence or 0.0)

    stale_penalty = _bounded(age_seconds / max(freshness_limit, 1.0))
    confidence_penalty = _bounded(1.0 - avg_confidence)
    object_metrics = [
        _object_metrics(row=row, target_state=target_state, now=now)
        for row in zone_objects
    ]
    object_uncertainty = (
        sum(float(item.get("uncertainty_score", 0.0)) for item in object_metrics)
        / float(len(object_metrics))
        if object_metrics
        else 0.0
    )
    object_drift = (
        sum(float(item.get("drift_rate", 0.0)) for item in object_metrics)
        / float(len(object_metrics))
        if object_metrics
        else 0.0
    )
    uncertainty_score = _bounded(
        (stale_penalty * 0.45)
        + (confidence_penalty * 0.2)
        + (object_uncertainty * 0.35)
    )
    drift_rate = _bounded((stale_penalty * 0.4) + (object_drift * 0.6))
    stability_score = _bounded(1.0 - ((uncertainty_score * 0.7) + (drift_rate * 0.3)))
    degraded = bool(
        stale_penalty >= 1.0
        or uncertainty_score
        > float(target_state.get("max_zone_uncertainty_score", 0.35) or 0.35)
        or drift_rate > float(target_state.get("max_zone_drift_rate", 0.35) or 0.35)
    )
    return {
        "zone": zone,
        "observation_count": 1 if latest_observation is not None else 0,
        "object_count": len(zone_objects),
        "latest_label": str(latest_observation.label or "").strip()
        if latest_observation
        else "",
        "age_seconds": round(age_seconds, 3),
        "avg_confidence": round(avg_confidence, 6),
        "stability_score": round(stability_score, 6),
        "uncertainty_score": round(uncertainty_score, 6),
        "drift_rate": round(drift_rate, 6),
        "degraded": degraded,
    }


def _inquiry_candidates(*, deviation_signals: list[dict]) -> list[dict]:
    candidates: list[dict] = []
    for signal in deviation_signals:
        signal_type = str(signal.get("signal_type", "")).strip()
        if signal_type == "persistent_zone_degradation":
            zone = str(signal.get("target_scope", "workspace")).strip() or "workspace"
            candidates.append(
                {
                    "trigger": signal_type,
                    "question": f"Why does zone '{zone}' keep degrading across stewardship cycles?",
                    "why": "repeated degradation suggests the current maintenance strategy is not stabilizing the environment",
                }
            )
        elif signal_type == "key_object_unknown":
            name = (
                str(signal.get("object_name", "that object")).strip() or "that object"
            )
            candidates.append(
                {
                    "trigger": signal_type,
                    "question": f"Why does key object '{name}' keep dropping out of known state?",
                    "why": "persistent object uncertainty may need different tracking or operator input",
                }
            )
        elif signal_type in {
            "execution_slower_than_expected",
            "retry_instability_detected",
            "fallback_path_used",
            "simulation_reality_mismatch",
            "environment_shift_during_execution",
        }:
            capability_name = (
                str(signal.get("capability_name", "that capability")).strip()
                or "that capability"
            )
            target_scope = (
                str(signal.get("target_scope", "workspace")).strip() or "workspace"
            )
            prompts = {
                "execution_slower_than_expected": (
                    f"Why are executions for '{capability_name}' in scope '{target_scope}' running slower than expected?",
                    "runtime latency drift may mean stewardship should tighten monitoring before trusting the current environment state",
                ),
                "retry_instability_detected": (
                    f"Why does '{capability_name}' keep needing retries in scope '{target_scope}'?",
                    "repeated retries suggest the environment or execution path is unstable enough to warrant stewardship follow-up",
                ),
                "fallback_path_used": (
                    f"Why is '{capability_name}' falling back during execution in scope '{target_scope}'?",
                    "fallback dependence can hide degraded runtime conditions from the normal stewardship loop",
                ),
                "simulation_reality_mismatch": (
                    f"Why is execution reality for '{capability_name}' diverging from the simulated path in scope '{target_scope}'?",
                    "simulation mismatch indicates stewardship may be reasoning over stale environment assumptions",
                ),
                "environment_shift_during_execution": (
                    f"Why is the environment shifting during '{capability_name}' execution in scope '{target_scope}'?",
                    "runtime environment shifts should be visible to stewardship before they become chronic drift",
                ),
            }
            question, why = prompts[signal_type]
            candidates.append(
                {
                    "trigger": signal_type,
                    "question": question,
                    "why": why,
                }
            )
    return candidates


def _followup_trigger_related_proposal_types(trigger_type: str) -> list[str]:
    mapping = {
        "persistent_zone_degradation": ["rescan_zone", "monitor_recheck_workspace"],
        "stale_zone_detected": ["rescan_zone", "monitor_search_adjacent_zone"],
        "zone_uncertainty_above_target": ["confirm_target_ready", "verify_moved_object"],
        "zone_drift_above_target": ["rescan_zone", "verify_moved_object"],
        "key_object_unknown": ["verify_moved_object", "confirm_target_ready"],
        "execution_slower_than_expected": ["monitor_recheck_workspace", "rescan_zone"],
        "retry_instability_detected": ["monitor_recheck_workspace", "rescan_zone"],
        "fallback_path_used": ["monitor_recheck_workspace"],
        "simulation_reality_mismatch": ["rescan_zone", "monitor_recheck_workspace"],
        "environment_shift_during_execution": ["rescan_zone", "monitor_search_adjacent_zone"],
    }
    proposal_types = mapping.get(str(trigger_type or "").strip(), [])
    return [item for item in proposal_types if str(item).strip()]


async def _proposal_arbitration_followup_preferences(
    *,
    managed_scope: str,
    inquiry_candidates: list[dict],
    degraded_signals: list[dict],
    db: AsyncSession,
) -> dict:
    triggers: list[str] = []
    seen: set[str] = set()
    for collection, key in ((inquiry_candidates, "trigger"), (degraded_signals, "signal_type")):
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            trigger = str(item.get(key, "") or "").strip()
            if not trigger or trigger in seen:
                continue
            seen.add(trigger)
            triggers.append(trigger)

    preferences: list[dict] = []
    for index, trigger in enumerate(triggers):
        proposal_types = _followup_trigger_related_proposal_types(trigger)
        if not proposal_types:
            continue
        influence = await workspace_proposal_arbitration_family_influence(
            proposal_types=proposal_types,
            related_zone=managed_scope,
            db=db,
            max_abs_bias=0.08,
        )
        preference_weight = float(influence.get("aggregate_priority_bias", 0.0) or 0.0)
        preferences.append(
            {
                "trigger_type": trigger,
                "preference_weight": round(preference_weight, 6),
                "sample_count": int(influence.get("sample_count", 0) or 0),
                "proposal_types": influence.get("proposal_types", proposal_types),
                "learning": influence.get("learning", []),
                "applied": bool(influence.get("applied", False)),
                "_index": index,
            }
        )

    if not preferences:
        return {
            "preferred_followup_type": "",
            "preferred_followup_weight": 0.0,
            "preferences": [],
            "applied": False,
        }

    ranked = sorted(
        preferences,
        key=lambda item: (-float(item.get("preference_weight", 0.0) or 0.0), int(item.get("_index", 0) or 0)),
    )
    preferred = ranked[0]
    preferred_type = ""
    preferred_weight = float(preferred.get("preference_weight", 0.0) or 0.0)
    if int(preferred.get("sample_count", 0) or 0) >= 2 and preferred_weight > 0.0:
        preferred_type = str(preferred.get("trigger_type", "") or "").strip()
    for item in preferences:
        item.pop("_index", None)
    return {
        "preferred_followup_type": preferred_type,
        "preferred_followup_weight": round(preferred_weight if preferred_type else 0.0, 6),
        "preferences": preferences,
        "applied": bool(preferred_type),
    }


def _followup_summary(
    *,
    persistent_degradation: bool,
    inquiry_candidates: list[dict],
    degraded_signals: list[dict],
    proposal_arbitration_followup: dict | None = None,
) -> dict:
    candidate_rows = inquiry_candidates if isinstance(inquiry_candidates, list) else []
    candidate_types: list[str] = []
    seen: set[str] = set()
    for item in candidate_rows:
        if not isinstance(item, dict):
            continue
        trigger = str(item.get("trigger", "")).strip()
        if not trigger or trigger in seen:
            continue
        seen.add(trigger)
        candidate_types.append(trigger)

    signal_rows = degraded_signals if isinstance(degraded_signals, list) else []
    for item in signal_rows:
        if not isinstance(item, dict):
            continue
        signal_type = str(item.get("signal_type", "")).strip()
        if not signal_type or signal_type in seen:
            continue
        seen.add(signal_type)
        candidate_types.append(signal_type)

    followup_preferences = (
        proposal_arbitration_followup
        if isinstance(proposal_arbitration_followup, dict)
        else {}
    )
    preference_rows = (
        followup_preferences.get("preferences", [])
        if isinstance(followup_preferences.get("preferences", []), list)
        else []
    )
    preference_lookup = {
        str(item.get("trigger_type", "") or "").strip(): float(item.get("preference_weight", 0.0) or 0.0)
        for item in preference_rows
        if isinstance(item, dict)
    }
    indexed_types = list(enumerate(candidate_types))
    indexed_types.sort(
        key=lambda item: (
            -float(preference_lookup.get(item[1], 0.0) or 0.0),
            int(item[0]),
        )
    )
    candidate_types = [item[1] for item in indexed_types]

    followup_generated = bool(candidate_types)
    followup_suppressed = bool(persistent_degradation and not followup_generated)
    followup_status = "not_needed"
    if followup_generated:
        followup_status = "generated"
    elif followup_suppressed:
        followup_status = "suppressed"

    return {
        "persistent_degradation": bool(persistent_degradation),
        "inquiry_candidate_count": len(candidate_rows),
        "inquiry_candidate_types": candidate_types,
        "followup_generated": followup_generated,
        "followup_suppressed": followup_suppressed,
        "followup_status": followup_status,
        "preferred_followup_type": str(followup_preferences.get("preferred_followup_type", "") or ""),
        "preferred_followup_weight": float(followup_preferences.get("preferred_followup_weight", 0.0) or 0.0),
        "proposal_arbitration_followup": followup_preferences,
    }


def _governance_snapshot(backlog_rows: list[object]) -> dict:
    decision_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for row in backlog_rows:
        decision = (
            str(getattr(row, "governance_decision", "") or "unknown").strip()
            or "unknown"
        )
        status = str(getattr(row, "status", "") or "unknown").strip() or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
    top_items = [
        {
            "improvement_id": int(getattr(row, "id", 0) or 0),
            "proposal_type": str(getattr(row, "proposal_type", "") or "").strip(),
            "priority_score": round(
                float(getattr(row, "priority_score", 0.0) or 0.0), 6
            ),
            "governance_decision": str(
                getattr(row, "governance_decision", "") or ""
            ).strip(),
            "status": str(getattr(row, "status", "") or "").strip(),
        }
        for row in backlog_rows[:5]
    ]
    return {
        "backlog_count": len(backlog_rows),
        "decision_counts": decision_counts,
        "status_counts": status_counts,
        "top_items": top_items,
    }


async def _assess_environment_state(
    *,
    stewardship_id: int | None,
    managed_scope: str,
    target_state: dict,
    lookback_hours: int,
    db: AsyncSession,
) -> dict:
    now = datetime.now(timezone.utc)
    observations = await _load_scope_observations(managed_scope=managed_scope, db=db)
    objects = await _load_scope_objects(managed_scope=managed_scope, db=db)
    latest_by_zone = _latest_observation_per_zone(observations)

    zone_names = set(latest_by_zone.keys()) | {
        str(item.zone or "").strip() or "workspace" for item in objects
    }

    zone_metrics: list[dict] = []
    for zone in sorted(zone_names):
        zone_objects = [
            item
            for item in objects
            if (str(item.zone or "").strip() or "workspace") == zone
        ]
        zone_metrics.append(
            _zone_metrics(
                zone=zone,
                latest_observation=latest_by_zone.get(zone),
                zone_objects=zone_objects,
                target_state=target_state,
                now=now,
            )
        )

    object_metrics = [
        _object_metrics(row=row, target_state=target_state, now=now) for row in objects
    ]
    object_lookup = _object_lookup(objects)
    key_object_metrics: list[dict] = []
    for key_name in target_state.get("key_objects", []):
        row = object_lookup.get(str(key_name).strip().lower())
        if row is None:
            key_object_metrics.append(
                {
                    "object_name": str(key_name).strip(),
                    "present": False,
                    "is_known": False,
                    "stability_score": 0.0,
                    "uncertainty_score": 1.0,
                    "drift_rate": 1.0,
                    "status": "missing",
                }
            )
            continue
        metrics = _object_metrics(row=row, target_state=target_state, now=now)
        key_object_metrics.append(
            {
                **metrics,
                "object_name": str(key_name).strip(),
                "present": True,
            }
        )

    prior_cycles = await _recent_scope_cycles(
        stewardship_id=stewardship_id,
        managed_scope=managed_scope,
        db=db,
        limit=5,
    )
    prior_degraded_count = sum(
        1
        for row in prior_cycles
        if bool(
            (row.metadata_json if isinstance(row.metadata_json, dict) else {})
            .get("assessment", {})
            .get("system_metrics", {})
            .get("degraded_zone_count", 0)
        )
    )

    degraded_signals: list[dict] = []
    for item in zone_metrics:
        if float(item.get("age_seconds", 0.0) or 0.0) > float(
            target_state.get("zone_freshness_seconds", 900) or 900
        ):
            degraded_signals.append(
                {
                    "signal_type": "stale_zone_detected",
                    "target_scope": item.get("zone"),
                    "age_seconds": item.get("age_seconds"),
                    "severity": round(
                        _bounded(
                            float(item.get("age_seconds", 0.0) or 0.0)
                            / max(
                                float(
                                    target_state.get("zone_freshness_seconds", 900)
                                    or 900
                                ),
                                1.0,
                            )
                        ),
                        6,
                    ),
                }
            )
        if float(item.get("uncertainty_score", 0.0) or 0.0) > float(
            target_state.get("max_zone_uncertainty_score", 0.35) or 0.35
        ):
            degraded_signals.append(
                {
                    "signal_type": "zone_uncertainty_above_target",
                    "target_scope": item.get("zone"),
                    "uncertainty_score": item.get("uncertainty_score"),
                    "severity": item.get("uncertainty_score"),
                }
            )
        if float(item.get("drift_rate", 0.0) or 0.0) > float(
            target_state.get("max_zone_drift_rate", 0.35) or 0.35
        ):
            degraded_signals.append(
                {
                    "signal_type": "zone_drift_above_target",
                    "target_scope": item.get("zone"),
                    "drift_rate": item.get("drift_rate"),
                    "severity": item.get("drift_rate"),
                }
            )

    for item in key_object_metrics:
        if not bool(item.get("is_known", False)):
            degraded_signals.append(
                {
                    "signal_type": "key_object_unknown",
                    "target_scope": managed_scope,
                    "object_name": item.get("object_name")
                    or item.get("canonical_name"),
                    "severity": item.get("uncertainty_score", 1.0),
                }
            )

    execution_truth_summary = await _execution_truth_assessment(
        managed_scope=managed_scope,
        lookback_hours=lookback_hours,
        db=db,
    )
    execution_truth_signals = (
        execution_truth_summary.get("signals", [])
        if isinstance(execution_truth_summary.get("signals", []), list)
        else []
    )
    degraded_signals.extend(execution_truth_signals)

    degraded_zone_count = sum(
        1 for item in zone_metrics if bool(item.get("degraded", False))
    )
    if degraded_zone_count > 0 and prior_degraded_count >= 2:
        degraded_signals.append(
            {
                "signal_type": "persistent_zone_degradation",
                "target_scope": managed_scope,
                "persistent_cycle_count": prior_degraded_count,
                "severity": round(_bounded(prior_degraded_count / 3.0), 6),
            }
        )

    zone_uncertainty_avg = (
        sum(float(item.get("uncertainty_score", 0.0)) for item in zone_metrics)
        / float(len(zone_metrics))
        if zone_metrics
        else 0.0
    )
    object_uncertainty_avg = (
        sum(float(item.get("uncertainty_score", 0.0)) for item in object_metrics)
        / float(len(object_metrics))
        if object_metrics
        else 0.0
    )
    zone_drift_avg = (
        sum(float(item.get("drift_rate", 0.0)) for item in zone_metrics)
        / float(len(zone_metrics))
        if zone_metrics
        else 0.0
    )
    object_drift_avg = (
        sum(float(item.get("drift_rate", 0.0)) for item in object_metrics)
        / float(len(object_metrics))
        if object_metrics
        else 0.0
    )
    missing_key_object_count = sum(
        1 for item in key_object_metrics if not bool(item.get("is_known", False))
    )
    system_uncertainty = _bounded(
        (zone_uncertainty_avg * 0.55) + (object_uncertainty_avg * 0.45)
    )
    system_drift = _bounded((zone_drift_avg * 0.6) + (object_drift_avg * 0.4))
    system_stability = _bounded(
        1.0 - ((system_uncertainty * 0.7) + (system_drift * 0.3))
    )

    system_metrics = {
        "stability_score": round(system_stability, 6),
        "uncertainty_score": round(system_uncertainty, 6),
        "drift_rate": round(system_drift, 6),
        "degraded_zone_count": int(degraded_zone_count),
        "known_object_count": int(
            sum(1 for item in object_metrics if bool(item.get("is_known", False)))
        ),
        "object_count": int(len(object_metrics)),
        "missing_key_object_count": int(missing_key_object_count),
        "persistent_degradation_cycles": int(prior_degraded_count),
        "execution_truth_execution_count": int(
            execution_truth_summary.get("execution_count", 0) or 0
        ),
        "execution_truth_signal_count": int(
            execution_truth_summary.get("signal_count", 0) or 0
        ),
        "execution_truth_signal_types": execution_truth_summary.get(
            "signal_types", []
        ),
    }

    needs_intervention = bool(
        degraded_zone_count > int(target_state.get("max_degraded_zones", 0) or 0)
        or missing_key_object_count
        > int(target_state.get("max_missing_key_objects", 0) or 0)
        or system_drift > float(target_state.get("max_system_drift_rate", 0.3) or 0.3)
        or degraded_signals
    )

    return {
        "desired_state": target_state,
        "scope_metrics": {
            "zones": zone_metrics,
            "objects": object_metrics[:100],
            "key_objects": key_object_metrics,
        },
        "system_metrics": system_metrics,
        "execution_truth_summary": execution_truth_summary,
        "deviation_signals": degraded_signals,
        "inquiry_candidates": _inquiry_candidates(deviation_signals=degraded_signals),
        "needs_intervention": needs_intervention,
    }


async def _get_or_create_stewardship(
    *,
    actor: str,
    source: str,
    managed_scope: str,
    desired_state_id: int,
    target_environment_state: dict,
    maintenance_priority: str,
    db: AsyncSession,
) -> WorkspaceStewardshipState:
    existing = (
        (
            await db.execute(
                select(WorkspaceStewardshipState)
                .where(WorkspaceStewardshipState.managed_scope == managed_scope)
                .where(WorkspaceStewardshipState.status == "active")
                .order_by(WorkspaceStewardshipState.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing:
        existing.linked_desired_state_id = desired_state_id
        existing.target_environment_state_json = target_environment_state
        existing.maintenance_priority = maintenance_priority
        return existing

    row = WorkspaceStewardshipState(
        source=source,
        actor=actor,
        status="active",
        linked_desired_state_id=desired_state_id,
        target_environment_state_json=target_environment_state
        if isinstance(target_environment_state, dict)
        else {},
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
    target_environment_state: dict | None,
    db: AsyncSession,
) -> tuple[WorkspaceStewardshipState, WorkspaceStewardshipCycle, dict]:
    strategy_goals = await _recent_strategy_goals(lookback_hours=lookback_hours, db=db)
    concepts = await _recent_concepts(lookback_hours=lookback_hours, db=db)
    patterns = await _recent_patterns(lookback_hours=lookback_hours, db=db)
    boundary = await _latest_boundary(db=db)
    preference_weight = await _preference_weight(db=db, managed_scope=managed_scope)
    execution_truth_governance = await latest_execution_truth_governance_snapshot(
        managed_scope=managed_scope,
        db=db,
    )
    operator_resolution_commitment = await latest_active_operator_resolution_commitment(
        scope=managed_scope,
        db=db,
    )
    operator_resolution_effects = commitment_downstream_effects(
        operator_resolution_commitment
    )
    learned_operator_preference = await latest_scope_learned_preference(
        managed_scope=managed_scope,
        db=db,
        operator_commitment=operator_resolution_commitment,
    )
    backlog_rows = await list_improvement_backlog(
        db=db, status="", risk_level="", limit=25
    )
    linked_goal_ids = [int(item.id) for item in strategy_goals[:25]]
    linked_strategy_types = sorted(
        {
            str(item.strategy_type or "").strip()
            for item in strategy_goals
            if str(item.strategy_type or "").strip()
        }
    )

    desired_state = _normalized_target_state(
        stale_after_seconds=stale_after_seconds,
        overrides=target_environment_state,
    )
    desired_state_row = await _get_or_create_desired_state(
        actor=actor,
        source=source,
        managed_scope=managed_scope,
        target_state=desired_state,
        priority_score=preference_weight,
        origin_strategy_goal_id=(linked_goal_ids[0] if linked_goal_ids else None),
        strategy_types=linked_strategy_types,
        metadata_json=metadata_json,
        db=db,
    )
    stewardship = await _get_or_create_stewardship(
        actor=actor,
        source=source,
        managed_scope=managed_scope,
        desired_state_id=int(desired_state_row.id),
        target_environment_state=desired_state,
        maintenance_priority=(
            "high"
            if preference_weight >= 0.65
            or str(execution_truth_governance.get("governance_decision", "monitor_only"))
            != "monitor_only"
            else "normal"
        ),
        db=db,
    )

    pre_assessment = await _assess_environment_state(
        stewardship_id=int(stewardship.id),
        managed_scope=managed_scope,
        target_state=desired_state,
        lookback_hours=lookback_hours,
        db=db,
    )

    autonomy_level = str(boundary.current_level if boundary else "operator_required")
    boundary_confidence = (
        _bounded(float(boundary.confidence or 0.0)) if boundary else 0.0
    )
    boundary_allowed = True
    allow_auto_execution = bool(auto_execute)
    governance_actions = (
        execution_truth_governance.get("downstream_actions", {})
        if isinstance(execution_truth_governance.get("downstream_actions", {}), dict)
        else {}
    )
    governance_blocks_auto = not bool(
        governance_actions.get("stewardship_auto_execute_allowed", True)
    )
    commitment_blocks_auto = bool(
        operator_resolution_effects.get("stewardship_defer_actions", False)
    ) or str(operator_resolution_effects.get("stewardship_mode", "") or "").strip() == "deferred"
    if not commitment_blocks_auto and operator_resolution_commitment is not None:
        if str(operator_resolution_commitment.decision_type or "").strip() in {
            "defer_action",
            "require_additional_evidence",
        }:
            commitment_blocks_auto = True
    boundary_blocks_auto = False
    if (
        autonomy_level in {"manual_only", "operator_required"}
        and boundary_confidence >= 0.5
    ):
        boundary_allowed = False
        boundary_blocks_auto = True

    policy_conflict_resolution = await resolve_stewardship_policy_conflict(
        managed_scope=managed_scope,
        requested_auto_execution=bool(auto_execute),
        execution_truth_governance=execution_truth_governance,
        operator_resolution_commitment=operator_resolution_commitment,
        learned_preference=learned_operator_preference,
        autonomy_level=autonomy_level,
        boundary_confidence=boundary_confidence,
        db=db,
    )
    policy_conflict_effects = (
        policy_conflict_resolution.get("policy_effects_json", {})
        if isinstance(policy_conflict_resolution.get("policy_effects_json", {}), dict)
        else {}
    )
    if "allow_auto_execution" in policy_conflict_effects:
        allow_auto_execution = bool(policy_conflict_effects.get("allow_auto_execution", False))
    else:
        if governance_blocks_auto or commitment_blocks_auto or boundary_blocks_auto:
            allow_auto_execution = False
    if governance_blocks_auto or commitment_blocks_auto or boundary_blocks_auto:
        allow_auto_execution = bool(allow_auto_execution and not (governance_blocks_auto or commitment_blocks_auto or boundary_blocks_auto)) if "allow_auto_execution" not in policy_conflict_effects else allow_auto_execution
    if boundary_blocks_auto and str(policy_conflict_resolution.get("winning_policy_source", "")).strip() == "autonomy_boundary":
        boundary_allowed = False

    should_run_correction = bool(
        force_degraded or pre_assessment.get("needs_intervention", False)
    )
    maintenance_run = None
    actions_executed = 0
    selected_actions: list[dict] = []
    throttle_state = await _intervention_throttle_state(
        stewardship_id=int(stewardship.id),
        managed_scope=managed_scope,
        target_state=desired_state,
        strategy_types=linked_strategy_types,
        db=db,
    )
    if not bool(throttle_state.get("allowed", False)):
        allow_auto_execution = False

    if operator_resolution_commitment is not None:
        selected_actions.append(
            {
                "action_type": "operator_resolution_commitment_applied",
                "managed_scope": managed_scope,
                "decision_type": str(
                    operator_resolution_commitment.decision_type or ""
                ).strip(),
                "authority_level": str(
                    operator_resolution_commitment.authority_level or ""
                ).strip(),
                "effects": operator_resolution_effects,
                "auto_execute_blocked": bool(commitment_blocks_auto),
            }
        )

    if str(policy_conflict_resolution.get("conflict_state", "")).strip() != "advisory":
        selected_actions.append(
            {
                "action_type": "policy_conflict_resolution_applied",
                "managed_scope": managed_scope,
                "decision_family": str(
                    policy_conflict_resolution.get("decision_family", "")
                ).strip(),
                "winning_policy_source": str(
                    policy_conflict_resolution.get("winning_policy_source", "")
                ).strip(),
                "conflict_state": str(
                    policy_conflict_resolution.get("conflict_state", "")
                ).strip(),
                "policy_effects": policy_conflict_effects,
            }
        )

    if should_run_correction:
        if bool(throttle_state.get("allowed", False)):
            (
                run,
                actions,
                _strategies,
                _memory_count,
            ) = await run_environment_maintenance_cycle(
                actor=actor,
                source=source,
                stale_after_seconds=desired_state.get(
                    "zone_freshness_seconds", stale_after_seconds
                ),
                max_strategies=max_strategies,
                max_actions=max_actions,
                auto_execute=allow_auto_execution,
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective60_stewardship": True,
                    "managed_scope": managed_scope,
                    "target_environment_state": desired_state,
                    "desired_state_id": int(desired_state_row.id),
                },
                db=db,
            )
            maintenance_run = run
            actions_executed = len(actions)
            if maintenance_run:
                selected_actions.append(
                    {
                        "action_type": "maintenance_cycle",
                        "maintenance_run_id": int(maintenance_run.id),
                        "actions_executed": int(actions_executed),
                        "auto_execute": bool(allow_auto_execution),
                    }
                )
        else:
            selected_actions.append(
                {
                    "action_type": "throttle_hold",
                    "reason": "stewardship_intervention_policy_blocked_execution",
                    "managed_scope": managed_scope,
                    "throttle_state": throttle_state,
                }
            )

    if str(execution_truth_governance.get("governance_decision", "monitor_only")) != "monitor_only":
        selected_actions.append(
            {
                "action_type": "execution_truth_governance_applied",
                "managed_scope": managed_scope,
                "governance_decision": str(
                    execution_truth_governance.get("governance_decision", "monitor_only")
                ),
                "reason": str(execution_truth_governance.get("governance_reason", "") or "").strip(),
            }
        )

    post_assessment = await _assess_environment_state(
        stewardship_id=int(stewardship.id),
        managed_scope=managed_scope,
        target_state=desired_state,
        lookback_hours=lookback_hours,
        db=db,
    )
    post_inquiry_candidates = (
        post_assessment.get("inquiry_candidates", [])
        if isinstance(post_assessment.get("inquiry_candidates", []), list)
        else []
    )
    persistent_degradation = bool(
        post_assessment.get("needs_intervention", False)
        or post_assessment.get("deviation_signals", [])
        or post_inquiry_candidates
    )
    proposal_arbitration_followup = await _proposal_arbitration_followup_preferences(
        managed_scope=managed_scope,
        inquiry_candidates=post_inquiry_candidates,
        degraded_signals=post_assessment.get("deviation_signals", []),
        db=db,
    )
    followup_summary = _followup_summary(
        persistent_degradation=persistent_degradation,
        inquiry_candidates=post_inquiry_candidates,
        degraded_signals=post_assessment.get("deviation_signals", []),
        proposal_arbitration_followup=proposal_arbitration_followup,
    )
    execution_truth_signal_count = int(
        post_assessment.get("execution_truth_summary", {}).get("signal_count", 0)
        if isinstance(post_assessment.get("execution_truth_summary", {}), dict)
        else 0
    )
    execution_truth_signal_types = (
        post_assessment.get("execution_truth_summary", {}).get("signal_types", [])
        if isinstance(post_assessment.get("execution_truth_summary", {}), dict)
        else []
    )
    execution_truth_followup_recommended = bool(
        persistent_degradation and execution_truth_signal_count > 0
    )

    if execution_truth_followup_recommended:
        selected_actions.append(
            {
                "action_type": "execution_truth_review_recommended",
                "reason": "runtime_truth_degradation_persists",
                "managed_scope": managed_scope,
                "execution_truth_signal_count": execution_truth_signal_count,
                "execution_truth_signal_types": execution_truth_signal_types,
            }
        )

    if persistent_degradation and not allow_auto_execution:
        selected_actions.append(
            {
                "action_type": "operator_review_recommended",
                "reason": "autonomy_boundary_prevented_auto_execution",
                "managed_scope": managed_scope,
            }
        )
    elif persistent_degradation and actions_executed == 0:
        selected_actions.append(
            {
                "action_type": "followup_required",
                "reason": "stewardship_deviation_persists",
                "managed_scope": managed_scope,
            }
        )

    next_cycle = datetime.now(timezone.utc) + timedelta(
        minutes=(30 if post_assessment.get("needs_intervention", False) else 90)
    )
    governance = _governance_snapshot(backlog_rows)

    pre_health = float(
        pre_assessment.get("system_metrics", {}).get("stability_score", 0.0) or 0.0
    )
    post_health = float(
        post_assessment.get("system_metrics", {}).get("stability_score", 0.0) or 0.0
    )
    improvement_delta = round(post_health - pre_health, 6)
    improved = post_health >= pre_health

    if actions_executed > 0 and improved:
        last_decision_summary = "executed_corrective_actions"
    elif post_assessment.get("needs_intervention", False) and str(
        policy_conflict_resolution.get("winning_policy_source", "")
    ).strip() == "operator_commitment":
        last_decision_summary = "defer_to_operator_commitment"
    elif post_assessment.get("needs_intervention", False) and str(
        policy_conflict_resolution.get("winning_policy_source", "")
    ).strip() == "execution_truth_governance":
        last_decision_summary = "defer_to_execution_truth_governance"
    elif post_assessment.get("needs_intervention", False) and not allow_auto_execution:
        last_decision_summary = "defer_to_operator_boundary"
    elif not post_assessment.get("needs_intervention", False):
        last_decision_summary = "monitor_only_stable_state"
    else:
        last_decision_summary = "correction_attempted_but_drift_persists"

    stewardship.current_health = post_health
    stewardship.last_cycle_at = datetime.now(timezone.utc)
    stewardship.next_cycle_at = next_cycle
    stewardship.cycle_count = int(stewardship.cycle_count or 0) + 1
    stewardship.linked_desired_state_id = int(desired_state_row.id)
    stewardship.linked_strategy_goal_ids_json = linked_goal_ids
    stewardship.linked_strategy_types_json = linked_strategy_types
    if maintenance_run:
        prior_run_ids = (
            stewardship.linked_maintenance_run_ids_json
            if isinstance(stewardship.linked_maintenance_run_ids_json, list)
            else []
        )
        stewardship.linked_maintenance_run_ids_json = [
            *prior_run_ids,
            int(maintenance_run.id),
        ][-50:]
    stewardship.linked_autonomy_boundary_id = int(boundary.id) if boundary else None
    stewardship.last_decision_summary = last_decision_summary
    stewardship.metadata_json = {
        **(
            stewardship.metadata_json
            if isinstance(stewardship.metadata_json, dict)
            else {}
        ),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "objective60_stewardship": True,
        "desired_state_model": {
            "desired_state_id": int(desired_state_row.id),
            "scope": desired_state_row.scope_type,
            "scope_ref": desired_state_row.scope_ref,
            "target_conditions": desired_state_row.target_conditions_json
            if isinstance(desired_state_row.target_conditions_json, dict)
            else {},
            "priority": float(desired_state_row.priority_score or 0.0),
            "strategy_link": desired_state_row.strategy_link_json
            if isinstance(desired_state_row.strategy_link_json, dict)
            else {},
            "created_from": desired_state_row.created_from,
        },
        "current_metrics": post_assessment.get("system_metrics", {}),
        "latest_assessment": post_assessment,
        "governance": governance,
        "operator_resolution_commitment": commitment_snapshot(
            operator_resolution_commitment
        ),
        "policy_conflict_resolution": policy_conflict_resolution,
    }

    verification = {
        "intervention_attempted": bool(should_run_correction),
        "actions_executed": int(actions_executed),
        "pre_system_stability": round(pre_health, 6),
        "post_system_stability": round(post_health, 6),
        "stabilized": bool(improved and not persistent_degradation),
        "remaining_deviation_count": len(post_assessment.get("deviation_signals", [])),
        "persistent_degradation": persistent_degradation,
        "inquiry_candidate_count": len(post_inquiry_candidates),
        "inquiry_candidate_types": followup_summary.get("inquiry_candidate_types", []),
        "inquiry_candidates": post_inquiry_candidates,
        "followup_generated": bool(followup_summary.get("followup_generated", False)),
        "followup_suppressed": bool(followup_summary.get("followup_suppressed", False)),
        "followup_status": str(followup_summary.get("followup_status", "not_needed")),
        "preferred_followup_type": str(followup_summary.get("preferred_followup_type", "")),
        "preferred_followup_weight": float(followup_summary.get("preferred_followup_weight", 0.0) or 0.0),
        "proposal_arbitration_followup": followup_summary.get("proposal_arbitration_followup", {}),
        "execution_truth_signal_count": execution_truth_signal_count,
        "execution_truth_signal_types": execution_truth_signal_types,
        "execution_truth_followup_recommended": execution_truth_followup_recommended,
        "maintenance_run_id": int(maintenance_run.id) if maintenance_run else None,
        "throttle_state": throttle_state,
        "execution_truth_governance": execution_truth_governance,
        "operator_resolution_commitment": commitment_snapshot(
            operator_resolution_commitment
        ),
        "operator_resolution_blocked_auto_execution": bool(commitment_blocks_auto),
        "policy_conflict_resolution": policy_conflict_resolution,
        "policy_conflict_blocked_auto_execution": not bool(
            allow_auto_execution
        )
        and bool(auto_execute),
    }

    cycle = WorkspaceStewardshipCycle(
        stewardship_id=int(stewardship.id),
        source=source,
        actor=actor,
        pre_health=pre_health,
        post_health=post_health,
        improvement_delta=improvement_delta,
        degraded_signals_json=pre_assessment.get("deviation_signals", []),
        selected_actions_json=selected_actions,
        decision_json={
            "desired_state_id": int(desired_state_row.id),
            "allow_auto_execution": bool(allow_auto_execution),
            "boundary_allowed": bool(boundary_allowed),
            "should_run_correction": bool(should_run_correction),
            "autonomy_level": autonomy_level,
            "boundary_confidence": boundary_confidence,
            "throttle_state": throttle_state,
            "decision": stewardship.last_decision_summary,
            "desired_state": desired_state,
            "execution_truth_governance": execution_truth_governance,
            "operator_resolution_commitment": commitment_snapshot(
                operator_resolution_commitment
            ),
            "policy_conflict_resolution": policy_conflict_resolution,
        },
        integration_evidence_json={
            "desired_state_id": int(desired_state_row.id),
            "strategy_goal_ids": linked_goal_ids,
            "strategy_types": linked_strategy_types,
            "concept_count": len(concepts),
            "development_pattern_count": len(patterns),
            "autonomy_boundary_id": int(boundary.id) if boundary else None,
            "operator_preference_weight": preference_weight,
            "governance": governance,
            "operator_resolution_commitment": commitment_snapshot(
                operator_resolution_commitment
            ),
            "policy_conflict_resolution": policy_conflict_resolution,
        },
        maintenance_run_id=(int(maintenance_run.id) if maintenance_run else None),
        improved=improved,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective60_stewardship": True,
            "managed_scope": managed_scope,
            "assessment": {
                "pre": pre_assessment,
                "post": post_assessment,
            },
            "trigger_summary": {
                "triggered_by": pre_assessment.get("deviation_signals", []),
                "expected_state": desired_state,
                "actual_state": pre_assessment.get("system_metrics", {}),
            },
            "verification": verification,
        },
    )
    db.add(cycle)
    await db.flush()

    summary = {
        "degraded_signal_count": len(pre_assessment.get("deviation_signals", [])),
        "actions_executed": int(actions_executed),
        "pre_health": round(pre_health, 6),
        "post_health": round(post_health, 6),
        "autonomy_level": autonomy_level,
        "allow_auto_execution": bool(allow_auto_execution),
        "desired_state_id": int(desired_state_row.id),
        "throttle_blocked": not bool(throttle_state.get("allowed", False)),
        "persistent_degradation": persistent_degradation,
        "inquiry_candidate_count": len(post_inquiry_candidates),
        "inquiry_candidate_types": followup_summary.get("inquiry_candidate_types", []),
        "followup_generated": bool(followup_summary.get("followup_generated", False)),
        "followup_suppressed": bool(followup_summary.get("followup_suppressed", False)),
        "followup_status": str(followup_summary.get("followup_status", "not_needed")),
        "preferred_followup_type": str(followup_summary.get("preferred_followup_type", "")),
        "preferred_followup_weight": float(followup_summary.get("preferred_followup_weight", 0.0) or 0.0),
        "proposal_arbitration_followup": followup_summary.get("proposal_arbitration_followup", {}),
        "execution_truth_signal_count": execution_truth_signal_count,
        "execution_truth_signal_types": execution_truth_signal_types,
        "execution_truth_followup_recommended": execution_truth_followup_recommended,
        "execution_truth_governance": execution_truth_governance,
        "operator_resolution_commitment": commitment_snapshot(
            operator_resolution_commitment
        ),
        "operator_resolution_blocked_auto_execution": bool(commitment_blocks_auto),
        "policy_conflict_resolution": policy_conflict_resolution,
        "system_metrics": post_assessment.get("system_metrics", {}),
        "integrations": {
            "strategy_goals": len(strategy_goals),
            "concept_memory": len(concepts),
            "development_patterns": len(patterns),
            "autonomy_boundary": bool(boundary),
            "operator_preference_weight": preference_weight,
            "governance_backlog": governance.get("backlog_count", 0),
        },
    }
    return stewardship, cycle, summary


async def list_stewardship_states(
    *, managed_scope: str, limit: int, db: AsyncSession
) -> list[WorkspaceStewardshipState]:
    stmt = select(WorkspaceStewardshipState).order_by(
        WorkspaceStewardshipState.id.desc()
    )
    if str(managed_scope or "").strip():
        stmt = stmt.where(
            WorkspaceStewardshipState.managed_scope == str(managed_scope).strip()
        )
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()
    return rows


async def get_stewardship_state(
    *, stewardship_id: int, db: AsyncSession
) -> WorkspaceStewardshipState | None:
    return (
        (
            await db.execute(
                select(WorkspaceStewardshipState)
                .where(WorkspaceStewardshipState.id == stewardship_id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def list_stewardship_cycles(
    *,
    stewardship_id: int | None,
    cycle_id: int | None,
    managed_scope: str,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceStewardshipCycle]:
    stmt = select(WorkspaceStewardshipCycle).order_by(
        WorkspaceStewardshipCycle.id.desc()
    )
    if stewardship_id is not None:
        stmt = stmt.where(
            WorkspaceStewardshipCycle.stewardship_id == int(stewardship_id)
        )
    if cycle_id is not None:
        stmt = stmt.where(WorkspaceStewardshipCycle.id == int(cycle_id))
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()
    requested_scope = str(managed_scope or "").strip()
    if not requested_scope:
        return rows
    return [
        row
        for row in rows
        if str(
            (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                "managed_scope", ""
            )
        ).strip()
        == requested_scope
    ]


async def list_stewardship_history(
    *, stewardship_id: int | None, limit: int, db: AsyncSession
) -> list[WorkspaceStewardshipCycle]:
    stmt = select(WorkspaceStewardshipCycle).order_by(
        WorkspaceStewardshipCycle.id.desc()
    )
    if stewardship_id is not None:
        stmt = stmt.where(
            WorkspaceStewardshipCycle.stewardship_id == int(stewardship_id)
        )
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()
    return rows


def to_stewardship_out(row: WorkspaceStewardshipState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return {
        "stewardship_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "linked_desired_state_id": row.linked_desired_state_id,
        "desired_state": metadata.get("desired_state_model", {})
        if isinstance(metadata.get("desired_state_model", {}), dict)
        else {},
        "target_environment_state": row.target_environment_state_json
        if isinstance(row.target_environment_state_json, dict)
        else {},
        "managed_scope": row.managed_scope,
        "maintenance_priority": row.maintenance_priority,
        "current_health": float(row.current_health or 0.0),
        "last_cycle": row.last_cycle_at,
        "next_cycle": row.next_cycle_at,
        "cycle_count": int(row.cycle_count or 0),
        "linked_strategy_goal_ids": row.linked_strategy_goal_ids_json
        if isinstance(row.linked_strategy_goal_ids_json, list)
        else [],
        "linked_maintenance_run_ids": row.linked_maintenance_run_ids_json
        if isinstance(row.linked_maintenance_run_ids_json, list)
        else [],
        "linked_strategy_types": row.linked_strategy_types_json
        if isinstance(row.linked_strategy_types_json, list)
        else [],
        "linked_autonomy_boundary_id": row.linked_autonomy_boundary_id,
        "last_decision_summary": row.last_decision_summary,
        "current_metrics": metadata.get("current_metrics", {})
        if isinstance(metadata.get("current_metrics", {}), dict)
        else {},
        "metadata_json": metadata,
        "created_at": row.created_at,
    }


def to_stewardship_cycle_out(row: WorkspaceStewardshipCycle) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    assessment = (
        metadata.get("assessment", {})
        if isinstance(metadata.get("assessment", {}), dict)
        else {}
    )
    verification = (
        metadata.get("verification", {})
        if isinstance(metadata.get("verification", {}), dict)
        else {}
    )
    followup_summary = _followup_summary(
        persistent_degradation=bool(verification.get("persistent_degradation", False)),
        inquiry_candidates=verification.get("inquiry_candidates", []),
        degraded_signals=(
            assessment.get("post", {}).get("deviation_signals", [])
            if isinstance(assessment.get("post", {}), dict)
            else row.degraded_signals_json
        ),
        proposal_arbitration_followup=(
            verification.get("proposal_arbitration_followup", {})
            if isinstance(verification.get("proposal_arbitration_followup", {}), dict)
            else {}
        ),
    )
    return {
        "cycle_id": int(row.id),
        "stewardship_id": int(row.stewardship_id),
        "source": row.source,
        "actor": row.actor,
        "pre_health": float(row.pre_health or 0.0),
        "post_health": float(row.post_health or 0.0),
        "improvement_delta": float(row.improvement_delta or 0.0),
        "degraded_signals": row.degraded_signals_json
        if isinstance(row.degraded_signals_json, list)
        else [],
        "selected_actions": row.selected_actions_json
        if isinstance(row.selected_actions_json, list)
        else [],
        "decision": row.decision_json if isinstance(row.decision_json, dict) else {},
        "integration_evidence": row.integration_evidence_json
        if isinstance(row.integration_evidence_json, dict)
        else {},
        "assessment": assessment,
        "verification": verification,
        "persistent_degradation": bool(
            followup_summary.get("persistent_degradation", False)
        ),
        "inquiry_candidate_count": int(
            followup_summary.get("inquiry_candidate_count", 0) or 0
        ),
        "inquiry_candidate_types": followup_summary.get("inquiry_candidate_types", []),
        "followup_generated": bool(followup_summary.get("followup_generated", False)),
        "followup_suppressed": bool(followup_summary.get("followup_suppressed", False)),
        "followup_status": str(followup_summary.get("followup_status", "not_needed")),
        "preferred_followup_type": str(followup_summary.get("preferred_followup_type", "")),
        "preferred_followup_weight": float(followup_summary.get("preferred_followup_weight", 0.0) or 0.0),
        "maintenance_run_id": row.maintenance_run_id,
        "improved": bool(row.improved),
        "metadata_json": metadata,
        "created_at": row.created_at,
    }
