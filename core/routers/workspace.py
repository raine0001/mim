import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from core.db import SessionLocal, get_db
from core.journal import write_journal
from core.models import CapabilityExecution, CapabilityRegistration, InputEvent, Task, WorkspaceActionPlan, WorkspaceAutonomousChain, WorkspaceInterruptionEvent, WorkspaceMonitoringState, WorkspaceObjectMemory, WorkspaceObjectRelation, WorkspaceObservation, WorkspaceProposal, WorkspaceReplanSignal, WorkspaceTargetResolution, WorkspaceZone, WorkspaceZoneRelation
from core.preferences import DEFAULT_USER_ID, apply_learning_signal, get_user_preference_payload, get_user_preference_value
from core.schemas import WorkspaceActionPlanAbortRequest, WorkspaceActionPlanCreateRequest, WorkspaceActionPlanDecisionRequest, WorkspaceActionPlanExecuteRequest, WorkspaceActionPlanHandoffRequest, WorkspaceActionPlanReplanRequest, WorkspaceActionPlanSimulationRequest, WorkspaceAutonomousChainAdvanceRequest, WorkspaceAutonomousChainApprovalRequest, WorkspaceAutonomousChainCreateRequest, WorkspaceAutonomyOverrideRequest, WorkspaceExecutionPauseRequest, WorkspaceExecutionPredictChangeRequest, WorkspaceExecutionProposalActionRequest, WorkspaceExecutionProposalCreateRequest, WorkspaceExecutionResumeRequest, WorkspaceExecutionStopRequest, WorkspaceMonitoringStartRequest, WorkspaceMonitoringStopRequest, WorkspaceProposalActionRequest, WorkspaceProposalPriorityPolicyUpdateRequest, WorkspaceTargetConfirmRequest, WorkspaceTargetResolveRequest

router = APIRouter()

RECENT_WINDOW_SECONDS = 600
OUTDATED_WINDOW_SECONDS = 3600
OBJECT_STALE_WINDOW_SECONDS = 7200

DEFAULT_ZONE_GRAPH: dict[str, dict[str, list[str] | int]] = {
    "front-left": {"adjacent_to": ["front-center", "rear-left"], "left_of": ["front-center"], "in_front_of": ["rear-left"], "hazard_level": 0},
    "front-center": {"adjacent_to": ["front-left", "front-right", "rear-center"], "left_of": ["front-right"], "right_of": ["front-left"], "in_front_of": ["rear-center"], "hazard_level": 0},
    "front-right": {"adjacent_to": ["front-center", "rear-right"], "right_of": ["front-center"], "in_front_of": ["rear-right"], "hazard_level": 0},
    "rear-left": {"adjacent_to": ["rear-center", "front-left"], "left_of": ["rear-center"], "behind": ["front-left"], "hazard_level": 0},
    "rear-center": {"adjacent_to": ["rear-left", "rear-right", "front-center"], "behind": ["front-center"], "hazard_level": 0},
    "rear-right": {"adjacent_to": ["rear-center", "front-right"], "right_of": ["rear-center"], "behind": ["front-right"], "hazard_level": 0},
}

SAFE_ACTION_TYPES = {
    "observe",
    "rescan",
    "speak",
    "prepare_reach_plan",
    "request_confirmation",
}

SIMULATION_BLOCK_THRESHOLD_DEFAULT = 0.45
EXECUTION_TARGET_CONFIDENCE_MINIMUM_DEFAULT = 0.7
EXECUTION_ALLOWED_CAPABILITIES = {
    "reach_target",
    "arm_move_safe",
}
EXECUTION_PROPOSAL_TYPE = "execution_candidate"
MONITORING_RECHECK_PROPOSAL_TYPE = "monitor_recheck_workspace"
MONITORING_SEARCH_PROPOSAL_TYPE = "monitor_search_adjacent_zone"
MONITORING_DEFAULT_INTERVAL_SECONDS = 30
MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS = 900
MONITORING_DEFAULT_COOLDOWN_SECONDS = 10
MONITORING_DEFAULT_MAX_SCAN_RATE = 6
CHAIN_DEFAULT_STEP_POLICY = {
    "terminal_statuses": ["accepted", "rejected"],
    "failure_statuses": ["rejected"],
}

INTERRUPTION_BLOCKING_TYPES = {
    "human_detected_in_workspace",
    "new_obstacle_detected",
    "target_confidence_drop",
    "workspace_state_changed",
    "safety_policy_interrupt",
}

INTERRUPTION_POLICY_OUTCOMES = {
    "human_detected_in_workspace": "auto_pause",
    "operator_pause": "auto_pause",
    "operator_stop": "auto_stop",
    "new_obstacle_detected": "auto_stop",
    "target_confidence_drop": "require_operator_decision",
    "workspace_state_changed": "require_operator_decision",
    "safety_policy_interrupt": "auto_stop",
}

REPLAN_OUTCOME_MAP = {
    "continue_monitor": "continue_monitor",
    "pause_and_resimulate": "pause_and_resimulate",
    "require_replan": "require_replan",
    "abort_chain": "abort_chain",
}

REPLAN_OUTCOME_SEVERITY = {
    "continue_monitor": 0,
    "pause_and_resimulate": 1,
    "require_replan": 2,
    "abort_chain": 3,
}

AUTONOMY_POLICY_TIERS = {
    "manual_only",
    "operator_required",
    "auto_safe",
    "auto_preferred",
}
AUTONOMY_PROPOSAL_POLICY_MAP: dict[str, str] = {
    "confirm_target_ready": "auto_safe",
    "rescan_zone": "auto_safe",
    "monitor_recheck_workspace": "auto_safe",
    "monitor_search_adjacent_zone": "auto_safe",
    "verify_moved_object": "operator_required",
    "target_confirmation": "manual_only",
    "target_reobserve": "manual_only",
    "execution_candidate": "operator_required",
}
AUTONOMY_PROPOSAL_RISK_SCORE: dict[str, float] = {
    "confirm_target_ready": 0.18,
    "rescan_zone": 0.12,
    "monitor_recheck_workspace": 0.1,
    "monitor_search_adjacent_zone": 0.14,
    "verify_moved_object": 0.55,
    "target_confirmation": 0.8,
    "target_reobserve": 0.78,
    "execution_candidate": 0.82,
}

PROPOSAL_PRIORITY_POLICY_VERSION = "proposal-priority-v1"
PROPOSAL_PRIORITY_DEFAULT = {
    "weights": {
        "urgency": 0.28,
        "confidence": 0.22,
        "safety": 0.2,
        "operator_preference": 0.15,
        "zone_importance": 0.1,
        "age": 0.05,
    },
    "urgency_map": {
        "execution_candidate": 0.95,
        "verify_moved_object": 0.9,
        "monitor_search_adjacent_zone": 0.8,
        "monitor_recheck_workspace": 0.72,
        "rescan_zone": 0.62,
        "confirm_target_ready": 0.52,
        "target_reobserve": 0.45,
        "target_confirmation": 0.4,
    },
    "zone_importance": {
        "front-center": 1.0,
        "front-left": 0.85,
        "front-right": 0.85,
        "rear-center": 0.7,
        "rear-left": 0.55,
        "rear-right": 0.55,
    },
    "operator_preference": {},
    "age_saturation_minutes": 120,
}


def _default_autonomy_state() -> dict:
    return {
        "auto_execution_enabled": True,
        "force_manual_approval": False,
        "max_auto_actions_per_minute": 6,
        "cooldown_between_actions_seconds": 5,
        "zone_action_limits": {},
        "auto_safe_confidence_threshold": 0.8,
        "auto_preferred_confidence_threshold": 0.7,
        "low_risk_score_max": 0.3,
        "recent_auto_actions": [],
    }


def _autonomy_state_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = metadata.get("autonomy", {}) if isinstance(metadata.get("autonomy", {}), dict) else {}
    defaults = _default_autonomy_state()
    merged = {
        **defaults,
        **raw,
    }
    merged["zone_action_limits"] = {
        str(key): max(1, int(value))
        for key, value in (merged.get("zone_action_limits", {}) if isinstance(merged.get("zone_action_limits", {}), dict) else {}).items()
        if str(key).strip()
    }
    return merged


def _store_autonomy_state(row: WorkspaceMonitoringState, autonomy_state: dict) -> None:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.metadata_json = {
        **metadata,
        "autonomy": autonomy_state,
    }


def _normalize_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _proposal_priority_policy_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = metadata.get("proposal_priority_policy", {}) if isinstance(metadata.get("proposal_priority_policy", {}), dict) else {}
    defaults = PROPOSAL_PRIORITY_DEFAULT

    policy = {
        "weights": {
            key: _normalize_score(value)
            for key, value in {
                **defaults.get("weights", {}),
                **(raw.get("weights", {}) if isinstance(raw.get("weights", {}), dict) else {}),
            }.items()
        },
        "urgency_map": {
            str(key): _normalize_score(value)
            for key, value in {
                **defaults.get("urgency_map", {}),
                **(raw.get("urgency_map", {}) if isinstance(raw.get("urgency_map", {}), dict) else {}),
            }.items()
            if str(key).strip()
        },
        "zone_importance": {
            str(key): _normalize_score(value)
            for key, value in {
                **defaults.get("zone_importance", {}),
                **(raw.get("zone_importance", {}) if isinstance(raw.get("zone_importance", {}), dict) else {}),
            }.items()
            if str(key).strip()
        },
        "operator_preference": {
            str(key): _normalize_score(value)
            for key, value in {
                **(defaults.get("operator_preference", {}) if isinstance(defaults.get("operator_preference", {}), dict) else {}),
                **(raw.get("operator_preference", {}) if isinstance(raw.get("operator_preference", {}), dict) else {}),
            }.items()
            if str(key).strip()
        },
        "age_saturation_minutes": max(1, int(raw.get("age_saturation_minutes", defaults.get("age_saturation_minutes", 120)))),
        "version": PROPOSAL_PRIORITY_POLICY_VERSION,
    }
    return policy


def _compute_workspace_proposal_priority(*, proposal: WorkspaceProposal, policy: dict, now: datetime) -> tuple[float, str, dict]:
    urgency_map = policy.get("urgency_map", {}) if isinstance(policy.get("urgency_map", {}), dict) else {}
    zone_importance_map = policy.get("zone_importance", {}) if isinstance(policy.get("zone_importance", {}), dict) else {}
    operator_preference_map = policy.get("operator_preference", {}) if isinstance(policy.get("operator_preference", {}), dict) else {}
    weights = policy.get("weights", {}) if isinstance(policy.get("weights", {}), dict) else {}

    normalized_zone = _normalize_zone_for_map(proposal.related_zone)
    urgency = _normalize_score(float(urgency_map.get(proposal.proposal_type, 0.5)))
    confidence = _normalize_score(float(proposal.confidence))
    safety = _normalize_score(1.0 - _autonomy_risk_score(proposal.proposal_type))
    operator_preference = _normalize_score(float(operator_preference_map.get(proposal.proposal_type, 0.5)))
    zone_importance = _normalize_score(float(zone_importance_map.get(normalized_zone, 0.5)))

    age_minutes = max(0.0, (now - proposal.created_at).total_seconds() / 60.0)
    age_saturation = max(1.0, float(policy.get("age_saturation_minutes", 120)))
    age = _normalize_score(age_minutes / age_saturation)

    components = {
        "urgency": urgency,
        "confidence": confidence,
        "safety": safety,
        "operator_preference": operator_preference,
        "zone_importance": zone_importance,
        "age": age,
    }
    score = 0.0
    contribution: dict[str, float] = {}
    for name, value in components.items():
        weighted = _normalize_score(float(weights.get(name, 0.0))) * value
        contribution[name] = round(weighted, 6)
        score += weighted

    top = sorted(contribution.items(), key=lambda item: item[1], reverse=True)[:3]
    reason = ", ".join(f"{name}={components[name]:.2f}" for name, _ in top)
    return round(_normalize_score(score), 4), reason, {
        "components": components,
        "weighted": contribution,
        "top_signals": [name for name, _ in top],
    }


async def _refresh_workspace_proposal_priority(*, proposal: WorkspaceProposal, db: AsyncSession) -> dict:
    monitoring = await _get_or_create_monitoring_state(db)
    policy = _proposal_priority_policy_from_monitoring(monitoring)

    preferred_scan_zones_payload = await get_user_preference_payload(
        db=db,
        preference_type="preferred_scan_zones",
        user_id=DEFAULT_USER_ID,
    )
    preferred_scan_zones = preferred_scan_zones_payload.get("value", [])
    if isinstance(preferred_scan_zones, list):
        zone_importance = dict(policy.get("zone_importance", {}))
        for zone_name in preferred_scan_zones:
            normalized = _normalize_zone_for_map(str(zone_name))
            if normalized:
                zone_importance[normalized] = max(0.95, float(zone_importance.get(normalized, 0.5)))
        policy["zone_importance"] = zone_importance

    auto_exec_tolerance_payload = await get_user_preference_payload(
        db=db,
        preference_type="auto_exec_tolerance",
        user_id=DEFAULT_USER_ID,
    )
    auto_exec_tolerance = float(auto_exec_tolerance_payload.get("value", 0.5) or 0.5)
    operator_preference = dict(policy.get("operator_preference", {}))
    operator_preference["confirm_target_ready"] = max(
        float(operator_preference.get("confirm_target_ready", 0.5)),
        max(0.0, min(1.0, auto_exec_tolerance)),
    )
    operator_preference["rescan_zone"] = max(
        float(operator_preference.get("rescan_zone", 0.5)),
        max(0.0, min(1.0, auto_exec_tolerance)),
    )
    policy["operator_preference"] = operator_preference

    now = datetime.now(timezone.utc)
    score, reason, breakdown = _compute_workspace_proposal_priority(proposal=proposal, policy=policy, now=now)
    proposal.priority_score = score
    proposal.priority_reason = reason
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "priority_policy_version": policy.get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
        "priority_breakdown": breakdown,
        "preference_context": {
            "preferred_scan_zones": preferred_scan_zones if isinstance(preferred_scan_zones, list) else [],
            "auto_exec_tolerance": auto_exec_tolerance,
        },
    }
    return {
        "policy": policy,
        "score": score,
        "reason": reason,
        "breakdown": breakdown,
    }


def _workspace_proposal_payload(row: WorkspaceProposal) -> dict:
    return {
        "proposal_id": row.id,
        "proposal_type": row.proposal_type,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "confidence": row.confidence,
        "priority_score": float(row.priority_score),
        "priority_reason": row.priority_reason,
        "source": row.source,
        "related_zone": row.related_zone,
        "related_object_id": row.related_object_id,
        "source_execution_id": row.source_execution_id,
        "trigger_json": row.trigger_json,
        "metadata_json": row.metadata_json,
        "created_at": row.created_at,
    }


async def _notification_payload_for_proposal(*, db: AsyncSession, proposal: WorkspaceProposal, action: str) -> dict:
    verbosity = await get_user_preference_value(
        db=db,
        preference_type="notification_verbosity",
        user_id=DEFAULT_USER_ID,
    )
    level = str(verbosity or "normal").strip().lower()
    if level not in {"low", "normal", "high"}:
        level = "normal"

    if level == "low":
        message = f"Proposal {proposal.id} {action}."
    elif level == "high":
        message = (
            f"Proposal {proposal.id} {action}: type={proposal.proposal_type}, "
            f"priority_score={float(proposal.priority_score):.2f}, confidence={float(proposal.confidence):.2f}, "
            f"reason={proposal.priority_reason}."
        )
    else:
        message = (
            f"Proposal {proposal.id} {action} with priority_score={float(proposal.priority_score):.2f} "
            f"and confidence={float(proposal.confidence):.2f}."
        )

    return {
        "verbosity": level,
        "message": message,
    }


async def _is_safe_zone(*, related_zone: str, db: AsyncSession) -> bool:
    zone_name = related_zone.strip()
    if not zone_name:
        return True
    mapped = _normalize_zone_for_map(zone_name)
    zone = (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped))).scalars().first()
    if not zone:
        return True
    return int(zone.hazard_level) <= 0


def _autonomy_policy_tier(proposal_type: str) -> str:
    return AUTONOMY_PROPOSAL_POLICY_MAP.get(proposal_type, "operator_required")


def _autonomy_confidence_threshold(*, tier: str, autonomy_state: dict) -> float:
    if tier == "auto_preferred":
        return float(autonomy_state.get("auto_preferred_confidence_threshold", 0.7))
    if tier == "auto_safe":
        return float(autonomy_state.get("auto_safe_confidence_threshold", 0.8))
    return 1.0


def _autonomy_risk_score(proposal_type: str) -> float:
    return float(AUTONOMY_PROPOSAL_RISK_SCORE.get(proposal_type, 1.0))


def _autonomy_simulation_safe(proposal: WorkspaceProposal) -> tuple[bool, str]:
    trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
    if "simulation_outcome" in trigger:
        outcome = str(trigger.get("simulation_outcome", ""))
    elif isinstance(trigger.get("preconditions", {}), dict):
        outcome = str(trigger.get("preconditions", {}).get("simulation_outcome", ""))
    else:
        return True, "not_required"

    if not outcome:
        return True, "not_required"
    return outcome == "plan_safe", outcome


def _parse_iso_to_utc(raw_value: str) -> datetime | None:
    candidate = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _autonomy_throttle_check(*, autonomy_state: dict, zone: str, now: datetime) -> tuple[bool, str, list[dict]]:
    recent_raw = autonomy_state.get("recent_auto_actions", [])
    recent_actions: list[dict] = []
    if isinstance(recent_raw, list):
        for item in recent_raw:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", "")).strip()
            parsed = _parse_iso_to_utc(ts) if ts else None
            if not parsed:
                continue
            if (now - parsed).total_seconds() <= 60:
                recent_actions.append({**item, "timestamp": parsed.isoformat()})

    max_per_min = max(1, int(autonomy_state.get("max_auto_actions_per_minute", 6)))
    if len(recent_actions) >= max_per_min:
        return False, "max_auto_actions_per_minute", recent_actions

    cooldown = max(0, int(autonomy_state.get("cooldown_between_actions_seconds", 0)))
    if recent_actions:
        latest = max((_parse_iso_to_utc(str(item.get("timestamp", ""))) for item in recent_actions), default=None)
        if latest and (now - latest).total_seconds() < cooldown:
            return False, "cooldown_between_actions", recent_actions

    zone_limits = autonomy_state.get("zone_action_limits", {}) if isinstance(autonomy_state.get("zone_action_limits", {}), dict) else {}
    if zone.strip() and zone in zone_limits:
        zone_count = sum(1 for item in recent_actions if str(item.get("zone", "")).strip() == zone)
        if zone_count >= max(1, int(zone_limits[zone])):
            return False, "zone_based_limit", recent_actions

    return True, "allowed", recent_actions


async def _execute_workspace_proposal_to_task(
    *,
    proposal: WorkspaceProposal,
    actor: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> int:
    task = Task(
        title=proposal.title,
        details=proposal.description,
        dependencies=[],
        acceptance_criteria="proposal accepted and queued for execution planning",
        assigned_to="tod",
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    proposal.status = "accepted"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": actor,
        "accept_reason": reason,
        "linked_task_id": task.id,
        **metadata_json,
    }
    return task.id


async def _maybe_auto_execute_workspace_proposal(
    *,
    proposal: WorkspaceProposal,
    trigger_reason: str,
    db: AsyncSession,
) -> tuple[bool, str]:
    monitoring = await _get_or_create_monitoring_state(db)
    autonomy_state = _autonomy_state_from_monitoring(monitoring)
    tier = _autonomy_policy_tier(proposal.proposal_type)
    if tier not in AUTONOMY_POLICY_TIERS:
        return False, "unknown_policy_tier"
    if not autonomy_state.get("auto_execution_enabled", True):
        return False, "auto_execution_disabled"
    if autonomy_state.get("force_manual_approval", False):
        return False, "force_manual_approval"
    if tier in {"manual_only", "operator_required"}:
        return False, f"policy_{tier}"

    threshold = _autonomy_confidence_threshold(tier=tier, autonomy_state=autonomy_state)
    if float(proposal.confidence) < threshold:
        return False, "confidence_below_threshold"

    safe_zone = await _is_safe_zone(related_zone=proposal.related_zone, db=db)
    if not safe_zone:
        return False, "unsafe_zone"

    risk_score = _autonomy_risk_score(proposal.proposal_type)
    if risk_score > float(autonomy_state.get("low_risk_score_max", 0.3)):
        return False, "risk_score_too_high"

    simulation_safe, simulation_result = _autonomy_simulation_safe(proposal)
    if not simulation_safe:
        return False, "simulation_not_safe"

    now = datetime.now(timezone.utc)
    throttle_allowed, throttle_reason, recent_actions = _autonomy_throttle_check(
        autonomy_state=autonomy_state,
        zone=proposal.related_zone,
        now=now,
    )
    if not throttle_allowed:
        return False, throttle_reason

    task_id = await _execute_workspace_proposal_to_task(
        proposal=proposal,
        actor="system-auto",
        reason="objective35_auto_execute",
        metadata_json={
            "auto_execution": True,
            "trigger_reason": trigger_reason,
            "policy_rule_used": tier,
            "confidence_score": float(proposal.confidence),
            "risk_score": risk_score,
            "simulation_result": simulation_result,
            "execution_outcome": "queued_task",
        },
        db=db,
    )

    recent_actions.append(
        {
            "timestamp": now.isoformat(),
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "zone": proposal.related_zone,
            "task_id": task_id,
        }
    )
    autonomy_state["recent_auto_actions"] = recent_actions
    _store_autonomy_state(monitoring, autonomy_state)

    await write_journal(
        db,
        actor="system-auto",
        action="workspace_proposal_auto_execute",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=f"Auto-executed workspace proposal {proposal.id}",
        metadata_json={
            "trigger_reason": trigger_reason,
            "policy_rule_used": tier,
            "confidence_score": float(proposal.confidence),
            "risk_score": risk_score,
            "simulation_result": simulation_result,
            "execution_outcome": "queued_task",
            "linked_task_id": task_id,
        },
    )
    return True, "auto_executed"


class _WorkspaceMonitoringRuntime:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.loop_started_at: datetime | None = None


MONITORING_RUNTIME = _WorkspaceMonitoringRuntime()


def _monitoring_policy_payload(row: WorkspaceMonitoringState) -> dict:
    return {
        "trigger_mode": row.scan_trigger_mode,
        "interval_seconds": int(row.interval_seconds),
        "freshness_threshold_seconds": int(row.freshness_threshold_seconds),
        "cooldown_seconds": int(row.cooldown_seconds),
        "max_scan_rate": int(row.max_scan_rate),
        "priority_zones": [item for item in (row.priority_zones if isinstance(row.priority_zones, list) else []) if str(item).strip()],
    }


def _monitoring_state_payload(row: WorkspaceMonitoringState) -> dict:
    autonomy = _autonomy_state_from_monitoring(row)
    return {
        "desired_running": row.desired_running,
        "runtime_status": row.runtime_status,
        "is_running": bool(MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done()),
        "task_started_at": MONITORING_RUNTIME.loop_started_at,
        "policy": _monitoring_policy_payload(row),
        "last_scan_at": row.last_scan_at,
        "scan_count": row.scan_count,
        "last_scan_reason": row.last_scan_reason,
        "last_deltas": row.last_deltas_json if isinstance(row.last_deltas_json, list) else [],
        "last_proposal_ids": row.last_proposal_ids if isinstance(row.last_proposal_ids, list) else [],
        "last_started_at": row.last_started_at,
        "last_stopped_at": row.last_stopped_at,
        "autonomy": {
            key: value
            for key, value in autonomy.items()
            if key != "recent_auto_actions"
        },
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
    }


def _to_workspace_autonomous_chain_out(row: WorkspaceAutonomousChain) -> dict:
    return {
        "chain_id": row.id,
        "chain_type": row.chain_type,
        "status": row.status,
        "source": row.source,
        "trigger_reason": row.trigger_reason,
        "proposal_ids": row.step_proposal_ids if isinstance(row.step_proposal_ids, list) else [],
        "step_policy_json": row.step_policy_json if isinstance(row.step_policy_json, dict) else {},
        "stop_on_failure": bool(row.stop_on_failure),
        "cooldown_seconds": int(row.cooldown_seconds),
        "requires_approval": bool(row.requires_approval),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "last_advanced_at": row.last_advanced_at,
        "current_step_index": row.current_step_index,
        "completed_step_ids": row.completed_step_ids if isinstance(row.completed_step_ids, list) else [],
        "failed_step_ids": row.failed_step_ids if isinstance(row.failed_step_ids, list) else [],
        "audit_trail": _coerced_json_list(row.audit_trail_json),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def _normalized_chain_step_policy(raw: dict | None) -> dict:
    policy = raw if isinstance(raw, dict) else {}
    terminal_statuses = [
        str(item).strip().lower()
        for item in (policy.get("terminal_statuses", CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]) if isinstance(policy.get("terminal_statuses", CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]), list) else CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"])
        if str(item).strip()
    ]
    if not terminal_statuses:
        terminal_statuses = CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]
    failure_statuses = [
        str(item).strip().lower()
        for item in (policy.get("failure_statuses", CHAIN_DEFAULT_STEP_POLICY["failure_statuses"]) if isinstance(policy.get("failure_statuses", CHAIN_DEFAULT_STEP_POLICY["failure_statuses"]), list) else CHAIN_DEFAULT_STEP_POLICY["failure_statuses"])
        if str(item).strip()
    ]
    return {
        "terminal_statuses": terminal_statuses,
        "failure_statuses": failure_statuses,
    }


def _coerced_json_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _interruption_outcome_for_type(interruption_type: str) -> str:
    return INTERRUPTION_POLICY_OUTCOMES.get(interruption_type, "require_operator_decision")


def _interruption_is_blocking(interruption_type: str) -> bool:
    return interruption_type in INTERRUPTION_BLOCKING_TYPES


async def _find_action_plan_for_execution(*, execution_id: int, db: AsyncSession) -> WorkspaceActionPlan | None:
    return (
        await db.execute(
            select(WorkspaceActionPlan)
            .where(WorkspaceActionPlan.execution_id == execution_id)
            .order_by(WorkspaceActionPlan.id.desc())
        )
    ).scalars().first()


async def _find_chains_for_execution(*, execution_id: int, db: AsyncSession) -> list[WorkspaceAutonomousChain]:
    proposals = (
        await db.execute(
            select(WorkspaceProposal).where(WorkspaceProposal.source_execution_id == execution_id)
        )
    ).scalars().all()
    proposal_ids = {item.id for item in proposals}

    chains = (await db.execute(select(WorkspaceAutonomousChain))).scalars().all()
    matched: list[WorkspaceAutonomousChain] = []
    for chain in chains:
        metadata = chain.metadata_json if isinstance(chain.metadata_json, dict) else {}
        linked_execution_id = int(metadata.get("active_execution_id", 0)) if str(metadata.get("active_execution_id", "")).isdigit() else 0
        if linked_execution_id == execution_id:
            matched.append(chain)
            continue
        step_ids = chain.step_proposal_ids if isinstance(chain.step_proposal_ids, list) else []
        if proposal_ids and any(int(item) in proposal_ids for item in step_ids):
            matched.append(chain)
    return matched


def _append_execution_interruption(execution: CapabilityExecution, *, event: str, actor: str, reason: str, metadata_json: dict) -> None:
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    interruptions = list(feedback.get("interruptions", []))
    interruptions.append(
        {
            "event": event,
            "actor": actor,
            "reason": reason,
            "metadata_json": metadata_json,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    execution.feedback_json = {
        **feedback,
        "interruptions": interruptions[-200:],
    }


async def _record_interruption_event(
    *,
    execution: CapabilityExecution,
    action_plan: WorkspaceActionPlan | None,
    chain: WorkspaceAutonomousChain | None,
    interruption_type: str,
    source: str,
    requested_outcome: str,
    applied_outcome: str,
    status: str,
    actor: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceInterruptionEvent:
    event = WorkspaceInterruptionEvent(
        execution_id=execution.id,
        action_plan_id=action_plan.id if action_plan else None,
        chain_id=chain.id if chain else None,
        interruption_type=interruption_type,
        source=source,
        requested_outcome=requested_outcome,
        applied_outcome=applied_outcome,
        status=status,
        reason=reason,
        actor=actor,
        metadata_json=metadata_json,
    )
    db.add(event)
    await db.flush()
    return event


def _append_chain_audit(row: WorkspaceAutonomousChain, *, actor: str, event: str, reason: str, metadata_json: dict) -> None:
    trail = list(_coerced_json_list(row.audit_trail_json))
    trail.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event,
            "reason": reason,
            "metadata_json": metadata_json,
            "status": row.status,
            "current_step_index": int(row.current_step_index),
        }
    )
    row.audit_trail_json = trail[-200:]
    flag_modified(row, "audit_trail_json")


async def _get_or_create_monitoring_state(db: AsyncSession) -> WorkspaceMonitoringState:
    row = (await db.execute(select(WorkspaceMonitoringState).order_by(WorkspaceMonitoringState.id.asc()))).scalars().first()
    if row:
        return row

    created = WorkspaceMonitoringState(
        desired_running=False,
        runtime_status="stopped",
        scan_trigger_mode="interval",
        interval_seconds=MONITORING_DEFAULT_INTERVAL_SECONDS,
        freshness_threshold_seconds=MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
        cooldown_seconds=MONITORING_DEFAULT_COOLDOWN_SECONDS,
        max_scan_rate=MONITORING_DEFAULT_MAX_SCAN_RATE,
        priority_zones=[],
        last_scan_at=None,
        scan_count=0,
        last_scan_reason="",
        last_deltas_json=[],
        last_proposal_ids=[],
        last_snapshot_json={},
        metadata_json={"recent_scans": []},
    )
    db.add(created)
    await db.flush()
    return created


def _snapshot_from_objects(rows: list[WorkspaceObjectMemory]) -> dict[str, dict]:
    return {
        str(item.id): {
            "zone": item.zone,
            "status": item.status,
            "confidence": float(item.confidence),
            "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else "",
            "canonical_name": item.canonical_name,
        }
        for item in rows
    }


def _compute_object_deltas(*, previous_snapshot: dict, current_rows: list[WorkspaceObjectMemory]) -> list[dict]:
    deltas: list[dict] = []
    current_snapshot = _snapshot_from_objects(current_rows)

    for row in current_rows:
        key = str(row.id)
        previous = previous_snapshot.get(key, {}) if isinstance(previous_snapshot, dict) else {}
        if not previous:
            deltas.append(
                {
                    "event": "new_object",
                    "object_memory_id": row.id,
                    "canonical_name": row.canonical_name,
                    "zone": row.zone,
                    "status": row.status,
                    "confidence": row.confidence,
                }
            )
            continue

        previous_zone = str(previous.get("zone", ""))
        if previous_zone and previous_zone != row.zone:
            deltas.append(
                {
                    "event": "object_moved",
                    "object_memory_id": row.id,
                    "canonical_name": row.canonical_name,
                    "from_zone": previous_zone,
                    "to_zone": row.zone,
                    "confidence": row.confidence,
                }
            )

        previous_status = str(previous.get("status", ""))
        if row.status in {"missing", "stale"} and previous_status not in {"missing", "stale"}:
            deltas.append(
                {
                    "event": "object_missing",
                    "object_memory_id": row.id,
                    "canonical_name": row.canonical_name,
                    "zone": row.zone,
                    "status": row.status,
                    "confidence": row.confidence,
                }
            )

        try:
            previous_confidence = float(previous.get("confidence", row.confidence))
        except (TypeError, ValueError):
            previous_confidence = float(row.confidence)
        if abs(float(row.confidence) - previous_confidence) >= 0.1:
            deltas.append(
                {
                    "event": "confidence_changed",
                    "object_memory_id": row.id,
                    "canonical_name": row.canonical_name,
                    "zone": row.zone,
                    "from_confidence": round(previous_confidence, 3),
                    "to_confidence": round(float(row.confidence), 3),
                }
            )

    previous_ids = set(previous_snapshot.keys()) if isinstance(previous_snapshot, dict) else set()
    current_ids = set(current_snapshot.keys())
    for orphan_id in sorted(previous_ids - current_ids):
        previous = previous_snapshot.get(orphan_id, {}) if isinstance(previous_snapshot, dict) else {}
        deltas.append(
            {
                "event": "object_missing",
                "object_memory_id": int(orphan_id),
                "canonical_name": str(previous.get("canonical_name", "unknown")),
                "zone": str(previous.get("zone", "")),
                "status": "missing",
                "confidence": float(previous.get("confidence", 0.0) or 0.0),
            }
        )

    return deltas


async def _create_monitoring_delta_proposal(
    *,
    db: AsyncSession,
    proposal_type: str,
    title: str,
    description: str,
    confidence: float,
    related_zone: str,
    related_object_id: int | None,
    trigger_json: dict,
) -> WorkspaceProposal | None:
    window_start = datetime.now(timezone.utc) - timedelta(seconds=MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS)
    existing = (
        await db.execute(
            select(WorkspaceProposal)
            .where(WorkspaceProposal.proposal_type == proposal_type)
            .where(WorkspaceProposal.status == "pending")
            .where(WorkspaceProposal.created_at >= window_start)
            .where(WorkspaceProposal.related_zone == related_zone)
            .where(WorkspaceProposal.related_object_id == related_object_id)
            .order_by(WorkspaceProposal.id.desc())
        )
    ).scalars().first()
    if existing:
        return None

    proposal = WorkspaceProposal(
        proposal_type=proposal_type,
        title=title,
        description=description,
        status="pending",
        confidence=max(0.0, min(1.0, confidence)),
        source="objective34",
        related_zone=related_zone,
        related_object_id=related_object_id,
        source_execution_id=None,
        trigger_json=trigger_json,
        metadata_json={
            "generated_by": "objective34",
            "integration": "objective33",
        },
    )
    db.add(proposal)
    await db.flush()
    await _refresh_workspace_proposal_priority(proposal=proposal, db=db)
    await _maybe_auto_execute_workspace_proposal(
        proposal=proposal,
        trigger_reason="objective34_delta",
        db=db,
    )
    return proposal


def _monitoring_should_scan(
    *,
    row: WorkspaceMonitoringState,
    objects: list[WorkspaceObjectMemory],
    now: datetime,
) -> tuple[bool, str]:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    recent_scans_raw = metadata.get("recent_scans", [])
    recent_scans: list[datetime] = []
    if isinstance(recent_scans_raw, list):
        for item in recent_scans_raw:
            if not isinstance(item, str):
                continue
            candidate = item[:-1] + "+00:00" if item.endswith("Z") else item
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            if (now - parsed).total_seconds() <= 60:
                recent_scans.append(parsed)

    if len(recent_scans) >= max(1, int(row.max_scan_rate)):
        return False, "max_scan_rate"

    if row.last_scan_at and (now - row.last_scan_at).total_seconds() < max(0, int(row.cooldown_seconds)):
        return False, "cooldown"

    if row.scan_trigger_mode == "freshness":
        priority = {item.strip() for item in (row.priority_zones if isinstance(row.priority_zones, list) else []) if str(item).strip()}
        threshold = max(30, int(row.freshness_threshold_seconds))
        for item in objects:
            if priority and item.zone not in priority:
                continue
            age_seconds = max((now - item.last_seen_at).total_seconds(), 0.0)
            if age_seconds >= threshold:
                return True, "freshness_drop"
        return False, "freshness_not_due"

    if not row.last_scan_at:
        return True, "interval_bootstrap"
    if (now - row.last_scan_at).total_seconds() >= max(1, int(row.interval_seconds)):
        return True, "interval_tick"
    return False, "interval_wait"


async def _run_monitoring_scan_cycle(*, db: AsyncSession, row: WorkspaceMonitoringState, reason: str) -> None:
    now = datetime.now(timezone.utc)
    objects = (
        await db.execute(select(WorkspaceObjectMemory).order_by(WorkspaceObjectMemory.id.asc()))
    ).scalars().all()

    previous_snapshot = row.last_snapshot_json if isinstance(row.last_snapshot_json, dict) else {}
    deltas = _compute_object_deltas(previous_snapshot=previous_snapshot, current_rows=objects)

    proposal_ids: list[int] = []
    for delta in deltas:
        event = str(delta.get("event", ""))
        object_id = int(delta.get("object_memory_id", 0)) if str(delta.get("object_memory_id", "")).isdigit() else None
        related_zone = str(delta.get("to_zone") or delta.get("zone") or "").strip()
        object_name = str(delta.get("canonical_name", "object"))
        if event == "object_moved":
            proposal = await _create_monitoring_delta_proposal(
                db=db,
                proposal_type=MONITORING_RECHECK_PROPOSAL_TYPE,
                title=f"Re-check moved object: {object_name}",
                description=f"Continuous monitoring detected movement for {object_name}; propose a re-check scan.",
                confidence=float(delta.get("confidence", 0.7) or 0.7),
                related_zone=related_zone,
                related_object_id=object_id,
                trigger_json={"event": event, **delta},
            )
            if proposal:
                proposal_ids.append(proposal.id)

        if event == "object_missing":
            adjacent = await _adjacent_zones(related_zone, db)
            proposal = await _create_monitoring_delta_proposal(
                db=db,
                proposal_type=MONITORING_SEARCH_PROPOSAL_TYPE,
                title=f"Search adjacent zone for missing object: {object_name}",
                description=f"Continuous monitoring detected {object_name} as missing; propose adjacent-zone search.",
                confidence=max(float(delta.get("confidence", 0.55) or 0.55), 0.55),
                related_zone=related_zone,
                related_object_id=object_id,
                trigger_json={"event": event, "adjacent_zones": adjacent, **delta},
            )
            if proposal:
                proposal_ids.append(proposal.id)

    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    recent_scans_raw = metadata.get("recent_scans", [])
    recent_scan_strings = [item for item in recent_scans_raw if isinstance(item, str)] if isinstance(recent_scans_raw, list) else []
    recent_scan_strings.append(now.isoformat())
    filtered_recent: list[str] = []
    for item in recent_scan_strings:
        candidate = item.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if (now - parsed).total_seconds() <= 60:
            filtered_recent.append(item)
    recent_scan_strings = filtered_recent

    row.last_scan_at = now
    row.scan_count = int(row.scan_count) + 1
    row.last_scan_reason = reason
    row.last_deltas_json = deltas
    row.last_proposal_ids = proposal_ids
    row.last_snapshot_json = _snapshot_from_objects(objects)
    row.runtime_status = "running"
    row.metadata_json = {
        **metadata,
        "recent_scans": recent_scan_strings,
        "last_cycle_object_count": len(objects),
    }

    await write_journal(
        db,
        actor="workspace-monitor",
        action="workspace_monitoring_scan",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary=f"Objective34 monitoring scan executed ({reason})",
        metadata_json={
            "scan_count": row.scan_count,
            "delta_count": len(deltas),
            "proposal_ids": proposal_ids,
        },
    )


async def _workspace_monitoring_loop() -> None:
    MONITORING_RUNTIME.loop_started_at = datetime.now(timezone.utc)
    while True:
        try:
            async with SessionLocal() as db:
                row = await _get_or_create_monitoring_state(db)
                if not row.desired_running:
                    row.runtime_status = "stopped"
                    await db.commit()
                    return

                objects = (await db.execute(select(WorkspaceObjectMemory))).scalars().all()
                should_scan, reason = _monitoring_should_scan(row=row, objects=objects, now=datetime.now(timezone.utc))
                if should_scan:
                    await _run_monitoring_scan_cycle(db=db, row=row, reason=reason)
                else:
                    row.runtime_status = "running"
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(1.0)
            continue

        await asyncio.sleep(1.0)


def _start_monitoring_runtime_if_needed() -> None:
    if MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done():
        return
    MONITORING_RUNTIME.task = asyncio.create_task(_workspace_monitoring_loop())


async def _stop_monitoring_runtime() -> None:
    if MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done():
        MONITORING_RUNTIME.task.cancel()
        try:
            await MONITORING_RUNTIME.task
        except asyncio.CancelledError:
            pass
    MONITORING_RUNTIME.task = None


async def initialize_workspace_monitoring_runtime() -> None:
    async with SessionLocal() as db:
        row = await _get_or_create_monitoring_state(db)
        if row.desired_running:
            row.runtime_status = "running"
            await db.commit()
            _start_monitoring_runtime_if_needed()
        else:
            row.runtime_status = "stopped"
            await db.commit()


async def shutdown_workspace_monitoring_runtime() -> None:
    await _stop_monitoring_runtime()


def _execution_policy_payload() -> dict:
    return {
        "policy_version": "objective33-v1",
        "allowed_capabilities": sorted(list(EXECUTION_ALLOWED_CAPABILITIES)),
        "default_collision_risk_threshold": SIMULATION_BLOCK_THRESHOLD_DEFAULT,
        "default_target_confidence_minimum": EXECUTION_TARGET_CONFIDENCE_MINIMUM_DEFAULT,
        "requires": {
            "operator_approved": True,
            "simulation_outcome": "plan_safe",
            "simulation_status": "completed",
            "simulation_gate_passed": True,
            "collision_risk_below_threshold": True,
            "target_confidence_at_or_above_minimum": True,
        },
    }


def _execution_precondition_violations(
    *,
    row: WorkspaceActionPlan,
    target: WorkspaceTargetResolution,
    collision_risk_threshold: float,
    target_confidence_minimum: float,
) -> list[str]:
    violations: list[str] = []
    if row.status != "approved":
        violations.append("operator_approval_required")
    if row.simulation_outcome != "plan_safe":
        violations.append("simulation_plan_safe_required")
    if row.simulation_status != "completed":
        violations.append("simulation_completion_required")
    if not row.simulation_gate_passed:
        violations.append("simulation_gate_pass_required")

    collision_risk = float((row.simulation_json or {}).get("collision_risk", 1.0)) if isinstance(row.simulation_json, dict) else 1.0
    if collision_risk >= collision_risk_threshold:
        violations.append("collision_risk_threshold_exceeded")

    if float(target.confidence) < target_confidence_minimum:
        violations.append("target_confidence_below_minimum")
    return violations


async def _ensure_execution_capability_registered(*, capability_name: str, db: AsyncSession) -> CapabilityRegistration:
    row = (
        await db.execute(select(CapabilityRegistration).where(CapabilityRegistration.capability_name == capability_name))
    ).scalars().first()
    safety_policy = {
        "scope": "actuating",
        "mode": "operator_guarded",
        "requires_simulation_safe": True,
    }
    if row:
        row.category = "manipulation"
        row.description = "Execute guarded reach/approach motion from simulated workspace action plan"
        row.requires_confirmation = True
        row.enabled = True
        row.safety_policy = safety_policy
        return row

    created = CapabilityRegistration(
        capability_name=capability_name,
        category="manipulation",
        description="Execute guarded reach/approach motion from simulated workspace action plan",
        requires_confirmation=True,
        enabled=True,
        safety_policy=safety_policy,
    )
    db.add(created)
    await db.flush()
    return created


def _execution_safety_score(*, collision_risk: float, target_confidence: float) -> float:
    return round(max(0.0, min(1.0, (1.0 - collision_risk) * target_confidence)), 3)


def _normalize_label(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = "".join(char if char.isalnum() else " " for char in lowered)
    return " ".join(cleaned.split())


def _label_score(target: str, candidate: str) -> float:
    left = _normalize_label(target)
    right = _normalize_label(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.85
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens.intersection(right_tokens))
    if overlap == 0:
        return 0.0
    return min(0.8, overlap / max(len(left_tokens), len(right_tokens)))


async def _adjacent_zones(zone_name: str, db: AsyncSession) -> list[str]:
    if not zone_name:
        return []
    row = (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == zone_name))).scalars().first()
    if not row:
        return []

    relations = (
        await db.execute(
            select(WorkspaceZoneRelation).where(
                WorkspaceZoneRelation.from_zone_id == row.id,
                WorkspaceZoneRelation.relation_type == "adjacent_to",
            )
        )
    ).scalars().all()

    names: list[str] = []
    for relation in relations:
        zone = await db.get(WorkspaceZone, relation.to_zone_id)
        if zone:
            names.append(zone.zone_name)
    return names


def _normalize_zone_for_map(zone_name: str) -> str:
    value = zone_name.strip()
    if not value:
        return ""
    if value in DEFAULT_ZONE_GRAPH:
        return value
    for candidate in DEFAULT_ZONE_GRAPH:
        if value.startswith(f"{candidate}-"):
            return candidate
    return value


async def _create_workspace_proposal_for_target(
    *,
    title: str,
    description: str,
    proposal_type: str,
    confidence: float,
    related_zone: str,
    related_object_id: int | None,
    trigger_json: dict,
    db: AsyncSession,
) -> WorkspaceProposal:
    proposal = WorkspaceProposal(
        proposal_type=proposal_type,
        title=title,
        description=description,
        status="pending",
        confidence=min(max(confidence, 0.0), 1.0),
        source="target_resolution",
        related_zone=related_zone,
        related_object_id=related_object_id,
        source_execution_id=None,
        trigger_json=trigger_json,
        metadata_json={"generated_by": "objective29"},
    )
    db.add(proposal)
    await db.flush()
    await _refresh_workspace_proposal_priority(proposal=proposal, db=db)
    await _maybe_auto_execute_workspace_proposal(
        proposal=proposal,
        trigger_reason="objective29_target_resolution",
        db=db,
    )
    return proposal


def _action_plan_policy(
    *,
    target: WorkspaceTargetResolution,
    action_type: str,
) -> tuple[str, list[dict], str]:
    if action_type not in SAFE_ACTION_TYPES:
        return "plan_rejected_unsupported_action", [], "rejected"

    steps = [
        {
            "step_index": 1,
            "type": "inspect_target_context",
            "description": "Review target resolution, zone, and policy state before progression.",
        }
    ]

    if target.policy_outcome == "target_confirmed":
        steps.extend(
            [
                {
                    "step_index": 2,
                    "type": action_type,
                    "description": "Prepare safe directed action under operator control.",
                },
                {
                    "step_index": 3,
                    "type": "queue_for_execution",
                    "description": "Queue approved safe action for downstream executor handoff.",
                },
            ]
        )
        return "plan_ready_for_approval", steps, "pending_approval"

    if target.policy_outcome in {"target_requires_confirmation", "target_stale_reobserve"}:
        steps.extend(
            [
                {
                    "step_index": 2,
                    "type": "request_confirmation",
                    "description": "Request operator confirmation or re-observation before any directed progression.",
                },
                {
                    "step_index": 3,
                    "type": "rescan",
                    "description": "Re-scan target zone and adjacent area to reduce ambiguity/staleness.",
                },
            ]
        )
        return "plan_requires_review", steps, "pending_review"

    if target.policy_outcome in {"target_not_found", "target_blocked_unsafe_zone"}:
        steps.extend(
            [
                {
                    "step_index": 2,
                    "type": "request_confirmation",
                    "description": "Hold progression and request explicit operator decision.",
                }
            ]
        )
        return "plan_blocked", steps, "blocked"

    steps.extend(
        [
            {
                "step_index": 2,
                "type": "request_confirmation",
                "description": "Unknown policy state; explicit operator confirmation required.",
            }
        ]
    )
    return "plan_requires_review", steps, "pending_review"


def _to_workspace_action_plan_out(row: WorkspaceActionPlan) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return {
        "plan_id": row.id,
        "target_resolution_id": row.target_resolution_id,
        "target_label": row.target_label,
        "target_zone": row.target_zone,
        "action_type": row.action_type,
        "safety_mode": row.safety_mode,
        "planning_outcome": row.planning_outcome,
        "status": row.status,
        "steps": row.steps_json if isinstance(row.steps_json, list) else [],
        "motion_plan": row.motion_plan_json if isinstance(row.motion_plan_json, dict) else {},
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
        "simulation": row.simulation_json if isinstance(row.simulation_json, dict) else {},
        "simulation_gate_passed": row.simulation_gate_passed,
        "execution_capability": row.execution_capability,
        "execution_status": row.execution_status,
        "execution_id": row.execution_id,
        "execution": row.execution_json if isinstance(row.execution_json, dict) else {},
        "abort_status": row.abort_status,
        "abort_reason": row.abort_reason,
        "queued_task_id": row.queued_task_id,
        "source": row.source,
        "metadata_json": metadata,
        "replan_history": _coerced_json_list(metadata.get("replan_history", [])),
        "created_at": row.created_at,
    }


async def _build_motion_plan(
    *,
    target_zone: str,
    target_label: str,
    action_type: str,
    db: AsyncSession,
) -> dict:
    mapped_zone = _normalize_zone_for_map(target_zone)
    adjacent = await _adjacent_zones(mapped_zone, db)
    approach_direction = "direct" if target_zone else "unknown"
    if target_zone.startswith("front"):
        approach_direction = "front_approach"
    elif target_zone.startswith("rear"):
        approach_direction = "rear_approach"

    return {
        "approach_vector": {
            "direction": approach_direction,
            "preferred_entry_zone": adjacent[0] if adjacent else target_zone,
        },
        "target_pose": {
            "zone": mapped_zone or target_zone,
            "label": target_label,
            "intent": action_type,
        },
        "clearance_zone": {
            "zone": mapped_zone or target_zone,
            "required_clearance_m": 0.35,
        },
        "estimated_path": {
            "zones": [*([adjacent[0]] if adjacent else []), mapped_zone or target_zone] if target_zone else [],
            "path_type": "safe_virtual_preview",
        },
        "collision_risk": 0.0,
    }


async def _simulate_action_plan(
    *,
    row: WorkspaceActionPlan,
    target: WorkspaceTargetResolution,
    collision_risk_threshold: float,
    db: AsyncSession,
) -> tuple[dict, str, bool]:
    zone_name = row.target_zone.strip()
    mapped_zone = _normalize_zone_for_map(zone_name)
    zone_row = (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped_zone))).scalars().first() if mapped_zone else None
    unknown_zone = bool(zone_name) and zone_row is None
    unsafe_zone = bool(zone_row and zone_row.hazard_level > 0)

    related_object = await db.get(WorkspaceObjectMemory, target.related_object_id) if target.related_object_id else None
    uncertain_identity = bool(related_object and related_object.status in {"uncertain", "stale", "missing"})

    obstacles: list[WorkspaceObjectMemory] = []
    if zone_name:
        candidates = (
            await db.execute(
                select(WorkspaceObjectMemory).where(
                    WorkspaceObjectMemory.zone == zone_name,
                    WorkspaceObjectMemory.status == "active",
                )
            )
        ).scalars().all()
        for item in candidates:
            if target.related_object_id and item.id == target.related_object_id:
                continue
            if item.confidence < 0.8:
                continue
            if _label_score(row.target_label, item.canonical_name) >= 0.9:
                continue
            obstacles.append(item)

    blocked_vectors: list[str] = []
    if len(obstacles) >= 1:
        blocked_vectors.append("direct")
    if len(obstacles) >= 2:
        blocked_vectors.append("side_approach")

    base_risk = 0.0
    base_risk += min(0.6, len(obstacles) * 0.3)
    if uncertain_identity:
        base_risk += 0.25
    if unsafe_zone:
        base_risk += 0.4
    if unknown_zone:
        base_risk += 0.5
    collision_risk = min(1.0, base_risk)

    reachable = not any([unknown_zone, unsafe_zone, len(obstacles) > 0])
    path_length = round(0.8 + (0.35 * len(obstacles)) + (0.25 if uncertain_identity else 0.0), 3)
    confidence = round(max(0.0, min(1.0, target.confidence * (1.0 - collision_risk))), 3)

    if unknown_zone or unsafe_zone:
        outcome = "plan_blocked"
    elif uncertain_identity:
        outcome = "plan_requires_adjustment"
    elif collision_risk > collision_risk_threshold or len(obstacles) > 0:
        outcome = "plan_blocked"
    else:
        outcome = "plan_safe"

    gate_passed = outcome == "plan_safe"

    simulation = {
        "reachable": reachable,
        "path_length": path_length,
        "collision_candidates": [
            {
                "object_memory_id": item.id,
                "canonical_name": item.canonical_name,
                "zone": item.zone,
                "status": item.status,
                "confidence": item.confidence,
            }
            for item in obstacles
        ],
        "blocked_approach_vectors": blocked_vectors,
        "collision_risk": collision_risk,
        "confidence": confidence,
        "outcome": outcome,
        "target_zone": zone_name,
        "target_zone_mapped": mapped_zone,
        "approach_direction": (row.motion_plan_json.get("approach_vector", {}) if isinstance(row.motion_plan_json, dict) else {}).get("direction", "unknown"),
        "clearance": (row.motion_plan_json.get("clearance_zone", {}) if isinstance(row.motion_plan_json, dict) else {}).get("required_clearance_m", 0.35),
        "obstacle_warnings": [
            *( ["unknown_zone"] if unknown_zone else [] ),
            *( ["unsafe_zone"] if unsafe_zone else [] ),
            *( ["uncertain_object_identity"] if uncertain_identity else [] ),
            *( [f"obstacle:{item.canonical_name}" for item in obstacles] ),
        ],
        "gate": {
            "collision_risk_threshold": collision_risk_threshold,
            "blocked": (outcome != "plan_safe"),
        },
    }

    return simulation, outcome, gate_passed


def _freshness_state(last_seen_at: datetime, now: datetime) -> str:
    age_seconds = max((now - last_seen_at).total_seconds(), 0.0)
    if age_seconds <= RECENT_WINDOW_SECONDS:
        return "recent"
    if age_seconds <= OUTDATED_WINDOW_SECONDS:
        return "aging"
    return "stale"


def _effective_confidence(confidence: float, freshness_state: str) -> float:
    if freshness_state == "recent":
        return min(max(confidence, 0.0), 1.0)
    if freshness_state == "aging":
        return min(max(confidence * 0.75, 0.0), 1.0)
    return min(max(confidence * 0.4, 0.0), 1.0)


def _apply_lifecycle_aging(observation: WorkspaceObservation, now: datetime) -> str:
    freshness = _freshness_state(observation.last_seen_at, now)
    if observation.lifecycle_status != "superseded":
        desired = "active" if freshness == "recent" else "outdated"
        observation.lifecycle_status = desired
    return freshness


def _to_workspace_observation_out(observation: WorkspaceObservation, now: datetime) -> dict:
    freshness = _freshness_state(observation.last_seen_at, now)
    return {
        "observation_id": observation.id,
        "timestamp": observation.last_seen_at,
        "zone": observation.zone,
        "detected_object": observation.label,
        "confidence": observation.confidence,
        "effective_confidence": _effective_confidence(observation.confidence, freshness),
        "freshness_state": freshness,
        "source": observation.source,
        "related_execution_id": observation.execution_id,
        "lifecycle_status": observation.lifecycle_status,
        "observation_count": observation.observation_count,
        "metadata_json": observation.metadata_json,
    }


def _apply_object_status_aging(item: WorkspaceObjectMemory, now: datetime) -> str:
    age_seconds = max((now - item.last_seen_at).total_seconds(), 0.0)
    if age_seconds > OBJECT_STALE_WINDOW_SECONDS and item.status != "stale":
        item.status = "stale"
    return item.status


def _to_workspace_object_out(item: WorkspaceObjectMemory, now: datetime) -> dict:
    freshness = _freshness_state(item.last_seen_at, now)
    return {
        "object_memory_id": item.id,
        "canonical_name": item.canonical_name,
        "aliases": item.candidate_labels if isinstance(item.candidate_labels, list) else [],
        "confidence": item.confidence,
        "effective_confidence": _effective_confidence(item.confidence, freshness),
        "zone": item.zone,
        "first_seen_at": item.first_seen_at,
        "last_seen_at": item.last_seen_at,
        "status": item.status,
        "last_execution_id": item.last_execution_id,
        "location_history": item.location_history if isinstance(item.location_history, list) else [],
        "metadata_json": item.metadata_json,
    }


async def _ensure_default_zone_map(db: AsyncSession) -> None:
    existing = (await db.execute(select(WorkspaceZone))).scalars().all()
    if existing:
        return

    zone_rows: dict[str, WorkspaceZone] = {}
    for zone_name, details in DEFAULT_ZONE_GRAPH.items():
        row = WorkspaceZone(
            zone_name=zone_name,
            display_name=zone_name.replace("-", " ").title(),
            hazard_level=int(details.get("hazard_level", 0)),
            metadata_json={"seeded": True},
        )
        db.add(row)
        zone_rows[zone_name] = row

    await db.flush()

    for zone_name, details in DEFAULT_ZONE_GRAPH.items():
        from_row = zone_rows[zone_name]
        for relation_type in ["adjacent_to", "left_of", "right_of", "in_front_of", "behind"]:
            targets = details.get(relation_type, [])
            if not isinstance(targets, list):
                continue
            for target_zone in targets:
                target_row = zone_rows.get(str(target_zone))
                if not target_row:
                    continue
                db.add(
                    WorkspaceZoneRelation(
                        from_zone_id=from_row.id,
                        to_zone_id=target_row.id,
                        relation_type=relation_type,
                        confidence=1.0,
                        metadata_json={"seeded": True},
                    )
                )

    await db.commit()


@router.get("/observations")
async def list_workspace_observations(
    zone: str = "",
    include_superseded: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _ensure_default_zone_map(db)
    stmt = select(WorkspaceObservation).order_by(WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc())
    if zone.strip():
        stmt = stmt.where(WorkspaceObservation.zone == zone.strip())
    if not include_superseded:
        stmt = stmt.where(WorkspaceObservation.lifecycle_status != "superseded")

    rows = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)

    changed = False
    for row in rows:
        before = row.lifecycle_status
        _apply_lifecycle_aging(row, now)
        if before != row.lifecycle_status:
            changed = True

    if changed:
        await db.commit()
        for row in rows:
            await db.refresh(row)

    return {
        "observations": [_to_workspace_observation_out(row, now) for row in rows],
    }


@router.get("/observations/{observation_id}")
async def get_workspace_observation(observation_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    observation = await db.get(WorkspaceObservation, observation_id)
    if not observation:
        raise HTTPException(status_code=404, detail="workspace observation not found")

    now = datetime.now(timezone.utc)
    before = observation.lifecycle_status
    _apply_lifecycle_aging(observation, now)
    if before != observation.lifecycle_status:
        await db.commit()
        await db.refresh(observation)

    return _to_workspace_observation_out(observation, now)


@router.get("/objects")
async def list_workspace_objects(
    label: str = "",
    zone: str = "",
    include_stale: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _ensure_default_zone_map(db)
    stmt = select(WorkspaceObjectMemory).order_by(WorkspaceObjectMemory.last_seen_at.desc(), WorkspaceObjectMemory.id.desc())
    if zone.strip():
        stmt = stmt.where(WorkspaceObjectMemory.zone == zone.strip())

    rows = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)

    changed = False
    filtered: list[WorkspaceObjectMemory] = []
    wanted = label.strip().lower()
    for row in rows:
        before = row.status
        _apply_object_status_aging(row, now)
        if before != row.status:
            changed = True

        if not include_stale and row.status == "stale":
            continue

        if wanted:
            aliases = row.candidate_labels if isinstance(row.candidate_labels, list) else []
            candidates = {row.canonical_name.lower(), *[str(item).lower() for item in aliases]}
            if not any(wanted in value for value in candidates):
                continue
        filtered.append(row)

    if changed:
        await db.commit()
        for row in filtered:
            await db.refresh(row)

    return {
        "objects": [_to_workspace_object_out(row, now) for row in filtered],
    }


@router.get("/objects/{object_memory_id}")
async def get_workspace_object(object_memory_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await _ensure_default_zone_map(db)
    row = await db.get(WorkspaceObjectMemory, object_memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace object not found")

    now = datetime.now(timezone.utc)
    before = row.status
    _apply_object_status_aging(row, now)
    if before != row.status:
        await db.commit()
        await db.refresh(row)

    return _to_workspace_object_out(row, now)


@router.get("/objects/{object_memory_id}/relations")
async def get_workspace_object_relations(object_memory_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await _ensure_default_zone_map(db)
    row = await db.get(WorkspaceObjectMemory, object_memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace object not found")

    relations = (
        await db.execute(
            select(WorkspaceObjectRelation)
            .where(
                (WorkspaceObjectRelation.subject_object_id == object_memory_id)
                | (WorkspaceObjectRelation.object_object_id == object_memory_id)
            )
            .order_by(WorkspaceObjectRelation.last_seen_at.desc(), WorkspaceObjectRelation.id.desc())
        )
    ).scalars().all()

    related_ids: set[int] = set()
    for relation in relations:
        related_ids.add(relation.subject_object_id)
        related_ids.add(relation.object_object_id)

    names: dict[int, str] = {}
    for related_id in related_ids:
        object_row = await db.get(WorkspaceObjectMemory, related_id)
        if object_row:
            names[related_id] = object_row.canonical_name

    return {
        "object_memory_id": object_memory_id,
        "canonical_name": row.canonical_name,
        "relations": [
            {
                "relation_id": relation.id,
                "subject_object_id": relation.subject_object_id,
                "subject_name": names.get(relation.subject_object_id, "unknown"),
                "object_object_id": relation.object_object_id,
                "object_name": names.get(relation.object_object_id, "unknown"),
                "relation_type": relation.relation_type,
                "relation_status": relation.relation_status,
                "confidence": relation.confidence,
                "last_seen_at": relation.last_seen_at,
                "source_execution_id": relation.source_execution_id,
                "metadata_json": relation.metadata_json,
            }
            for relation in relations
        ],
    }


@router.get("/map")
async def get_workspace_map(db: AsyncSession = Depends(get_db)) -> dict:
    await _ensure_default_zone_map(db)
    zones = (await db.execute(select(WorkspaceZone).order_by(WorkspaceZone.zone_name.asc()))).scalars().all()
    relations = (await db.execute(select(WorkspaceZoneRelation).order_by(WorkspaceZoneRelation.id.asc()))).scalars().all()

    names: dict[int, str] = {zone.id: zone.zone_name for zone in zones}
    return {
        "zones": [
            {
                "zone_id": zone.id,
                "zone_name": zone.zone_name,
                "display_name": zone.display_name,
                "hazard_level": zone.hazard_level,
                "metadata_json": zone.metadata_json,
            }
            for zone in zones
        ],
        "relations": [
            {
                "relation_id": relation.id,
                "from_zone": names.get(relation.from_zone_id, "unknown"),
                "to_zone": names.get(relation.to_zone_id, "unknown"),
                "relation_type": relation.relation_type,
                "confidence": relation.confidence,
                "metadata_json": relation.metadata_json,
            }
            for relation in relations
        ],
    }


@router.get("/map/zones")
async def get_workspace_map_zones(db: AsyncSession = Depends(get_db)) -> dict:
    await _ensure_default_zone_map(db)
    zones = (await db.execute(select(WorkspaceZone).order_by(WorkspaceZone.zone_name.asc()))).scalars().all()
    return {
        "zones": [
            {
                "zone_id": zone.id,
                "zone_name": zone.zone_name,
                "display_name": zone.display_name,
                "hazard_level": zone.hazard_level,
                "metadata_json": zone.metadata_json,
            }
            for zone in zones
        ]
    }


@router.get("/monitoring")
async def get_workspace_monitoring_status(db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get_or_create_monitoring_state(db)
    if row.desired_running and not (MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done()):
        _start_monitoring_runtime_if_needed()
        row.runtime_status = "running"
        await db.commit()
        await db.refresh(row)
    return _monitoring_state_payload(row)


@router.post("/monitoring/start")
async def start_workspace_monitoring(
    payload: WorkspaceMonitoringStartRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_or_create_monitoring_state(db)
    row.desired_running = True
    row.runtime_status = "running"
    row.scan_trigger_mode = payload.trigger_mode
    row.interval_seconds = payload.interval_seconds
    row.freshness_threshold_seconds = payload.freshness_threshold_seconds
    row.cooldown_seconds = payload.cooldown_seconds
    row.max_scan_rate = payload.max_scan_rate
    row.priority_zones = [item.strip() for item in payload.priority_zones if str(item).strip()]
    row.last_started_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "started_by": payload.actor,
        "start_reason": payload.reason,
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_monitoring_start",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary="Started continuous workspace monitoring loop",
        metadata_json={
            "policy": _monitoring_policy_payload(row),
            "reason": payload.reason,
        },
    )

    await db.commit()
    await db.refresh(row)
    _start_monitoring_runtime_if_needed()
    return _monitoring_state_payload(row)


@router.post("/monitoring/stop")
async def stop_workspace_monitoring(
    payload: WorkspaceMonitoringStopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_or_create_monitoring_state(db)
    if not payload.preserve_desired_running:
        row.desired_running = False
    row.runtime_status = "stopped"
    row.last_stopped_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "stopped_by": payload.actor,
        "stop_reason": payload.reason,
        "preserve_desired_running": payload.preserve_desired_running,
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_monitoring_stop",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary="Stopped continuous workspace monitoring loop",
        metadata_json={
            "preserve_desired_running": payload.preserve_desired_running,
            "reason": payload.reason,
        },
    )
    await db.commit()
    await _stop_monitoring_runtime()
    await db.refresh(row)
    return _monitoring_state_payload(row)


@router.get("/autonomy/policy")
async def get_workspace_autonomy_policy(db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get_or_create_monitoring_state(db)
    autonomy = _autonomy_state_from_monitoring(row)
    proposal_priority_policy = _proposal_priority_policy_from_monitoring(row)
    return {
        "tiers": sorted(list(AUTONOMY_POLICY_TIERS)),
        "proposal_policy_map": AUTONOMY_PROPOSAL_POLICY_MAP,
        "proposal_risk_scores": AUTONOMY_PROPOSAL_RISK_SCORE,
        "proposal_priority_policy": {
            "policy_version": proposal_priority_policy.get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
            "weights": proposal_priority_policy.get("weights", {}),
            "urgency_map": proposal_priority_policy.get("urgency_map", {}),
            "zone_importance": proposal_priority_policy.get("zone_importance", {}),
            "operator_preference": proposal_priority_policy.get("operator_preference", {}),
            "age_saturation_minutes": proposal_priority_policy.get("age_saturation_minutes", 120),
        },
        "autonomy": {
            key: value
            for key, value in autonomy.items()
            if key != "recent_auto_actions"
        },
    }


@router.post("/autonomy/override")
async def set_workspace_autonomy_override(
    payload: WorkspaceAutonomyOverrideRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_or_create_monitoring_state(db)
    autonomy = _autonomy_state_from_monitoring(row)

    if payload.auto_execution_enabled is not None:
        autonomy["auto_execution_enabled"] = payload.auto_execution_enabled
    if payload.force_manual_approval is not None:
        autonomy["force_manual_approval"] = payload.force_manual_approval
    if payload.max_auto_actions_per_minute is not None:
        autonomy["max_auto_actions_per_minute"] = payload.max_auto_actions_per_minute
    if payload.cooldown_between_actions_seconds is not None:
        autonomy["cooldown_between_actions_seconds"] = payload.cooldown_between_actions_seconds
    if payload.zone_action_limits:
        autonomy["zone_action_limits"] = {
            str(key).strip(): max(1, int(value))
            for key, value in payload.zone_action_limits.items()
            if str(key).strip()
        }
    if payload.auto_safe_confidence_threshold is not None:
        autonomy["auto_safe_confidence_threshold"] = payload.auto_safe_confidence_threshold
    if payload.auto_preferred_confidence_threshold is not None:
        autonomy["auto_preferred_confidence_threshold"] = payload.auto_preferred_confidence_threshold
    if payload.low_risk_score_max is not None:
        autonomy["low_risk_score_max"] = payload.low_risk_score_max
    if payload.reset_auto_history:
        autonomy["recent_auto_actions"] = []

    _store_autonomy_state(row, autonomy)

    if payload.pause_monitoring_loop:
        row.desired_running = False
        row.runtime_status = "paused"
        row.last_stopped_at = datetime.now(timezone.utc)

    if (
        payload.auto_execution_enabled is not None
        or payload.force_manual_approval is not None
        or payload.max_auto_actions_per_minute is not None
        or payload.cooldown_between_actions_seconds is not None
        or payload.auto_safe_confidence_threshold is not None
        or payload.auto_preferred_confidence_threshold is not None
        or payload.low_risk_score_max is not None
    ):
        await apply_learning_signal(db=db, signal="policy_override", user_id=payload.actor or DEFAULT_USER_ID)

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_autonomy_override",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary="Updated workspace autonomy overrides",
        metadata_json={
            "reason": payload.reason,
            "pause_monitoring_loop": payload.pause_monitoring_loop,
            "reset_auto_history": payload.reset_auto_history,
            "autonomy": {
                key: value
                for key, value in autonomy.items()
                if key != "recent_auto_actions"
            },
            **payload.metadata_json,
        },
    )

    await db.commit()
    if payload.pause_monitoring_loop:
        await _stop_monitoring_runtime()
    await db.refresh(row)
    return {
        "monitoring": _monitoring_state_payload(row),
        "autonomy": {
            key: value
            for key, value in autonomy.items()
            if key != "recent_auto_actions"
        },
    }


@router.get("/chains")
async def list_workspace_autonomous_chains(
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceAutonomousChain).order_by(WorkspaceAutonomousChain.id.desc())
    if status.strip():
        stmt = stmt.where(WorkspaceAutonomousChain.status == status.strip())
    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {
        "chains": [_to_workspace_autonomous_chain_out(row) for row in rows],
    }


@router.post("/chains")
async def create_workspace_autonomous_chain(
    payload: WorkspaceAutonomousChainCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal_ids = [int(item) for item in payload.proposal_ids if int(item) > 0]
    if not proposal_ids:
        raise HTTPException(status_code=422, detail="proposal_ids must include at least one proposal")

    valid_ids: list[int] = []
    for proposal_id in proposal_ids:
        proposal = await db.get(WorkspaceProposal, proposal_id)
        if proposal and proposal.status in {"pending", "accepted"}:
            valid_ids.append(proposal_id)

    if not valid_ids:
        raise HTTPException(status_code=422, detail="no valid proposals found for chain")

    step_policy = _normalized_chain_step_policy(payload.step_policy_json)

    row = WorkspaceAutonomousChain(
        chain_type=payload.chain_type,
        status="pending_approval" if payload.requires_approval else "active",
        source=payload.source,
        trigger_reason=payload.reason,
        step_proposal_ids=valid_ids,
        step_policy_json=step_policy,
        stop_on_failure=payload.stop_on_failure,
        cooldown_seconds=payload.cooldown_seconds,
        requires_approval=payload.requires_approval,
        current_step_index=0,
        completed_step_ids=[],
        failed_step_ids=[],
        audit_trail_json=[],
        metadata_json={
            "created_by": payload.actor,
            **payload.metadata_json,
        },
    )
    db.add(row)
    await db.flush()

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_autonomous_chain_create",
        target_type="workspace_chain",
        target_id=str(row.id),
        summary=f"Created workspace autonomous chain {row.id}",
        metadata_json={
            "proposal_ids": valid_ids,
            "chain_type": payload.chain_type,
            "reason": payload.reason,
            "requires_approval": payload.requires_approval,
            "cooldown_seconds": payload.cooldown_seconds,
            "stop_on_failure": payload.stop_on_failure,
            "step_policy_json": step_policy,
        },
    )

    _append_chain_audit(
        row,
        actor=payload.actor,
        event="chain_created",
        reason=payload.reason,
        metadata_json={
            "requires_approval": payload.requires_approval,
            "cooldown_seconds": payload.cooldown_seconds,
            "stop_on_failure": payload.stop_on_failure,
            "step_policy_json": step_policy,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_autonomous_chain_out(row)


@router.get("/chains/{chain_id}")
async def get_workspace_autonomous_chain(chain_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace autonomous chain not found")
    return _to_workspace_autonomous_chain_out(row)


@router.get("/chains/{chain_id}/audit")
async def get_workspace_autonomous_chain_audit(chain_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace autonomous chain not found")
    return {
        "chain_id": row.id,
        "status": row.status,
        "audit_trail": _coerced_json_list(row.audit_trail_json),
    }


@router.post("/chains/{chain_id}/approve")
async def approve_workspace_autonomous_chain(
    chain_id: int,
    payload: WorkspaceAutonomousChainApprovalRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace autonomous chain not found")
    if row.status in {"completed", "failed", "canceled"}:
        raise HTTPException(status_code=422, detail="workspace autonomous chain is terminal and cannot be approved")

    row.approved_by = payload.actor
    row.approved_at = datetime.now(timezone.utc)
    if row.status == "pending_approval":
        row.status = "active"

    _append_chain_audit(
        row,
        actor=payload.actor,
        event="chain_approved",
        reason=payload.reason,
        metadata_json=payload.metadata_json,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_autonomous_chain_approve",
        target_type="workspace_chain",
        target_id=str(row.id),
        summary=f"Approved workspace autonomous chain {row.id}",
        metadata_json={
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_autonomous_chain_out(row)


@router.post("/chains/{chain_id}/advance")
async def advance_workspace_autonomous_chain(
    chain_id: int,
    payload: WorkspaceAutonomousChainAdvanceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace autonomous chain not found")
    if row.status not in {"active", "pending"}:
        raise HTTPException(status_code=422, detail="workspace autonomous chain is not advanceable")

    if row.requires_approval and not row.approved_at:
        raise HTTPException(status_code=422, detail="workspace autonomous chain requires approval before advance")

    now = datetime.now(timezone.utc)
    if row.last_advanced_at and int(row.cooldown_seconds) > 0:
        elapsed = (now - row.last_advanced_at).total_seconds()
        if elapsed < int(row.cooldown_seconds):
            raise HTTPException(status_code=429, detail="workspace autonomous chain cooldown active")

    step_policy = _normalized_chain_step_policy(row.step_policy_json if isinstance(row.step_policy_json, dict) else {})

    proposal_ids = row.step_proposal_ids if isinstance(row.step_proposal_ids, list) else []
    if row.current_step_index >= len(proposal_ids):
        row.status = "completed"
        row.last_advanced_at = now
        _append_chain_audit(
            row,
            actor=payload.actor,
            event="chain_completed",
            reason=payload.reason,
            metadata_json={**payload.metadata_json},
        )
        await db.commit()
        await db.refresh(row)
        return _to_workspace_autonomous_chain_out(row)

    current_proposal_id = int(proposal_ids[row.current_step_index])
    proposal = await db.get(WorkspaceProposal, current_proposal_id)
    if not proposal:
        failed = list(row.failed_step_ids) if isinstance(row.failed_step_ids, list) else []
        failed.append(current_proposal_id)
        row.failed_step_ids = failed
        row.status = "failed" if row.stop_on_failure else "active"
        if not row.stop_on_failure:
            row.current_step_index = row.current_step_index + 1
            if row.current_step_index >= len(proposal_ids):
                row.status = "completed"
    else:
        proposal_status = str(proposal.status).strip().lower()
        is_terminal = proposal_status in set(step_policy.get("terminal_statuses", []))
        is_failure = proposal_status in set(step_policy.get("failure_statuses", []))
        if is_failure:
            failed = list(row.failed_step_ids) if isinstance(row.failed_step_ids, list) else []
            failed.append(current_proposal_id)
            row.failed_step_ids = failed
            if row.stop_on_failure and not payload.force:
                row.status = "failed"
            else:
                row.current_step_index = row.current_step_index + 1
                if row.current_step_index >= len(proposal_ids):
                    row.status = "completed"
                else:
                    row.status = "active"
        elif is_terminal or payload.force:
            completed = list(row.completed_step_ids) if isinstance(row.completed_step_ids, list) else []
            completed.append(current_proposal_id)
            row.completed_step_ids = completed
            row.current_step_index = row.current_step_index + 1
            if row.current_step_index >= len(proposal_ids):
                row.status = "completed"
            else:
                row.status = "active"
        else:
            row.status = "active"

    row.last_advanced_at = now

    _append_chain_audit(
        row,
        actor=payload.actor,
        event="chain_advanced",
        reason=payload.reason,
        metadata_json={
            "force": payload.force,
            "proposal_id": current_proposal_id,
            "current_step_index": row.current_step_index,
            **payload.metadata_json,
        },
    )

    if proposal and (proposal.status in {"accepted", "rejected"} or payload.force):
        completed = list(row.completed_step_ids) if isinstance(row.completed_step_ids, list) else []
        if current_proposal_id not in completed:
            completed.append(current_proposal_id)
            row.completed_step_ids = completed

    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "last_advance": {
            "actor": payload.actor,
            "reason": payload.reason,
            "force": payload.force,
            **payload.metadata_json,
        },
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_autonomous_chain_advance",
        target_type="workspace_chain",
        target_id=str(row.id),
        summary=f"Advanced workspace autonomous chain {row.id}",
        metadata_json={
            "current_step_index": row.current_step_index,
            "status": row.status,
            "proposal_id": current_proposal_id,
            "force": payload.force,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_autonomous_chain_out(row)


@router.get("/proposals")
async def list_workspace_proposals(
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceProposal).order_by(WorkspaceProposal.id.desc())
    if status.strip():
        stmt = stmt.where(WorkspaceProposal.status == status.strip())

    rows = (await db.execute(stmt)).scalars().all()[:limit]
    refresh_needed = False
    for row in rows:
        if status.strip() == "pending" or (not status.strip() and row.status == "pending"):
            await _refresh_workspace_proposal_priority(proposal=row, db=db)
            refresh_needed = True
    if refresh_needed:
        await db.commit()
    return {"proposals": [_workspace_proposal_payload(row) for row in rows]}


@router.get("/proposals/priority-policy")
async def get_workspace_proposal_priority_policy(db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get_or_create_monitoring_state(db)
    policy = _proposal_priority_policy_from_monitoring(row)
    return {
        "policy_version": policy.get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
        "weights": policy.get("weights", {}),
        "urgency_map": policy.get("urgency_map", {}),
        "zone_importance": policy.get("zone_importance", {}),
        "operator_preference": policy.get("operator_preference", {}),
        "age_saturation_minutes": policy.get("age_saturation_minutes", 120),
    }


@router.post("/proposals/priority-policy")
async def update_workspace_proposal_priority_policy(
    payload: WorkspaceProposalPriorityPolicyUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_or_create_monitoring_state(db)
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    existing = metadata.get("proposal_priority_policy", {}) if isinstance(metadata.get("proposal_priority_policy", {}), dict) else {}

    updated = {
        **existing,
        "weights": {
            **(existing.get("weights", {}) if isinstance(existing.get("weights", {}), dict) else {}),
            **{key: _normalize_score(value) for key, value in payload.weights.items() if str(key).strip()},
        },
        "urgency_map": {
            **(existing.get("urgency_map", {}) if isinstance(existing.get("urgency_map", {}), dict) else {}),
            **{str(key).strip(): _normalize_score(value) for key, value in payload.urgency_map.items() if str(key).strip()},
        },
        "zone_importance": {
            **(existing.get("zone_importance", {}) if isinstance(existing.get("zone_importance", {}), dict) else {}),
            **{str(key).strip(): _normalize_score(value) for key, value in payload.zone_importance.items() if str(key).strip()},
        },
        "operator_preference": {
            **(existing.get("operator_preference", {}) if isinstance(existing.get("operator_preference", {}), dict) else {}),
            **{str(key).strip(): _normalize_score(value) for key, value in payload.operator_preference.items() if str(key).strip()},
        },
        "version": PROPOSAL_PRIORITY_POLICY_VERSION,
    }
    if payload.age_saturation_minutes is not None:
        updated["age_saturation_minutes"] = payload.age_saturation_minutes

    row.metadata_json = {
        **metadata,
        "proposal_priority_policy": updated,
        "proposal_priority_policy_last_updated": {
            "actor": payload.actor,
            "reason": payload.reason,
            "at": datetime.now(timezone.utc).isoformat(),
            **payload.metadata_json,
        },
    }
    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_proposal_priority_policy_update",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary="Updated workspace proposal priority policy",
        metadata_json={
            "reason": payload.reason,
            "version": PROPOSAL_PRIORITY_POLICY_VERSION,
        },
    )
    await db.commit()
    policy = _proposal_priority_policy_from_monitoring(row)
    return {
        "updated": True,
        "policy_version": policy.get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
        "weights": policy.get("weights", {}),
        "urgency_map": policy.get("urgency_map", {}),
        "zone_importance": policy.get("zone_importance", {}),
        "operator_preference": policy.get("operator_preference", {}),
        "age_saturation_minutes": policy.get("age_saturation_minutes", 120),
    }


@router.get("/proposals/next")
async def get_next_workspace_proposal(
    actor: str = "scheduler",
    reason: str = "priority_selection",
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
) -> dict:
    status_filter = status.strip() or "pending"
    rows = (
        await db.execute(
            select(WorkspaceProposal)
            .where(WorkspaceProposal.status == status_filter)
            .order_by(WorkspaceProposal.created_at.asc(), WorkspaceProposal.id.asc())
        )
    ).scalars().all()
    if not rows:
        return {"proposal": None, "selected": False, "status": status_filter}

    scored: list[tuple[WorkspaceProposal, dict]] = []
    for row in rows:
        priority = await _refresh_workspace_proposal_priority(proposal=row, db=db)
        scored.append((row, priority))

    scored.sort(
        key=lambda item: (
            float(item[0].priority_score),
            float(item[0].confidence),
            item[0].created_at,
            item[0].id,
        ),
        reverse=True,
    )
    selected_row, selected_priority = scored[0]
    await write_journal(
        db,
        actor=actor,
        action="workspace_proposal_priority_next",
        target_type="workspace_proposal",
        target_id=str(selected_row.id),
        summary=f"Selected next workspace proposal {selected_row.id} by policy score",
        metadata_json={
            "reason": reason,
            "status_filter": status_filter,
            "priority_score": selected_row.priority_score,
            "priority_reason": selected_row.priority_reason,
            "policy_version": selected_priority.get("policy", {}).get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
            "priority_breakdown": selected_priority.get("breakdown", {}),
        },
    )
    notification = await _notification_payload_for_proposal(
        db=db,
        proposal=selected_row,
        action="selected_for_next",
    )
    await db.commit()
    return {
        "selected": True,
        "status": status_filter,
        "proposal": _workspace_proposal_payload(selected_row),
        "policy_version": selected_priority.get("policy", {}).get("version", PROPOSAL_PRIORITY_POLICY_VERSION),
        "priority_breakdown": selected_priority.get("breakdown", {}),
        "notification": notification,
    }


@router.get("/proposals/{proposal_id}")
async def get_workspace_proposal(proposal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceProposal, proposal_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace proposal not found")
    if row.status == "pending":
        await _refresh_workspace_proposal_priority(proposal=row, db=db)
        await db.commit()
    return _workspace_proposal_payload(row)


@router.post("/proposals/{proposal_id}/accept")
async def accept_workspace_proposal(
    proposal_id: int,
    payload: WorkspaceProposalActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await db.get(WorkspaceProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="workspace proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=422, detail="workspace proposal is not pending")

    task = Task(
        title=proposal.title,
        details=proposal.description,
        dependencies=[],
        acceptance_criteria="proposal accepted and queued for execution planning",
        assigned_to="tod",
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    proposal.status = "accepted"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": payload.actor,
        "accept_reason": payload.reason,
        "linked_task_id": task.id,
        **payload.metadata_json,
    }
    await apply_learning_signal(db=db, signal="proposal_accept", user_id=payload.actor or DEFAULT_USER_ID)
    notification = await _notification_payload_for_proposal(db=db, proposal=proposal, action="accepted")

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_proposal_accept",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=f"Accepted workspace proposal {proposal.id}",
        metadata_json={"task_id": task.id, "proposal_type": proposal.proposal_type},
    )
    await db.commit()
    await db.refresh(proposal)
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "linked_task_id": task.id,
        "notification": notification,
    }


@router.post("/proposals/{proposal_id}/reject")
async def reject_workspace_proposal(
    proposal_id: int,
    payload: WorkspaceProposalActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await db.get(WorkspaceProposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="workspace proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=422, detail="workspace proposal is not pending")

    proposal.status = "rejected"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "rejected_by": payload.actor,
        "reject_reason": payload.reason,
        **payload.metadata_json,
    }
    await apply_learning_signal(db=db, signal="proposal_reject", user_id=payload.actor or DEFAULT_USER_ID)
    notification = await _notification_payload_for_proposal(db=db, proposal=proposal, action="rejected")

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_proposal_reject",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=f"Rejected workspace proposal {proposal.id}",
        metadata_json={"proposal_type": proposal.proposal_type},
    )
    await db.commit()
    await db.refresh(proposal)
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "notification": notification,
    }


@router.post("/targets/resolve")
async def resolve_workspace_target(payload: WorkspaceTargetResolveRequest, db: AsyncSession = Depends(get_db)) -> dict:
    await _ensure_default_zone_map(db)

    rows = (await db.execute(select(WorkspaceObjectMemory).order_by(WorkspaceObjectMemory.last_seen_at.desc()))).scalars().all()
    requested_zone = payload.preferred_zone.strip()
    unsafe_set = {item.strip() for item in payload.unsafe_zones if item.strip()}
    preferred_threshold_raw = await get_user_preference_value(
        db=db,
        preference_type="preferred_confirmation_threshold",
        user_id=DEFAULT_USER_ID,
    )
    preferred_confirmation_threshold = max(0.5, min(0.99, float(preferred_threshold_raw or 0.9)))
    auto_exec_safe_tasks = bool(
        await get_user_preference_value(
            db=db,
            preference_type="auto_exec_safe_tasks",
            user_id=DEFAULT_USER_ID,
        )
    )
    if auto_exec_safe_tasks:
        preferred_confirmation_threshold = max(0.5, preferred_confirmation_threshold - 0.05)

    scored: list[tuple[WorkspaceObjectMemory, float]] = []
    for row in rows:
        aliases = row.candidate_labels if isinstance(row.candidate_labels, list) else []
        candidate_labels = [row.canonical_name, *[str(item) for item in aliases]]
        best_label_score = max((_label_score(payload.target_label, item) for item in candidate_labels), default=0.0)
        if best_label_score < 0.5:
            continue

        zone_bonus = 0.0
        if requested_zone and row.zone == requested_zone:
            zone_bonus = 0.1
        relation_bonus = 0.0
        if requested_zone and row.zone != requested_zone:
            adjacent = await _adjacent_zones(requested_zone, db)
            if row.zone in adjacent:
                relation_bonus = 0.05

        score = min(1.0, best_label_score + zone_bonus + relation_bonus)
        scored.append((row, score))

    scored.sort(key=lambda item: item[1], reverse=True)

    match_outcome = "no_match"
    policy_outcome = "target_not_found"
    status = "resolved"
    confidence = 0.0
    related_object_id: int | None = None
    candidate_object_ids: list[int] = []
    suggested_actions: list[str] = []
    trigger_json: dict = {}

    if not scored:
        suggested_actions = ["rescan target zone", "request operator confirmation"]
    else:
        candidate_object_ids = [row.id for row, _ in scored[:5]]
        top_row, top_score = scored[0]
        confidence = top_score

        if len(scored) > 1 and abs(scored[0][1] - scored[1][1]) < 0.05:
            match_outcome = "ambiguous_candidates"
            policy_outcome = "target_requires_confirmation"
            status = "pending_confirmation"
            suggested_actions = ["request operator confirmation", "rescan target zone"]
        elif top_score >= 0.95:
            match_outcome = "exact_match"
            related_object_id = top_row.id
        else:
            match_outcome = "likely_match"
            related_object_id = top_row.id

        if related_object_id is not None:
            if top_row.status in {"stale", "missing"}:
                policy_outcome = "target_stale_reobserve"
                status = "pending_confirmation"
                suggested_actions = ["rescan target zone", "rescan adjacent zone"]
            elif top_row.zone in unsafe_set:
                policy_outcome = "target_blocked_unsafe_zone"
                status = "blocked"
                suggested_actions = ["request operator confirmation"]
            elif match_outcome == "exact_match" and top_row.status == "active" and top_score >= preferred_confirmation_threshold:
                policy_outcome = "target_confirmed"
                status = "confirmed"
                suggested_actions = ["create proposal", "queue safely"]
            else:
                policy_outcome = "target_requires_confirmation"
                status = "pending_confirmation"
                suggested_actions = ["request operator confirmation", "rescan target zone"]

        trigger_json = {
            "requested_target": payload.target_label,
            "requested_zone": requested_zone,
            "top_score": top_score,
            "top_zone": top_row.zone,
            "top_status": top_row.status,
            "preferred_confirmation_threshold": preferred_confirmation_threshold,
            "auto_exec_safe_tasks": auto_exec_safe_tasks,
        }

    target = WorkspaceTargetResolution(
        requested_target=payload.target_label,
        requested_zone=requested_zone,
        match_outcome=match_outcome,
        policy_outcome=policy_outcome,
        status=status,
        confidence=confidence,
        related_object_id=related_object_id,
        candidate_object_ids=candidate_object_ids,
        suggested_actions=suggested_actions,
        source=payload.source,
        metadata_json={"unsafe_zones": sorted(list(unsafe_set)), "trigger": trigger_json},
    )
    db.add(target)
    await db.flush()

    proposal_id: int | None = None
    if payload.create_proposal and policy_outcome in {"target_confirmed", "target_requires_confirmation", "target_stale_reobserve"}:
        proposal_type = (
            "target_confirmed" if policy_outcome == "target_confirmed" else
            "target_reobserve" if policy_outcome == "target_stale_reobserve" else
            "target_confirmation"
        )
        proposal = await _create_workspace_proposal_for_target(
            title=f"Target resolution: {payload.target_label}",
            description=f"Policy outcome: {policy_outcome}",
            proposal_type=proposal_type,
            confidence=confidence,
            related_zone=requested_zone,
            related_object_id=related_object_id,
            trigger_json={"target_resolution_id": target.id, "policy_outcome": policy_outcome},
            db=db,
        )
        proposal_id = proposal.id

    await write_journal(
        db,
        actor="workspace",
        action="target_resolve",
        target_type="workspace_target",
        target_id=str(target.id),
        summary=f"Resolved target {payload.target_label}: {policy_outcome}",
        metadata_json={
            "match_outcome": match_outcome,
            "policy_outcome": policy_outcome,
            "related_object_id": related_object_id,
            "proposal_id": proposal_id,
        },
    )

    if proposal_id is not None:
        target.metadata_json = {
            **(target.metadata_json if isinstance(target.metadata_json, dict) else {}),
            "proposal_id": proposal_id,
        }

    await db.commit()
    await db.refresh(target)
    return {
        "target_resolution_id": target.id,
        "requested_target": target.requested_target,
        "requested_zone": target.requested_zone,
        "match_outcome": target.match_outcome,
        "policy_outcome": target.policy_outcome,
        "status": target.status,
        "confidence": target.confidence,
        "related_object_id": target.related_object_id,
        "candidate_object_ids": target.candidate_object_ids,
        "suggested_actions": target.suggested_actions,
        "metadata_json": target.metadata_json,
        "applied_confirmation_threshold": preferred_confirmation_threshold,
    }


@router.get("/targets/{target_resolution_id}")
async def get_workspace_target_resolution(target_resolution_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceTargetResolution, target_resolution_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")
    return {
        "target_resolution_id": row.id,
        "requested_target": row.requested_target,
        "requested_zone": row.requested_zone,
        "match_outcome": row.match_outcome,
        "policy_outcome": row.policy_outcome,
        "status": row.status,
        "confidence": row.confidence,
        "related_object_id": row.related_object_id,
        "candidate_object_ids": row.candidate_object_ids,
        "suggested_actions": row.suggested_actions,
        "source": row.source,
        "metadata_json": row.metadata_json,
        "created_at": row.created_at,
    }


@router.post("/targets/{target_resolution_id}/confirm")
async def confirm_workspace_target_resolution(
    target_resolution_id: int,
    payload: WorkspaceTargetConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceTargetResolution, target_resolution_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    if row.status not in {"pending_confirmation", "confirmed"}:
        raise HTTPException(status_code=422, detail="workspace target resolution is not confirmable")

    row.status = "confirmed"
    row.policy_outcome = "target_confirmed"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "confirmed_by": payload.actor,
        "confirm_reason": payload.reason,
        **payload.metadata_json,
    }

    proposal = await _create_workspace_proposal_for_target(
        title=f"Confirmed target: {row.requested_target}",
        description=f"Operator confirmed target resolution {row.id}",
        proposal_type="target_confirmed",
        confidence=row.confidence,
        related_zone=row.requested_zone,
        related_object_id=row.related_object_id,
        trigger_json={"target_resolution_id": row.id, "confirmed": True},
        db=db,
    )

    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "proposal_id": proposal.id,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="target_confirm",
        target_type="workspace_target",
        target_id=str(row.id),
        summary=f"Confirmed workspace target {row.id}",
        metadata_json={"proposal_id": proposal.id, "related_object_id": row.related_object_id},
    )

    await db.commit()
    await db.refresh(row)
    return {
        "target_resolution_id": row.id,
        "status": row.status,
        "policy_outcome": row.policy_outcome,
        "proposal_id": proposal.id,
    }


@router.post("/action-plans")
async def create_workspace_action_plan(
    payload: WorkspaceActionPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    target = await db.get(WorkspaceTargetResolution, payload.target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    planning_outcome, steps, status = _action_plan_policy(target=target, action_type=payload.action_type)

    row = WorkspaceActionPlan(
        target_resolution_id=target.id,
        target_label=target.requested_target,
        target_zone=target.requested_zone,
        action_type=payload.action_type,
        safety_mode="operator_controlled",
        planning_outcome=planning_outcome,
        status=status,
        steps_json=steps,
        motion_plan_json={},
        simulation_outcome="not_run",
        simulation_status="not_run",
        simulation_json={},
        simulation_gate_passed=False,
        execution_capability="",
        execution_status="not_started",
        execution_id=None,
        execution_json={},
        abort_status="not_aborted",
        abort_reason="",
        queued_task_id=None,
        source=payload.source,
        metadata_json={
            "target_policy_outcome": target.policy_outcome,
            "target_match_outcome": target.match_outcome,
            "notes": payload.notes,
            **payload.metadata_json,
        },
    )
    db.add(row)
    await db.flush()

    base_motion_plan = await _build_motion_plan(
        target_zone=target.requested_zone,
        target_label=target.requested_target,
        action_type=payload.action_type,
        db=db,
    )
    row.motion_plan_json = {
        **base_motion_plan,
        **(payload.motion_plan_overrides if isinstance(payload.motion_plan_overrides, dict) else {}),
    }

    await write_journal(
        db,
        actor="workspace",
        action="workspace_action_plan_create",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Created workspace action plan {row.id}: {planning_outcome}",
        metadata_json={
            "target_resolution_id": target.id,
            "planning_outcome": planning_outcome,
            "status": status,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_action_plan_out(row)


@router.get("/action-plans/{plan_id}")
async def get_workspace_action_plan(plan_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    return _to_workspace_action_plan_out(row)


@router.post("/action-plans/{plan_id}/approve")
async def approve_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanDecisionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    if row.status in {"queued", "rejected", "blocked"}:
        raise HTTPException(status_code=422, detail="workspace action plan cannot be approved")

    prior = row.status
    row.status = "approved"
    row.planning_outcome = "plan_approved"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "approved_by": payload.actor,
        "approve_reason": payload.reason,
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_approve",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Approved workspace action plan {row.id}: {prior}->approved",
        metadata_json={"prior_status": prior},
    )
    await db.commit()
    await db.refresh(row)
    return _to_workspace_action_plan_out(row)


@router.post("/action-plans/{plan_id}/reject")
async def reject_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanDecisionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    if row.status in {"queued", "rejected"}:
        raise HTTPException(status_code=422, detail="workspace action plan cannot be rejected")

    prior = row.status
    row.status = "rejected"
    row.planning_outcome = "plan_rejected"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "rejected_by": payload.actor,
        "reject_reason": payload.reason,
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_reject",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Rejected workspace action plan {row.id}: {prior}->rejected",
        metadata_json={"prior_status": prior},
    )
    await db.commit()
    await db.refresh(row)
    return _to_workspace_action_plan_out(row)


@router.post("/action-plans/{plan_id}/queue")
async def queue_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanHandoffRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    if row.status != "approved":
        raise HTTPException(status_code=422, detail="workspace action plan must be approved before queue")
    if row.simulation_status == "completed" and (row.simulation_outcome != "plan_safe" or not row.simulation_gate_passed):
        raise HTTPException(status_code=422, detail="workspace action plan simulation must pass before queue")

    task = Task(
        title=f"Execute safe workspace action: {row.action_type}",
        details=f"Target={row.target_label} zone={row.target_zone} plan={row.id}",
        dependencies=[],
        acceptance_criteria="execute queued workspace action plan under operator safeguards",
        assigned_to=payload.requested_executor,
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    row.queued_task_id = task.id
    row.status = "queued"
    row.planning_outcome = "plan_queued"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "queued_by": payload.actor,
        "queue_reason": payload.reason,
        "requested_executor": payload.requested_executor,
        "queue_handoff": {
            "task_id": task.id,
            "status": "queued",
            "executor": payload.requested_executor,
        },
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_queue",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Queued workspace action plan {row.id} to task {task.id}",
        metadata_json={"task_id": task.id, "requested_executor": payload.requested_executor},
    )

    await db.commit()
    await db.refresh(row)
    return {
        **_to_workspace_action_plan_out(row),
        "handoff": {
            "task_id": task.id,
            "task_status": task.state,
            "requested_executor": payload.requested_executor,
            "dispatch_decision": "queued_for_executor",
        },
    }


@router.post("/action-plans/{plan_id}/execute")
async def execute_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanExecuteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")

    if row.status != "approved":
        raise HTTPException(status_code=422, detail="workspace action plan must be approved before execute")
    if row.simulation_outcome != "plan_safe" or row.simulation_status != "completed" or not row.simulation_gate_passed:
        raise HTTPException(status_code=422, detail="workspace action plan simulation_status must be plan_safe before execute")

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    predictive_freshness = await _evaluate_predictive_freshness(action_plan=row, target=target, db=db)
    predictive_outcome = predictive_freshness.get("recommended_outcome", "continue_monitor")
    if predictive_outcome == "pause_and_resimulate":
        raise HTTPException(status_code=422, detail="workspace action plan requires resimulation before execute")
    if predictive_outcome == "require_replan":
        raise HTTPException(status_code=422, detail="workspace action plan requires replan before execute")
    if predictive_outcome == "abort_chain":
        raise HTTPException(status_code=422, detail="workspace action plan blocked by severe predictive drift")

    violations = _execution_precondition_violations(
        row=row,
        target=target,
        collision_risk_threshold=payload.collision_risk_threshold,
        target_confidence_minimum=payload.target_confidence_minimum,
    )
    if violations:
        raise HTTPException(status_code=422, detail=f"workspace action plan execution preconditions failed: {', '.join(violations)}")

    collision_risk = float((row.simulation_json or {}).get("collision_risk", 1.0)) if isinstance(row.simulation_json, dict) else 1.0

    if payload.capability_name not in EXECUTION_ALLOWED_CAPABILITIES:
        raise HTTPException(status_code=422, detail="workspace action plan capability is not allowed")

    capability = await _ensure_execution_capability_registered(capability_name=payload.capability_name, db=db)
    if not capability.enabled:
        raise HTTPException(status_code=422, detail="workspace action plan capability is disabled")

    task = Task(
        title=f"Execute workspace action plan {row.id}: {payload.capability_name}",
        details=f"Target={row.target_label} zone={row.target_zone} plan={row.id}",
        dependencies=[],
        acceptance_criteria="execute guarded workspace action plan under simulation and operator preconditions",
        assigned_to=payload.requested_executor,
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    event = InputEvent(
        source="api",
        raw_input=f"execute workspace action plan {row.id}",
        parsed_intent="execute_capability",
        confidence=target.confidence,
        target_system="tod",
        requested_goal=f"execute_plan:{row.id}",
        safety_flags=["requires_confirmation", "simulation_safe_required"],
        metadata_json={
            "plan_id": row.id,
            "target_resolution_id": row.target_resolution_id,
            "requested_executor": payload.requested_executor,
            "capability_name": payload.capability_name,
        },
        normalized=True,
    )
    db.add(event)
    await db.flush()

    motion_plan = row.motion_plan_json if isinstance(row.motion_plan_json, dict) else {}
    target_pose = motion_plan.get("target_pose", {}) if isinstance(motion_plan.get("target_pose", {}), dict) else {}
    approach_vector = motion_plan.get("approach_vector", {}) if isinstance(motion_plan.get("approach_vector", {}), dict) else {}
    clearance = motion_plan.get("clearance_zone", {}) if isinstance(motion_plan.get("clearance_zone", {}), dict) else {}
    safety_score = _execution_safety_score(collision_risk=collision_risk, target_confidence=target.confidence)

    execution = CapabilityExecution(
        input_event_id=event.id,
        resolution_id=None,
        goal_id=None,
        capability_name=payload.capability_name,
        arguments_json={
            "plan_id": row.id,
            "target_pose": target_pose,
            "approach_vector": approach_vector,
            "clearance": clearance,
            "safety_score": safety_score,
        },
        safety_mode="operator_controlled",
        requested_executor=payload.requested_executor,
        dispatch_decision="queued_for_executor",
        status="dispatched",
        reason=payload.reason or "workspace_action_plan_execute",
        feedback_json={
            "plan_id": row.id,
            "target_resolution_id": row.target_resolution_id,
            "task_id": task.id,
            "preconditions": {
                "simulation_outcome": row.simulation_outcome,
                "collision_risk": collision_risk,
                "collision_risk_threshold": payload.collision_risk_threshold,
                "operator_approved": row.status == "approved",
                "target_confidence": target.confidence,
                "target_confidence_minimum": payload.target_confidence_minimum,
            },
        },
    )
    db.add(execution)
    await db.flush()

    row.queued_task_id = task.id
    row.execution_capability = payload.capability_name
    row.execution_status = execution.status
    row.execution_id = execution.id
    row.execution_json = {
        "plan_id": row.id,
        "capability_name": payload.capability_name,
        "target_pose": target_pose,
        "approach_vector": approach_vector,
        "clearance": clearance,
        "safety_score": safety_score,
        "requested_executor": payload.requested_executor,
        "execution_id": execution.id,
        "task_id": task.id,
    }
    row.status = "executing"
    row.planning_outcome = "plan_executing"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "execution": {
            "queued_by": payload.actor,
            "queue_reason": payload.reason,
            "capability_name": payload.capability_name,
            "requested_executor": payload.requested_executor,
            "collision_risk_threshold": payload.collision_risk_threshold,
            "target_confidence_minimum": payload.target_confidence_minimum,
            **payload.metadata_json,
        },
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_execute",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Executed workspace action plan {row.id} via {payload.capability_name}",
        metadata_json={
            "execution_id": execution.id,
            "task_id": task.id,
            "requested_executor": payload.requested_executor,
            "safety_score": safety_score,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        **_to_workspace_action_plan_out(row),
        "handoff": {
            "task_id": task.id,
            "execution_id": execution.id,
            "requested_executor": payload.requested_executor,
            "dispatch_decision": execution.dispatch_decision,
            "feedback_endpoint": f"/gateway/capabilities/executions/{execution.id}/feedback",
        },
    }


@router.get("/execution-proposals/policy")
async def get_workspace_execution_proposal_policy() -> dict:
    return _execution_policy_payload()


@router.get("/execution-proposals")
async def list_workspace_execution_proposals(
    status: str = "pending",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceProposal).where(WorkspaceProposal.proposal_type == EXECUTION_PROPOSAL_TYPE).order_by(WorkspaceProposal.id.desc())
    if status.strip():
        stmt = stmt.where(WorkspaceProposal.status == status.strip())
    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {
        "policy": _execution_policy_payload(),
        "proposals": [
            {
                "proposal_id": row.id,
                "proposal_type": row.proposal_type,
                "title": row.title,
                "description": row.description,
                "status": row.status,
                "confidence": row.confidence,
                "source": row.source,
                "related_zone": row.related_zone,
                "related_object_id": row.related_object_id,
                "trigger_json": row.trigger_json,
                "metadata_json": row.metadata_json,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.post("/action-plans/{plan_id}/propose-execution")
async def propose_workspace_action_plan_execution(
    plan_id: int,
    payload: WorkspaceExecutionProposalCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    pending_rows = (
        await db.execute(
            select(WorkspaceProposal).where(
                WorkspaceProposal.proposal_type == EXECUTION_PROPOSAL_TYPE,
                WorkspaceProposal.status == "pending",
            )
        )
    ).scalars().all()
    for existing in pending_rows:
        trigger = existing.trigger_json if isinstance(existing.trigger_json, dict) else {}
        if int(trigger.get("plan_id", 0)) == row.id:
            return {
                "proposal_id": existing.id,
                "status": existing.status,
                "already_exists": True,
                "plan_id": row.id,
            }

    violations = _execution_precondition_violations(
        row=row,
        target=target,
        collision_risk_threshold=SIMULATION_BLOCK_THRESHOLD_DEFAULT,
        target_confidence_minimum=EXECUTION_TARGET_CONFIDENCE_MINIMUM_DEFAULT,
    )
    if violations:
        raise HTTPException(status_code=422, detail=f"workspace action plan cannot be proposed for execution: {', '.join(violations)}")

    simulation = row.simulation_json if isinstance(row.simulation_json, dict) else {}
    confidence = float(simulation.get("confidence", target.confidence))
    proposal = WorkspaceProposal(
        proposal_type=EXECUTION_PROPOSAL_TYPE,
        title=f"Autonomous execution proposal for plan {row.id}",
        description=f"Suggest executing {row.action_type} for target '{row.target_label}' under Objective33 policy.",
        status="pending",
        confidence=max(0.0, min(1.0, confidence)),
        source="objective33",
        related_zone=row.target_zone,
        related_object_id=target.related_object_id,
        source_execution_id=row.execution_id,
        trigger_json={
            "plan_id": row.id,
            "target_resolution_id": row.target_resolution_id,
            "recommended_capability": "reach_target",
            "preconditions": {
                "simulation_outcome": row.simulation_outcome,
                "simulation_status": row.simulation_status,
                "simulation_gate_passed": row.simulation_gate_passed,
                "collision_risk": float(simulation.get("collision_risk", 0.0)),
                "target_confidence": target.confidence,
            },
        },
        metadata_json={
            "proposed_by": payload.actor,
            "proposal_reason": payload.reason,
            "objective": "objective33",
            "policy": _execution_policy_payload(),
            **payload.metadata_json,
        },
    )
    db.add(proposal)
    await db.flush()
    await _refresh_workspace_proposal_priority(proposal=proposal, db=db)

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_propose_execution",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Proposed autonomous execution for plan {row.id} via proposal {proposal.id}",
        metadata_json={"proposal_id": proposal.id, "plan_id": row.id},
    )

    await db.commit()
    await db.refresh(proposal)
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "plan_id": row.id,
        "recommended_capability": "reach_target",
        "policy": _execution_policy_payload(),
    }


@router.post("/execution-proposals/{proposal_id}/accept")
async def accept_workspace_execution_proposal(
    proposal_id: int,
    payload: WorkspaceExecutionProposalActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await db.get(WorkspaceProposal, proposal_id)
    if not proposal or proposal.proposal_type != EXECUTION_PROPOSAL_TYPE:
        raise HTTPException(status_code=404, detail="workspace execution proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=422, detail="workspace execution proposal is not pending")

    trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
    plan_id = int(trigger.get("plan_id", 0))
    if plan_id <= 0:
        raise HTTPException(status_code=422, detail="workspace execution proposal missing plan reference")

    proposal.status = "accepted"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": payload.actor,
        "accept_reason": payload.reason,
        **payload.metadata_json,
    }

    execution = await execute_workspace_action_plan(
        plan_id=plan_id,
        payload=WorkspaceActionPlanExecuteRequest(
            actor=payload.actor,
            reason=payload.reason or "accepted_autonomous_execution_proposal",
            requested_executor=payload.requested_executor,
            capability_name=payload.capability_name,
            collision_risk_threshold=payload.collision_risk_threshold,
            target_confidence_minimum=payload.target_confidence_minimum,
            metadata_json={"proposal_id": proposal.id, **payload.metadata_json},
        ),
        db=db,
    )
    return {
        "proposal_id": proposal.id,
        "proposal_status": proposal.status,
        "plan_execution": execution,
    }


@router.post("/execution-proposals/{proposal_id}/reject")
async def reject_workspace_execution_proposal(
    proposal_id: int,
    payload: WorkspaceExecutionProposalActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal = await db.get(WorkspaceProposal, proposal_id)
    if not proposal or proposal.proposal_type != EXECUTION_PROPOSAL_TYPE:
        raise HTTPException(status_code=404, detail="workspace execution proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=422, detail="workspace execution proposal is not pending")

    proposal.status = "rejected"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "rejected_by": payload.actor,
        "reject_reason": payload.reason,
        **payload.metadata_json,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_execution_proposal_reject",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=f"Rejected autonomous execution proposal {proposal.id}",
        metadata_json={"proposal_id": proposal.id},
    )
    await db.commit()
    await db.refresh(proposal)
    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
    }


@router.post("/action-plans/{plan_id}/abort")
async def abort_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanAbortRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    if not row.execution_id:
        raise HTTPException(status_code=422, detail="workspace action plan has no active execution")

    execution = await db.get(CapabilityExecution, row.execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    if execution.status in {"succeeded", "failed", "blocked"}:
        raise HTTPException(status_code=422, detail="workspace action plan execution cannot be aborted in current state")

    prior_execution_status = execution.status
    history = list(execution.feedback_json.get("history", [])) if isinstance(execution.feedback_json, dict) else []
    history.append(
        {
            "from": prior_execution_status,
            "to": "blocked",
            "reason": payload.reason or "execution_aborted",
            "actor": payload.actor,
            "runtime_outcome": "guardrail_blocked",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    execution.status = "blocked"
    execution.reason = payload.reason or "execution_aborted"
    execution.feedback_json = {
        **(execution.feedback_json if isinstance(execution.feedback_json, dict) else {}),
        "abort": {
            "aborted": True,
            "actor": payload.actor,
            "reason": payload.reason,
            "metadata_json": payload.metadata_json,
            "aborted_at": datetime.now(timezone.utc).isoformat(),
        },
        "history": history,
    }

    row.execution_status = "aborted"
    row.abort_status = "aborted"
    row.abort_reason = payload.reason or "execution_aborted"
    row.status = "aborted"
    row.planning_outcome = "plan_aborted"
    row.execution_json = {
        **(row.execution_json if isinstance(row.execution_json, dict) else {}),
        "abort": {
            "aborted": True,
            "actor": payload.actor,
            "reason": payload.reason,
            "metadata_json": payload.metadata_json,
            "aborted_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_abort",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Aborted workspace action plan {row.id}: {prior_execution_status}->blocked",
        metadata_json={
            "execution_id": execution.id,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        **_to_workspace_action_plan_out(row),
        "abort": {
            "execution_id": execution.id,
            "prior_execution_status": prior_execution_status,
            "current_execution_status": execution.status,
            "reason": execution.reason,
        },
    }


@router.post("/action-plans/{plan_id}/simulate")
async def simulate_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanSimulationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    if row.status in {"rejected", "queued"}:
        raise HTTPException(status_code=422, detail="workspace action plan cannot be simulated in current state")

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    simulation, outcome, gate_passed = await _simulate_action_plan(
        row=row,
        target=target,
        collision_risk_threshold=payload.collision_risk_threshold,
        db=db,
    )

    if isinstance(row.motion_plan_json, dict):
        row.motion_plan_json = {
            **row.motion_plan_json,
            "collision_risk": simulation.get("collision_risk", 0.0),
            "estimated_path": {
                **(row.motion_plan_json.get("estimated_path", {}) if isinstance(row.motion_plan_json.get("estimated_path", {}), dict) else {}),
                "path_length": simulation.get("path_length", 0.0),
            },
        }

    row.simulation_outcome = outcome
    row.simulation_status = "completed"
    row.simulation_json = {
        **simulation,
        "simulated_by": payload.actor,
        "simulate_reason": payload.reason,
        "simulated_at": datetime.now(timezone.utc).isoformat(),
    }
    row.simulation_gate_passed = gate_passed
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "simulation": {
            "outcome": outcome,
            "gate_passed": gate_passed,
            "collision_risk": simulation.get("collision_risk", 0.0),
            "collision_risk_threshold": payload.collision_risk_threshold,
            **payload.metadata_json,
        },
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_simulate",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Simulated workspace action plan {row.id}: {outcome}",
        metadata_json={
            "outcome": outcome,
            "gate_passed": gate_passed,
            "collision_risk": simulation.get("collision_risk", 0.0),
            "collision_risk_threshold": payload.collision_risk_threshold,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_action_plan_out(row)


@router.get("/action-plans/{plan_id}/simulation")
async def get_workspace_action_plan_simulation(plan_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    return {
        "plan_id": row.id,
        "target_resolution_id": row.target_resolution_id,
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
        "simulation_gate_passed": row.simulation_gate_passed,
        "motion_plan": row.motion_plan_json if isinstance(row.motion_plan_json, dict) else {},
        "simulation": row.simulation_json if isinstance(row.simulation_json, dict) else {},
        "created_at": row.created_at,
    }


def _to_workspace_interruption_out(row: WorkspaceInterruptionEvent) -> dict:
    return {
        "interruption_id": row.id,
        "execution_id": row.execution_id,
        "action_plan_id": row.action_plan_id,
        "chain_id": row.chain_id,
        "interruption_type": row.interruption_type,
        "source": row.source,
        "requested_outcome": row.requested_outcome,
        "applied_outcome": row.applied_outcome,
        "status": row.status,
        "reason": row.reason,
        "actor": row.actor,
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def _to_workspace_replan_signal_out(row: WorkspaceReplanSignal) -> dict:
    return {
        "signal_id": row.id,
        "execution_id": row.execution_id,
        "action_plan_id": row.action_plan_id,
        "chain_id": row.chain_id,
        "signal_type": row.signal_type,
        "predicted_outcome": row.predicted_outcome,
        "confidence": row.confidence,
        "source": row.source,
        "status": row.status,
        "reason": row.reason,
        "actor": row.actor,
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


async def _active_replan_signals_for_execution(*, execution_id: int, db: AsyncSession) -> list[WorkspaceReplanSignal]:
    return (
        await db.execute(
            select(WorkspaceReplanSignal)
            .where(WorkspaceReplanSignal.execution_id == execution_id)
            .where(WorkspaceReplanSignal.status == "active")
            .order_by(WorkspaceReplanSignal.id.desc())
        )
    ).scalars().all()


def _merge_replan_outcomes(*, primary: str, secondary: str) -> str:
    primary_value = REPLAN_OUTCOME_SEVERITY.get(primary, 0)
    secondary_value = REPLAN_OUTCOME_SEVERITY.get(secondary, 0)
    return primary if primary_value >= secondary_value else secondary


async def _evaluate_predictive_freshness(
    *,
    action_plan: WorkspaceActionPlan | None,
    target: WorkspaceTargetResolution | None,
    db: AsyncSession,
) -> dict:
    now = datetime.now(timezone.utc)
    reasons: list[str] = []
    recommended_outcome = "continue_monitor"
    operator_confirmation_required = False
    confidence = 0.35

    freshness = {
        "target_memory_fresh": True,
        "map_context_stable": True,
        "simulation_assumptions_stable": True,
    }

    target_zone = ""
    related_object: WorkspaceObjectMemory | None = None
    if target:
        target_zone = target.requested_zone
        if target.related_object_id:
            related_object = await db.get(WorkspaceObjectMemory, target.related_object_id)

    if related_object is None:
        freshness["target_memory_fresh"] = False
        reasons.append("target_memory_missing")
        recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="require_replan")
        confidence = max(confidence, 0.75)
    else:
        age_seconds = max((now - related_object.last_seen_at).total_seconds(), 0.0)
        if related_object.status in {"missing", "stale"}:
            freshness["target_memory_fresh"] = False
            reasons.append("target_no_longer_valid")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="abort_chain")
            confidence = max(confidence, 0.92)
            operator_confirmation_required = True
        elif related_object.status == "uncertain":
            freshness["target_memory_fresh"] = False
            reasons.append("target_identity_uncertain")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="pause_and_resimulate")
            confidence = max(confidence, 0.7)
        elif age_seconds > OUTDATED_WINDOW_SECONDS:
            freshness["target_memory_fresh"] = False
            reasons.append("target_memory_stale")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="pause_and_resimulate")
            confidence = max(confidence, 0.62)

        if target_zone and related_object.zone and related_object.zone != target_zone:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("object_moved_since_plan")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="require_replan")
            confidence = max(confidence, 0.8)

        if target and float(related_object.confidence) + 0.05 < float(target.confidence):
            reasons.append("confidence_drop")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="require_replan")
            confidence = max(confidence, 0.78)
            operator_confirmation_required = True

    mapped_zone = _normalize_zone_for_map(target_zone)
    if mapped_zone:
        zone = (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped_zone))).scalars().first()
        if zone and int(zone.hazard_level) > 0:
            freshness["map_context_stable"] = False
            reasons.append("zone_state_changed")
            severe_outcome = "abort_chain" if int(zone.hazard_level) >= 2 else "require_replan"
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary=severe_outcome)
            confidence = max(confidence, 0.8)
            operator_confirmation_required = True

    if action_plan and isinstance(action_plan.simulation_json, dict):
        simulation = action_plan.simulation_json
        sim_target_zone = str(simulation.get("target_zone", "")).strip()
        if not sim_target_zone and isinstance(action_plan.motion_plan_json, dict):
            sim_target_zone = str((action_plan.motion_plan_json.get("target_pose", {}) if isinstance(action_plan.motion_plan_json.get("target_pose", {}), dict) else {}).get("zone", "")).strip()

        current_zone = related_object.zone if related_object else ""
        if sim_target_zone and current_zone and sim_target_zone != current_zone:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("simulation_target_zone_drift")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="require_replan")
            confidence = max(confidence, 0.79)

        try:
            simulation_collision_risk = float(simulation.get("collision_risk", 0.0))
        except (TypeError, ValueError):
            simulation_collision_risk = 0.0
        if simulation_collision_risk >= SIMULATION_BLOCK_THRESHOLD_DEFAULT:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("simulation_collision_risk_elevated")
            recommended_outcome = _merge_replan_outcomes(primary=recommended_outcome, secondary="require_replan")
            confidence = max(confidence, 0.76)

    if recommended_outcome in {"require_replan", "abort_chain"}:
        operator_confirmation_required = True

    return {
        "recommended_outcome": recommended_outcome,
        "operator_confirmation_required": operator_confirmation_required,
        "confidence": max(0.0, min(1.0, confidence)),
        "reasons": list(dict.fromkeys(reasons)),
        "freshness": freshness,
        "evaluated_at": now.isoformat(),
    }


@router.post("/executions/{execution_id}/predict-change")
async def predict_workspace_execution_change(
    execution_id: int,
    payload: WorkspaceExecutionPredictChangeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")

    action_plan = await _find_action_plan_for_execution(execution_id=execution.id, db=db)
    chains = await _find_chains_for_execution(execution_id=execution.id, db=db)

    signal = WorkspaceReplanSignal(
        execution_id=execution.id,
        action_plan_id=action_plan.id if action_plan else None,
        chain_id=chains[0].id if chains else None,
        signal_type=payload.signal_type,
        predicted_outcome=REPLAN_OUTCOME_MAP.get(payload.predicted_outcome, "continue_monitor"),
        confidence=payload.confidence,
        source=payload.source,
        status="active",
        reason=payload.reason,
        actor=payload.actor,
        metadata_json=payload.metadata_json,
    )
    db.add(signal)
    await db.flush()

    applied_hold = False
    predicted = signal.predicted_outcome
    if predicted in {"pause_and_resimulate", "require_replan", "abort_chain"} and execution.status in {"dispatched", "accepted", "running"}:
        execution.status = "paused"
        execution.dispatch_decision = "predictive_replan_hold"
        execution.reason = payload.reason or predicted
        applied_hold = True

        if action_plan:
            action_plan.status = "paused"
            action_plan.execution_status = "paused"
            if predicted == "pause_and_resimulate":
                action_plan.planning_outcome = "plan_requires_resimulation"
            elif predicted == "require_replan":
                action_plan.planning_outcome = "plan_requires_review"
            else:
                action_plan.planning_outcome = "plan_blocked"
            action_plan.metadata_json = {
                **(action_plan.metadata_json if isinstance(action_plan.metadata_json, dict) else {}),
                "predictive_hold": {
                    "signal_id": signal.id,
                    "predicted_outcome": predicted,
                    "reason": payload.reason,
                    "confidence": payload.confidence,
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            }

        for chain in chains:
            if predicted == "abort_chain":
                if chain.status not in {"completed", "failed", "canceled"}:
                    chain.status = "canceled"
            elif chain.status in {"active", "pending"}:
                chain.status = "paused"

            _append_chain_audit(
                chain,
                actor=payload.actor,
                event="chain_predictive_hold",
                reason=payload.reason or predicted,
                metadata_json={
                    "execution_id": execution.id,
                    "signal_id": signal.id,
                    "predicted_outcome": predicted,
                    "confidence": payload.confidence,
                    **payload.metadata_json,
                },
            )

    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    predictive_signals = list(feedback.get("predictive_signals", []))
    predictive_signals.append(
        {
            "signal_id": signal.id,
            "signal_type": signal.signal_type,
            "predicted_outcome": signal.predicted_outcome,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "timestamp": signal.created_at.isoformat() if signal.created_at else datetime.now(timezone.utc).isoformat(),
        }
    )
    execution.feedback_json = {
        **feedback,
        "predictive_signals": predictive_signals[-200:],
        "replan_required": predicted in {"require_replan", "abort_chain"},
        "latest_predictive_signal_id": signal.id,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_execution_predict_change",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Predictive change signal {signal.id} recorded for execution {execution.id}",
        metadata_json={
            "signal_id": signal.id,
            "signal_type": signal.signal_type,
            "predicted_outcome": signal.predicted_outcome,
            "confidence": signal.confidence,
            "applied_hold": applied_hold,
            "action_plan_id": action_plan.id if action_plan else None,
            "chain_ids": [item.id for item in chains],
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(execution)
    await db.refresh(signal)
    return {
        "signal": _to_workspace_replan_signal_out(signal),
        "execution_id": execution.id,
        "execution_status": execution.status,
        "dispatch_decision": execution.dispatch_decision,
        "applied_hold": applied_hold,
        "action_plan_id": action_plan.id if action_plan else None,
        "chain_ids": [item.id for item in chains],
    }


@router.get("/replan-signals")
async def list_workspace_replan_signals(
    execution_id: int | None = None,
    action_plan_id: int | None = None,
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceReplanSignal).order_by(WorkspaceReplanSignal.id.desc())
    if execution_id is not None:
        stmt = stmt.where(WorkspaceReplanSignal.execution_id == execution_id)
    if action_plan_id is not None:
        stmt = stmt.where(WorkspaceReplanSignal.action_plan_id == action_plan_id)
    if status.strip():
        stmt = stmt.where(WorkspaceReplanSignal.status == status.strip())

    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {
        "replan_signals": [_to_workspace_replan_signal_out(row) for row in rows],
    }


@router.get("/replan-signals/{signal_id}")
async def get_workspace_replan_signal(signal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceReplanSignal, signal_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace replan signal not found")
    return _to_workspace_replan_signal_out(row)


@router.post("/action-plans/{plan_id}/replan")
async def replan_workspace_action_plan(
    plan_id: int,
    payload: WorkspaceActionPlanReplanRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    signal: WorkspaceReplanSignal | None = None
    if payload.signal_id is not None:
        signal = await db.get(WorkspaceReplanSignal, payload.signal_id)
        if not signal:
            raise HTTPException(status_code=404, detail="workspace replan signal not found")
        if signal.action_plan_id not in {None, row.id} and signal.execution_id != row.execution_id:
            raise HTTPException(status_code=422, detail="workspace replan signal does not belong to this plan")
    else:
        signal = (
            await db.execute(
                select(WorkspaceReplanSignal)
                .where(WorkspaceReplanSignal.action_plan_id == row.id)
                .where(WorkspaceReplanSignal.status == "active")
                .order_by(WorkspaceReplanSignal.id.desc())
            )
        ).scalars().first()

    freshness = await _evaluate_predictive_freshness(action_plan=row, target=target, db=db)
    selected_outcome = freshness["recommended_outcome"]
    if signal:
        selected_outcome = _merge_replan_outcomes(primary=selected_outcome, secondary=signal.predicted_outcome)
    if payload.force and selected_outcome == "abort_chain":
        selected_outcome = "require_replan"

    prior_snapshot = {
        "plan_id": row.id,
        "status": row.status,
        "planning_outcome": row.planning_outcome,
        "steps": row.steps_json if isinstance(row.steps_json, list) else [],
        "motion_plan": row.motion_plan_json if isinstance(row.motion_plan_json, dict) else {},
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
    }

    planning_outcome, steps, _ = _action_plan_policy(target=target, action_type=row.action_type)
    status = "pending_approval"
    operator_confirmation_required = False
    if selected_outcome == "pause_and_resimulate":
        planning_outcome = "plan_requires_resimulation"
        status = "pending_review"
        operator_confirmation_required = True
    elif selected_outcome == "require_replan":
        planning_outcome = "plan_requires_review"
        status = "pending_review"
        operator_confirmation_required = True
    elif selected_outcome == "abort_chain":
        planning_outcome = "plan_blocked"
        status = "blocked"
        operator_confirmation_required = True
    else:
        planning_outcome = "plan_replanned"

    row.status = status
    row.planning_outcome = planning_outcome
    row.steps_json = steps
    row.simulation_outcome = "not_run"
    row.simulation_status = "not_run"
    row.simulation_gate_passed = False
    row.simulation_json = {}

    base_motion_plan = await _build_motion_plan(
        target_zone=target.requested_zone,
        target_label=target.requested_target,
        action_type=row.action_type,
        db=db,
    )
    row.motion_plan_json = {
        **base_motion_plan,
        **(payload.motion_plan_overrides if isinstance(payload.motion_plan_overrides, dict) else {}),
    }

    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    history = _coerced_json_list(metadata.get("replan_history", []))
    replan_entry = {
        "replan_id": len(history) + 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": payload.actor,
        "reason": payload.reason,
        "signal_id": signal.id if signal else None,
        "selected_outcome": selected_outcome,
        "operator_confirmation_required": operator_confirmation_required,
        "freshness": freshness,
        "prior_plan": prior_snapshot,
        "new_plan": {
            "status": row.status,
            "planning_outcome": row.planning_outcome,
            "steps": row.steps_json if isinstance(row.steps_json, list) else [],
            "motion_plan": row.motion_plan_json if isinstance(row.motion_plan_json, dict) else {},
        },
        "metadata_json": payload.metadata_json,
    }
    history.append(replan_entry)

    row.metadata_json = {
        **metadata,
        "replan_history": history[-200:],
        "latest_replan": {
            "signal_id": signal.id if signal else None,
            "selected_outcome": selected_outcome,
            "operator_confirmation_required": operator_confirmation_required,
            "reason": payload.reason,
            "actor": payload.actor,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    }

    if row.execution_id:
        execution = await db.get(CapabilityExecution, row.execution_id)
        if execution:
            execution.status = "paused"
            execution.dispatch_decision = "replan_required"
            execution.reason = payload.reason or selected_outcome
            feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
            execution.feedback_json = {
                **feedback,
                "replan_required": selected_outcome in {"require_replan", "abort_chain"},
                "latest_replan_plan_id": row.id,
                "latest_replan_signal_id": signal.id if signal else None,
                "latest_replan_outcome": selected_outcome,
            }

            chains = await _find_chains_for_execution(execution_id=execution.id, db=db)
            for chain in chains:
                if selected_outcome == "abort_chain":
                    if chain.status not in {"completed", "failed", "canceled"}:
                        chain.status = "canceled"
                elif chain.status in {"active", "pending"}:
                    chain.status = "paused"
                _append_chain_audit(
                    chain,
                    actor=payload.actor,
                    event="chain_replan",
                    reason=payload.reason or selected_outcome,
                    metadata_json={
                        "execution_id": execution.id,
                        "plan_id": row.id,
                        "selected_outcome": selected_outcome,
                        "signal_id": signal.id if signal else None,
                        **payload.metadata_json,
                    },
                )

    if signal:
        signal.status = "resolved"
        signal.resolved_by = payload.actor
        signal.resolved_at = datetime.now(timezone.utc)
        signal.action_plan_id = row.id
        signal.metadata_json = {
            **(signal.metadata_json if isinstance(signal.metadata_json, dict) else {}),
            "resolved_by_replan": True,
            "resolved_plan_id": row.id,
            "resolved_outcome": selected_outcome,
            "replan_reason": payload.reason,
            **payload.metadata_json,
        }

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_action_plan_replan",
        target_type="workspace_action_plan",
        target_id=str(row.id),
        summary=f"Replanned workspace action plan {row.id}: {selected_outcome}",
        metadata_json={
            "signal_id": signal.id if signal else None,
            "selected_outcome": selected_outcome,
            "operator_confirmation_required": operator_confirmation_required,
            "force": payload.force,
            "freshness": freshness,
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        **_to_workspace_action_plan_out(row),
        "replan": {
            "signal_id": signal.id if signal else None,
            "selected_outcome": selected_outcome,
            "operator_confirmation_required": operator_confirmation_required,
            "freshness": freshness,
        },
    }


@router.get("/action-plans/{plan_id}/replan-history")
async def get_workspace_action_plan_replan_history(plan_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")

    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    history = _coerced_json_list(metadata.get("replan_history", []))
    return {
        "plan_id": row.id,
        "replan_history": history,
    }


@router.get("/interruptions")
async def list_workspace_interruptions(
    execution_id: int | None = None,
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceInterruptionEvent).order_by(WorkspaceInterruptionEvent.id.desc())
    if execution_id is not None:
        stmt = stmt.where(WorkspaceInterruptionEvent.execution_id == execution_id)
    if status.strip():
        stmt = stmt.where(WorkspaceInterruptionEvent.status == status.strip())

    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {
        "interruptions": [_to_workspace_interruption_out(row) for row in rows],
    }


@router.get("/interruptions/{interruption_id}")
async def get_workspace_interruption(interruption_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(WorkspaceInterruptionEvent, interruption_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace interruption not found")
    return _to_workspace_interruption_out(row)


@router.post("/executions/{execution_id}/pause")
async def pause_workspace_execution(
    execution_id: int,
    payload: WorkspaceExecutionPauseRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    if execution.status not in {"dispatched", "accepted", "running"}:
        raise HTTPException(status_code=422, detail="execution is not in a pausable state")

    requested_outcome = _interruption_outcome_for_type(payload.interruption_type)
    prior_status = execution.status
    execution.status = "paused"
    execution.dispatch_decision = "interrupted_paused"
    execution.reason = payload.reason or payload.interruption_type
    _append_execution_interruption(
        execution,
        event=payload.interruption_type,
        actor=payload.actor,
        reason=execution.reason,
        metadata_json={
            "source": payload.source,
            **payload.metadata_json,
        },
    )

    action_plan = await _find_action_plan_for_execution(execution_id=execution.id, db=db)
    if action_plan:
        action_plan.status = "paused"
        action_plan.execution_status = "paused"
        action_plan.planning_outcome = "plan_paused"
        action_plan.metadata_json = {
            **(action_plan.metadata_json if isinstance(action_plan.metadata_json, dict) else {}),
            "interruption": {
                "type": payload.interruption_type,
                "actor": payload.actor,
                "source": payload.source,
                "reason": execution.reason,
                "at": datetime.now(timezone.utc).isoformat(),
                **payload.metadata_json,
            },
        }

    chains = await _find_chains_for_execution(execution_id=execution.id, db=db)
    for chain in chains:
        if chain.status in {"active", "pending"}:
            chain.status = "paused"
        _append_chain_audit(
            chain,
            actor=payload.actor,
            event="chain_paused",
            reason=execution.reason,
            metadata_json={
                "execution_id": execution.id,
                "interruption_type": payload.interruption_type,
                "source": payload.source,
                **payload.metadata_json,
            },
        )

    interruption = await _record_interruption_event(
        execution=execution,
        action_plan=action_plan,
        chain=chains[0] if chains else None,
        interruption_type=payload.interruption_type,
        source=payload.source,
        requested_outcome=requested_outcome,
        applied_outcome="paused",
        status="active",
        actor=payload.actor,
        reason=execution.reason,
        metadata_json={
            "chain_ids": [item.id for item in chains],
            **payload.metadata_json,
        },
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_execution_pause",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Paused workspace execution {execution.id}: {prior_status}->paused",
        metadata_json={
            "interruption_id": interruption.id,
            "interruption_type": payload.interruption_type,
            "source": payload.source,
            "requested_outcome": requested_outcome,
            "applied_outcome": "paused",
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(execution)
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "dispatch_decision": execution.dispatch_decision,
        "reason": execution.reason,
        "interruption": _to_workspace_interruption_out(interruption),
        "action_plan_id": action_plan.id if action_plan else None,
        "chain_ids": [item.id for item in chains],
    }


@router.post("/executions/{execution_id}/stop")
async def stop_workspace_execution(
    execution_id: int,
    payload: WorkspaceExecutionStopRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    if execution.status in {"succeeded", "failed", "blocked"}:
        raise HTTPException(status_code=422, detail="execution is already in a terminal state")

    requested_outcome = _interruption_outcome_for_type(payload.interruption_type)
    prior_status = execution.status
    execution.status = "blocked"
    execution.dispatch_decision = "interrupted_stopped"
    execution.reason = payload.reason or payload.interruption_type
    _append_execution_interruption(
        execution,
        event=payload.interruption_type,
        actor=payload.actor,
        reason=execution.reason,
        metadata_json={
            "source": payload.source,
            **payload.metadata_json,
        },
    )

    action_plan = await _find_action_plan_for_execution(execution_id=execution.id, db=db)
    if action_plan:
        action_plan.status = "aborted"
        action_plan.execution_status = "stopped"
        action_plan.abort_status = "aborted"
        action_plan.abort_reason = execution.reason
        action_plan.planning_outcome = "plan_aborted"
        action_plan.metadata_json = {
            **(action_plan.metadata_json if isinstance(action_plan.metadata_json, dict) else {}),
            "interruption": {
                "type": payload.interruption_type,
                "actor": payload.actor,
                "source": payload.source,
                "reason": execution.reason,
                "at": datetime.now(timezone.utc).isoformat(),
                **payload.metadata_json,
            },
        }

    chains = await _find_chains_for_execution(execution_id=execution.id, db=db)
    for chain in chains:
        if chain.status not in {"completed", "failed", "canceled"}:
            chain.status = "canceled"
        _append_chain_audit(
            chain,
            actor=payload.actor,
            event="chain_stopped",
            reason=execution.reason,
            metadata_json={
                "execution_id": execution.id,
                "interruption_type": payload.interruption_type,
                "source": payload.source,
                **payload.metadata_json,
            },
        )

    interruption = await _record_interruption_event(
        execution=execution,
        action_plan=action_plan,
        chain=chains[0] if chains else None,
        interruption_type=payload.interruption_type,
        source=payload.source,
        requested_outcome=requested_outcome,
        applied_outcome="stopped",
        status="applied",
        actor=payload.actor,
        reason=execution.reason,
        metadata_json={
            "chain_ids": [item.id for item in chains],
            **payload.metadata_json,
        },
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_execution_stop",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Stopped workspace execution {execution.id}: {prior_status}->blocked",
        metadata_json={
            "interruption_id": interruption.id,
            "interruption_type": payload.interruption_type,
            "source": payload.source,
            "requested_outcome": requested_outcome,
            "applied_outcome": "stopped",
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(execution)
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "dispatch_decision": execution.dispatch_decision,
        "reason": execution.reason,
        "interruption": _to_workspace_interruption_out(interruption),
        "action_plan_id": action_plan.id if action_plan else None,
        "chain_ids": [item.id for item in chains],
    }


@router.post("/executions/{execution_id}/resume")
async def resume_workspace_execution(
    execution_id: int,
    payload: WorkspaceExecutionResumeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    if execution.status != "paused":
        raise HTTPException(status_code=422, detail="execution is not paused")
    if not payload.safety_ack:
        raise HTTPException(status_code=422, detail="resume requires explicit safety_ack")

    action_plan = await _find_action_plan_for_execution(execution_id=execution.id, db=db)
    if action_plan:
        if action_plan.simulation_outcome != "plan_safe" or action_plan.simulation_status != "completed" or not action_plan.simulation_gate_passed:
            raise HTTPException(status_code=422, detail="resume blocked: action plan simulation is no longer safe")

    unresolved_blocking = (
        await db.execute(
            select(WorkspaceInterruptionEvent)
            .where(WorkspaceInterruptionEvent.execution_id == execution.id)
            .where(WorkspaceInterruptionEvent.status == "active")
            .order_by(WorkspaceInterruptionEvent.id.desc())
        )
    ).scalars().all()
    unresolved_blocking = [
        item for item in unresolved_blocking if _interruption_is_blocking(item.interruption_type)
    ]

    if unresolved_blocking and not payload.conditions_restored:
        raise HTTPException(status_code=422, detail="resume blocked: workspace changed and conditions are not restored")

    active_replan_signals = await _active_replan_signals_for_execution(execution_id=execution.id, db=db)
    highest_replan_outcome = "continue_monitor"
    for signal in active_replan_signals:
        highest_replan_outcome = _merge_replan_outcomes(primary=highest_replan_outcome, secondary=signal.predicted_outcome)

    target = await db.get(WorkspaceTargetResolution, action_plan.target_resolution_id) if action_plan else None
    predictive_freshness = await _evaluate_predictive_freshness(action_plan=action_plan, target=target, db=db)
    highest_replan_outcome = _merge_replan_outcomes(
        primary=highest_replan_outcome,
        secondary=predictive_freshness.get("recommended_outcome", "continue_monitor"),
    )

    if highest_replan_outcome in {"pause_and_resimulate", "require_replan", "abort_chain"} and not payload.conditions_restored:
        raise HTTPException(status_code=422, detail=f"resume blocked: predictive drift requires {highest_replan_outcome}")

    resolved_ids: list[int] = []
    if payload.conditions_restored:
        for item in unresolved_blocking:
            item.status = "resolved"
            item.resolved_by = payload.actor
            item.resolved_at = datetime.now(timezone.utc)
            item.metadata_json = {
                **(item.metadata_json if isinstance(item.metadata_json, dict) else {}),
                "conditions_restored": True,
                "restored_by": payload.actor,
                "restored_reason": payload.reason,
                **payload.metadata_json,
            }
            resolved_ids.append(item.id)

    resolved_replan_signal_ids: list[int] = []
    if payload.conditions_restored:
        for signal in active_replan_signals:
            signal.status = "resolved"
            signal.resolved_by = payload.actor
            signal.resolved_at = datetime.now(timezone.utc)
            signal.metadata_json = {
                **(signal.metadata_json if isinstance(signal.metadata_json, dict) else {}),
                "conditions_restored": True,
                "restored_by": payload.actor,
                "restored_reason": payload.reason,
                **payload.metadata_json,
            }
            resolved_replan_signal_ids.append(signal.id)

    prior_status = execution.status
    execution.status = "running"
    execution.dispatch_decision = "operator_resume_safe"
    execution.reason = payload.reason or "operator_resume_safe"
    _append_execution_interruption(
        execution,
        event="operator_resume",
        actor=payload.actor,
        reason=execution.reason,
        metadata_json={
            "source": payload.source,
            "resolved_interruption_ids": resolved_ids,
            "resolved_replan_signal_ids": resolved_replan_signal_ids,
            "predictive_freshness": predictive_freshness,
            "conditions_restored": payload.conditions_restored,
            **payload.metadata_json,
        },
    )

    if action_plan:
        action_plan.status = "executing"
        action_plan.execution_status = "running"
        action_plan.planning_outcome = "plan_executing"
        action_plan.metadata_json = {
            **(action_plan.metadata_json if isinstance(action_plan.metadata_json, dict) else {}),
            "resume": {
                "resumed_by": payload.actor,
                "resume_reason": payload.reason,
                "conditions_restored": payload.conditions_restored,
                "resolved_interruption_ids": resolved_ids,
                "resolved_replan_signal_ids": resolved_replan_signal_ids,
                "predictive_freshness": predictive_freshness,
                **payload.metadata_json,
            },
        }

    chains = await _find_chains_for_execution(execution_id=execution.id, db=db)
    for chain in chains:
        if chain.status == "paused":
            chain.status = "active"
        _append_chain_audit(
            chain,
            actor=payload.actor,
            event="chain_resumed",
            reason=execution.reason,
            metadata_json={
                "execution_id": execution.id,
                "conditions_restored": payload.conditions_restored,
                "resolved_interruption_ids": resolved_ids,
                "resolved_replan_signal_ids": resolved_replan_signal_ids,
                "predictive_freshness": predictive_freshness,
                **payload.metadata_json,
            },
        )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_execution_resume",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Resumed workspace execution {execution.id}: {prior_status}->running",
        metadata_json={
            "source": payload.source,
            "conditions_restored": payload.conditions_restored,
            "resolved_interruption_ids": resolved_ids,
            "resolved_replan_signal_ids": resolved_replan_signal_ids,
            "predictive_freshness": predictive_freshness,
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(execution)
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "dispatch_decision": execution.dispatch_decision,
        "reason": execution.reason,
        "resolved_interruption_ids": resolved_ids,
        "resolved_replan_signal_ids": resolved_replan_signal_ids,
        "predictive_freshness": predictive_freshness,
        "action_plan_id": action_plan.id if action_plan else None,
        "chain_ids": [item.id for item in chains],
    }
