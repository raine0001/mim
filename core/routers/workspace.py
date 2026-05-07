import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from core.db import SessionLocal, get_db
from core.execution_readiness_service import (
    execution_readiness_policy_effects,
    load_latest_execution_readiness,
)
from core.execution_policy_gate import (
    build_intent_key,
    evaluate_execution_policy_gate,
    sync_execution_control_state,
)
from core.execution_trace_service import infer_managed_scope
from core.journal import write_journal
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
from core.proposal_arbitration_learning_service import (
    list_workspace_proposal_arbitration_learning,
    list_workspace_proposal_arbitration_outcomes,
    record_workspace_proposal_arbitration_outcome,
    to_workspace_proposal_arbitration_out,
    workspace_proposal_arbitration_learning_bias,
)
from core.proposal_policy_convergence_service import (
    converge_workspace_proposal_policy_preference,
    list_workspace_proposal_policy_preferences,
)
from core.policy_conflict_resolution_service import (
    list_workspace_policy_conflict_profiles,
    resolve_workspace_proposal_policy_conflict,
)
from core.autonomy_boundary_service import (
    build_autonomy_decision_context,
    build_boundary_action_controls,
    build_boundary_profile_snapshot,
    evaluate_adaptive_autonomy_boundaries,
    get_autonomy_boundary_profile,
    get_latest_autonomy_boundary_for_scope,
    list_autonomy_boundary_profiles,
    to_autonomy_boundary_profile_out,
)
from core.constraint_service import evaluate_and_record_constraints
from core.concept_memory_service import concept_influence_for_proposal
from core.models import (
    CapabilityExecution,
    CapabilityRegistration,
    InputEvent,
    SpeechOutputAction,
    Task,
    WorkspaceActionPlan,
    WorkspaceAutonomousChain,
    WorkspaceCapabilityChain,
    WorkspaceInterruptionEvent,
    WorkspaceMonitoringState,
    WorkspaceObjectMemory,
    WorkspaceObjectRelation,
    WorkspaceObservation,
    WorkspaceProposal,
    WorkspaceReplanSignal,
    WorkspaceTargetResolution,
    WorkspaceZone,
    WorkspaceZoneRelation,
)
from core.models import WorkspaceReachSimulation
from core.safe_reach_simulation_service import (
    run_simulation as _run_safe_reach_simulation,
    STALE_OBJECT_STATUSES as _STALE_OBJECT_STATUSES,
)
from core.preferences import (
    DEFAULT_USER_ID,
    apply_learning_signal,
    get_user_preference_payload,
    get_user_preference_value,
)
from core.schemas import (
    AdaptiveAutonomyBoundaryEvaluateRequest,
    WorkspaceActionPlanAbortRequest,
    WorkspaceActionPlanCreateRequest,
    WorkspaceActionPlanDecisionRequest,
    WorkspaceActionPlanExecuteRequest,
    WorkspaceActionPlanHandoffRequest,
    WorkspaceActionPlanReplanRequest,
    WorkspaceActionPlanSimulationRequest,
    WorkspaceAutonomousChainAdvanceRequest,
    WorkspaceAutonomousChainApprovalRequest,
    WorkspaceAutonomousChainCreateRequest,
    WorkspaceAutonomyOverrideRequest,
    WorkspaceCapabilityChainAdvanceRequest,
    WorkspaceCapabilityChainCreateRequest,
    WorkspaceExecutionPauseRequest,
    WorkspaceExecutionPredictChangeRequest,
    WorkspaceExecutionProposalActionRequest,
    WorkspaceExecutionProposalCreateRequest,
    WorkspaceExecutionResumeRequest,
    WorkspaceExecutionStopRequest,
    WorkspaceHumanAwareSignalUpdateRequest,
    WorkspaceMonitoringStartRequest,
    WorkspaceMonitoringStopRequest,
    WorkspaceProposalActionRequest,
    WorkspaceProposalArbitrationLearningOut,
    WorkspaceProposalArbitrationOutcomeOut,
    WorkspaceProposalArbitrationOutcomeRecordRequest,
    WorkspacePolicyConflictProfileOut,
    WorkspaceProposalPolicyPreferenceOut,
    WorkspaceProposalPriorityPolicyUpdateRequest,
    WorkspaceTargetConfirmRequest,
    WorkspaceTargetResolveRequest,
)
from core.schemas import WorkspaceTargetSimulateRequest

router = APIRouter()

RECENT_WINDOW_SECONDS = 600
OUTDATED_WINDOW_SECONDS = 3600
OBJECT_STALE_WINDOW_SECONDS = 7200

DEFAULT_ZONE_GRAPH: dict[str, dict[str, list[str] | int]] = {
    "front-left": {
        "adjacent_to": ["front-center", "rear-left"],
        "left_of": ["front-center"],
        "in_front_of": ["rear-left"],
        "hazard_level": 0,
    },
    "front-center": {
        "adjacent_to": ["front-left", "front-right", "rear-center"],
        "left_of": ["front-right"],
        "right_of": ["front-left"],
        "in_front_of": ["rear-center"],
        "hazard_level": 0,
    },
    "front-right": {
        "adjacent_to": ["front-center", "rear-right"],
        "right_of": ["front-center"],
        "in_front_of": ["rear-right"],
        "hazard_level": 0,
    },
    "rear-left": {
        "adjacent_to": ["rear-center", "front-left"],
        "left_of": ["rear-center"],
        "behind": ["front-left"],
        "hazard_level": 0,
    },
    "rear-center": {
        "adjacent_to": ["rear-left", "rear-right", "front-center"],
        "behind": ["front-center"],
        "hazard_level": 0,
    },
    "rear-right": {
        "adjacent_to": ["rear-center", "front-right"],
        "right_of": ["rear-center"],
        "behind": ["front-right"],
        "hazard_level": 0,
    },
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
MONITORING_CONFIDENCE_DELTA_THRESHOLD = 0.02
MONITORING_DELTA_HISTORY_SECONDS = 30
MONITORING_DELTA_HISTORY_MAX_ITEMS = 200
MONITORING_AUTONOMY_STEP_TIMEOUT_SECONDS = 0.25
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
    "auto_execute",
    "auto_safe",
    "auto_preferred",
}
AUTONOMY_POLICY_OUTCOMES = {
    "auto_execute",
    "operator_required",
    "manual_only",
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
CAPABILITY_CHAIN_POLICY_VERSION = "capability-chain-policy-v1"
SAFE_CAPABILITY_CHAIN_COMBINATIONS = {
    ("workspace_scan", "observation_update"),
    ("workspace_scan", "target_resolution"),
    ("target_resolution", "speech_output"),
    ("rescan_zone", "proposal_resolution"),
}
HUMAN_AWARE_POLICY_VERSION = "human-aware-policy-v1"
HUMAN_AWARE_POLICY_OUTCOMES = {
    "continue",
    "slow_suppress",
    "pause",
    "require_operator_confirmation",
    "stop_replan",
}
HUMAN_AWARE_PHYSICAL_PROPOSAL_TYPES = {
    "execution_candidate",
}
HUMAN_AWARE_PHYSICAL_CAPABILITIES = {
    "target_resolution",
    "proposal_resolution",
}
HUMAN_AWARE_SIGNAL_STALE_AFTER_SECONDS = 10
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


def _health_detail_phrase(health_summary: dict) -> str:
    """Return a brief phrase listing which metrics are actively degraded."""
    if not isinstance(health_summary, dict):
        return ""
    _rate_metrics = {"api_error_rate", "cache_hit_rate"}
    _ms_metrics = {"api_latency_ms", "state_bus_lag_ms"}
    parts: list[str] = []
    for name, trend in (health_summary.get("trends") or {}).items():
        if not isinstance(trend, dict) or not trend.get("degradation_detected"):
            continue
        val = trend.get("current_value")
        label = name.replace("_", " ")
        if val is None:
            parts.append(label)
        elif name in _rate_metrics:
            parts.append(f"{label} {float(val):.1%}")
        elif name in _ms_metrics:
            parts.append(f"{label} {float(val):.0f}ms")
        else:
            parts.append(f"{label} {float(val):.0f}%")
    return ", ".join(parts[:2]) if parts else ""


def _physical_execution_health_gate() -> dict[str, object]:
    status = "healthy"
    health_summary: dict = {}
    try:
        health_summary = _mim_health_monitor.get_health_summary() or {}
        if isinstance(health_summary, dict):
            status = str(health_summary.get("status", "healthy")).strip().lower() or "healthy"
    except Exception:
        status = "healthy"

    if status in {"degraded", "critical"}:
        detail = _health_detail_phrase(health_summary)
        detail_suffix = f" ({detail})" if detail else ""
        return {
            "active": True,
            "status": status,
            "requested_decision": "requires_confirmation",
            "requested_status": "pending_confirmation",
            "requested_reason": "system_health_degraded",
            "governance_summary": (
                f"System health is {status}{detail_suffix}; physical execution remains confirmation-gated."
            ),
        }

    return {
        "active": False,
        "status": status,
        "requested_decision": "queued_for_executor",
        "requested_status": "dispatched",
        "requested_reason": "workspace_action_plan_execute",
        "governance_summary": "",
    }


def _default_autonomy_state() -> dict:
    return {
        "auto_execution_enabled": True,
        "force_manual_approval": False,
        "max_auto_actions_per_minute": 6,
        "max_auto_tasks_per_window": 6,
        "auto_window_seconds": 60,
        "cooldown_between_actions_seconds": 5,
        "capability_cooldown_seconds": {},
        "zone_action_limits": {},
        "restricted_zones": [],
        "auto_safe_confidence_threshold": 0.8,
        "auto_preferred_confidence_threshold": 0.7,
        "low_risk_score_max": 0.3,
        "max_autonomy_retries": 1,
        "recent_auto_actions": [],
    }


def _autonomy_state_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = (
        metadata.get("autonomy", {})
        if isinstance(metadata.get("autonomy", {}), dict)
        else {}
    )
    defaults = _default_autonomy_state()
    merged = {
        **defaults,
        **raw,
    }
    merged["zone_action_limits"] = {
        str(key): max(1, int(value))
        for key, value in (
            merged.get("zone_action_limits", {})
            if isinstance(merged.get("zone_action_limits", {}), dict)
            else {}
        ).items()
        if str(key).strip()
    }
    merged["capability_cooldown_seconds"] = {
        str(key).strip(): max(0, int(value))
        for key, value in (
            merged.get("capability_cooldown_seconds", {})
            if isinstance(merged.get("capability_cooldown_seconds", {}), dict)
            else {}
        ).items()
        if str(key).strip()
    }
    merged["restricted_zones"] = [
        _normalize_zone_for_map(str(item))
        for item in (
            merged.get("restricted_zones", [])
            if isinstance(merged.get("restricted_zones", []), list)
            else []
        )
        if _normalize_zone_for_map(str(item))
    ]
    return merged


def _store_autonomy_state(row: WorkspaceMonitoringState, autonomy_state: dict) -> None:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.metadata_json = {
        **metadata,
        "autonomy": autonomy_state,
    }


def _default_human_aware_state() -> dict:
    return {
        "human_in_workspace": False,
        "human_near_target_zone": False,
        "human_near_motion_path": False,
        "shared_workspace_active": False,
        "operator_present": False,
        "occupied_zones": [],
        "high_proximity_zones": [],
        "last_updated_at": "",
        "last_updated_by": "",
        "last_reason": "",
        "last_policy_decision": {
            "outcome": "continue",
            "reason": "default_clear",
            "at": "",
        },
    }


def _human_aware_state_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = (
        metadata.get("human_aware", {})
        if isinstance(metadata.get("human_aware", {}), dict)
        else {}
    )
    defaults = _default_human_aware_state()
    merged = {
        **defaults,
        **raw,
    }
    for key in [
        "human_in_workspace",
        "human_near_target_zone",
        "human_near_motion_path",
        "shared_workspace_active",
        "operator_present",
    ]:
        merged[key] = bool(merged.get(key, False))
    merged["occupied_zones"] = [
        normalized
        for normalized in (
            _normalize_zone_for_map(str(item))
            for item in (
                merged.get("occupied_zones", [])
                if isinstance(merged.get("occupied_zones", []), list)
                else []
            )
        )
        if normalized
    ]
    merged["high_proximity_zones"] = [
        normalized
        for normalized in (
            _normalize_zone_for_map(str(item))
            for item in (
                merged.get("high_proximity_zones", [])
                if isinstance(merged.get("high_proximity_zones", []), list)
                else []
            )
        )
        if normalized
    ]
    last_updated_at = str(merged.get("last_updated_at", "")).strip()
    updated_at = _parse_iso_to_utc(last_updated_at) if last_updated_at else None
    is_stale = True
    if updated_at is not None:
        age_seconds = max(
            (datetime.now(timezone.utc) - updated_at).total_seconds(), 0.0
        )
        is_stale = age_seconds > HUMAN_AWARE_SIGNAL_STALE_AFTER_SECONDS
    if is_stale:
        merged["human_in_workspace"] = False
        merged["human_near_target_zone"] = False
        merged["human_near_motion_path"] = False
        merged["shared_workspace_active"] = False
        merged["operator_present"] = False
        merged["occupied_zones"] = []
        merged["high_proximity_zones"] = []
    return merged


def _store_human_aware_state(
    row: WorkspaceMonitoringState, human_aware_state: dict
) -> None:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.metadata_json = {
        **metadata,
        "human_aware": human_aware_state,
    }


def _is_physical_capability_step(capability: str) -> bool:
    return capability in HUMAN_AWARE_PHYSICAL_CAPABILITIES


def _human_aware_policy_for_capability_step(
    *, step: dict, human_aware: dict
) -> tuple[str, str]:
    capability = str(step.get("capability", "")).strip()
    params = step.get("params", {}) if isinstance(step.get("params", {}), dict) else {}
    normalized_zone = _normalize_zone_for_map(str(params.get("zone", "")))

    if bool(human_aware.get("shared_workspace_active", False)) and bool(
        human_aware.get("human_in_workspace", False)
    ):
        return "pause", "shared_workspace_active"

    if bool(
        human_aware.get("human_near_motion_path", False)
    ) and _is_physical_capability_step(capability):
        return "stop_replan", "human_near_motion_path"

    occupied = set(
        human_aware.get("occupied_zones", [])
        if isinstance(human_aware.get("occupied_zones", []), list)
        else []
    )
    if (
        normalized_zone
        and normalized_zone in occupied
        and _is_physical_capability_step(capability)
    ):
        return "pause", "occupied_zone_no_autonomous_movement"

    high_proximity = set(
        human_aware.get("high_proximity_zones", [])
        if isinstance(human_aware.get("high_proximity_zones", []), list)
        else []
    )
    if (
        normalized_zone
        and normalized_zone in high_proximity
        and _is_physical_capability_step(capability)
    ):
        return "require_operator_confirmation", "high_proximity_zone"

    if bool(
        human_aware.get("human_near_target_zone", False)
    ) and _is_physical_capability_step(capability):
        return "require_operator_confirmation", "human_near_target_zone"

    if bool(
        human_aware.get("human_in_workspace", False)
    ) and _is_physical_capability_step(capability):
        return "require_operator_confirmation", "human_in_workspace_physical_action"

    if (
        bool(human_aware.get("operator_present", False))
        and capability == "speech_output"
    ):
        return "slow_suppress", "operator_present_speech_suppressed"

    return "continue", "clear"


def _human_aware_policy_for_proposal(
    *, proposal: WorkspaceProposal, human_aware: dict
) -> tuple[str, str]:
    proposal_type = str(proposal.proposal_type or "").strip()
    normalized_zone = _normalize_zone_for_map(str(proposal.related_zone or ""))
    is_physical = proposal_type in HUMAN_AWARE_PHYSICAL_PROPOSAL_TYPES

    if bool(human_aware.get("shared_workspace_active", False)) and bool(
        human_aware.get("human_in_workspace", False)
    ):
        return "pause", "shared_workspace_active"

    if bool(human_aware.get("human_near_motion_path", False)) and is_physical:
        return "stop_replan", "human_near_motion_path"

    occupied = set(
        human_aware.get("occupied_zones", [])
        if isinstance(human_aware.get("occupied_zones", []), list)
        else []
    )
    if normalized_zone and normalized_zone in occupied and is_physical:
        return "pause", "occupied_zone_no_autonomous_movement"

    high_proximity = set(
        human_aware.get("high_proximity_zones", [])
        if isinstance(human_aware.get("high_proximity_zones", []), list)
        else []
    )
    if normalized_zone and normalized_zone in high_proximity and is_physical:
        return "require_operator_confirmation", "high_proximity_zone"

    if bool(human_aware.get("human_near_target_zone", False)) and is_physical:
        return "require_operator_confirmation", "human_near_target_zone"

    if bool(human_aware.get("human_in_workspace", False)) and is_physical:
        return "require_operator_confirmation", "human_in_workspace_physical_action"

    return "continue", "clear"


def _human_aware_inspectability_payload(*, row: WorkspaceMonitoringState) -> dict:
    state = _human_aware_state_from_monitoring(row)
    return {
        "policy_version": HUMAN_AWARE_POLICY_VERSION,
        "signals": {
            "human_in_workspace": bool(state.get("human_in_workspace", False)),
            "human_near_target_zone": bool(state.get("human_near_target_zone", False)),
            "human_near_motion_path": bool(state.get("human_near_motion_path", False)),
            "shared_workspace_active": bool(
                state.get("shared_workspace_active", False)
            ),
            "operator_present": bool(state.get("operator_present", False)),
            "occupied_zones": state.get("occupied_zones", []),
            "high_proximity_zones": state.get("high_proximity_zones", []),
        },
        "policy_outcomes": sorted(list(HUMAN_AWARE_POLICY_OUTCOMES)),
        "last_policy_decision": state.get("last_policy_decision", {}),
        "last_updated_at": state.get("last_updated_at", ""),
        "last_updated_by": state.get("last_updated_by", ""),
        "last_reason": state.get("last_reason", ""),
    }


def _normalize_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _proposal_priority_policy_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = (
        metadata.get("proposal_priority_policy", {})
        if isinstance(metadata.get("proposal_priority_policy", {}), dict)
        else {}
    )
    defaults = PROPOSAL_PRIORITY_DEFAULT

    policy = {
        "weights": {
            key: _normalize_score(value)
            for key, value in {
                **defaults.get("weights", {}),
                **(
                    raw.get("weights", {})
                    if isinstance(raw.get("weights", {}), dict)
                    else {}
                ),
            }.items()
        },
        "urgency_map": {
            str(key): _normalize_score(value)
            for key, value in {
                **defaults.get("urgency_map", {}),
                **(
                    raw.get("urgency_map", {})
                    if isinstance(raw.get("urgency_map", {}), dict)
                    else {}
                ),
            }.items()
            if str(key).strip()
        },
        "zone_importance": {
            str(key): _normalize_score(value)
            for key, value in {
                **defaults.get("zone_importance", {}),
                **(
                    raw.get("zone_importance", {})
                    if isinstance(raw.get("zone_importance", {}), dict)
                    else {}
                ),
            }.items()
            if str(key).strip()
        },
        "operator_preference": {
            str(key): _normalize_score(value)
            for key, value in {
                **(
                    defaults.get("operator_preference", {})
                    if isinstance(defaults.get("operator_preference", {}), dict)
                    else {}
                ),
                **(
                    raw.get("operator_preference", {})
                    if isinstance(raw.get("operator_preference", {}), dict)
                    else {}
                ),
            }.items()
            if str(key).strip()
        },
        "age_saturation_minutes": max(
            1,
            int(
                raw.get(
                    "age_saturation_minutes",
                    defaults.get("age_saturation_minutes", 120),
                )
            ),
        ),
        "version": PROPOSAL_PRIORITY_POLICY_VERSION,
    }
    return policy


def _compute_workspace_proposal_priority(
    *, proposal: WorkspaceProposal, policy: dict, now: datetime
) -> tuple[float, str, dict]:
    urgency_map = (
        policy.get("urgency_map", {})
        if isinstance(policy.get("urgency_map", {}), dict)
        else {}
    )
    zone_importance_map = (
        policy.get("zone_importance", {})
        if isinstance(policy.get("zone_importance", {}), dict)
        else {}
    )
    operator_preference_map = (
        policy.get("operator_preference", {})
        if isinstance(policy.get("operator_preference", {}), dict)
        else {}
    )
    weights = (
        policy.get("weights", {}) if isinstance(policy.get("weights", {}), dict) else {}
    )

    normalized_zone = _normalize_zone_for_map(proposal.related_zone)
    urgency = _normalize_score(float(urgency_map.get(proposal.proposal_type, 0.5)))
    confidence = _normalize_score(float(proposal.confidence))
    safety = _normalize_score(1.0 - _autonomy_risk_score(proposal.proposal_type))
    operator_preference = _normalize_score(
        float(operator_preference_map.get(proposal.proposal_type, 0.5))
    )
    zone_importance = _normalize_score(
        float(zone_importance_map.get(normalized_zone, 0.5))
    )

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
    return (
        round(_normalize_score(score), 4),
        reason,
        {
            "components": components,
            "weighted": contribution,
            "top_signals": [name for name, _ in top],
        },
    )


async def _refresh_workspace_proposal_priority(
    *, proposal: WorkspaceProposal, db: AsyncSession
) -> dict:
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
                zone_importance[normalized] = max(
                    0.95, float(zone_importance.get(normalized, 0.5))
                )
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
    score, reason, breakdown = _compute_workspace_proposal_priority(
        proposal=proposal, policy=policy, now=now
    )
    concept_influence = await concept_influence_for_proposal(
        related_zone=proposal.related_zone,
        proposal_type=proposal.proposal_type,
        db=db,
    )
    if bool(concept_influence.get("applied", False)):
        breakdown = {
            **(breakdown if isinstance(breakdown, dict) else {}),
            "concept_influence": concept_influence,
        }
        reason = f"{reason}, concept_context" if reason else "concept_context"

    proposal_arbitration_learning = await workspace_proposal_arbitration_learning_bias(
        proposal_type=proposal.proposal_type,
        related_zone=proposal.related_zone,
        db=db,
    )
    arbitration_bias = float(
        proposal_arbitration_learning.get("priority_bias", 0.0) or 0.0
    )
    if abs(arbitration_bias) >= 1e-9:
        score = round(_normalize_score(score + arbitration_bias), 4)
    breakdown = {
        **(breakdown if isinstance(breakdown, dict) else {}),
        "proposal_arbitration_learning": proposal_arbitration_learning,
    }
    if bool(proposal_arbitration_learning.get("applied", False)):
        reason = f"{reason}, arbitration_learning" if reason else "arbitration_learning"

    execution_readiness = load_latest_execution_readiness(
        action=proposal.proposal_type,
        capability_name=proposal.proposal_type,
        managed_scope=proposal.related_zone,
        requested_executor="tod",
        metadata_json={
            "proposal_id": int(proposal.id),
            "proposal_type": proposal.proposal_type,
            "managed_scope": proposal.related_zone,
        },
    )
    readiness_effects = execution_readiness_policy_effects(
        readiness=execution_readiness,
        surface="proposal",
    )
    readiness_delta = float(readiness_effects.get("priority_delta", 0.0) or 0.0)
    if abs(readiness_delta) >= 1e-9:
        score = round(_normalize_score(score + readiness_delta), 4)
    readiness_cap = readiness_effects.get("score_cap")
    if isinstance(readiness_cap, (int, float)):
        score = round(min(score, _normalize_score(float(readiness_cap))), 4)
    breakdown = {
        **(breakdown if isinstance(breakdown, dict) else {}),
        "execution_readiness": {
            "readiness": execution_readiness,
            "policy_effects_json": readiness_effects,
        },
    }
    if readiness_delta < 0.0 or isinstance(readiness_cap, (int, float)):
        reason = f"{reason}, execution_readiness" if reason else "execution_readiness"

    proposal_policy_convergence = await converge_workspace_proposal_policy_preference(
        proposal_type=proposal.proposal_type,
        related_zone=proposal.related_zone,
        db=db,
    )
    policy_effects = (
        proposal_policy_convergence.get("policy_effects_json", {})
        if isinstance(proposal_policy_convergence.get("policy_effects_json", {}), dict)
        else {}
    )
    policy_delta = float(policy_effects.get("priority_delta", 0.0) or 0.0)
    if abs(policy_delta) >= 1e-9:
        score = round(_normalize_score(score + policy_delta), 4)
    score_cap = policy_effects.get("score_cap")
    if isinstance(score_cap, (int, float)):
        score = round(min(score, _normalize_score(float(score_cap))), 4)
    breakdown = {
        **(breakdown if isinstance(breakdown, dict) else {}),
        "proposal_policy_convergence": proposal_policy_convergence,
    }
    if bool(proposal_policy_convergence.get("applied", False)):
        reason = (
            f"{reason}, proposal_policy_convergence"
            if reason
            else "proposal_policy_convergence"
        )

    policy_conflict_resolution = await resolve_workspace_proposal_policy_conflict(
        proposal=proposal,
        proposal_type=proposal.proposal_type,
        related_zone=proposal.related_zone,
        proposal_policy_convergence=proposal_policy_convergence,
        db=db,
    )
    conflict_effects = (
        policy_conflict_resolution.get("policy_effects_json", {})
        if isinstance(policy_conflict_resolution.get("policy_effects_json", {}), dict)
        else {}
    )
    conflict_delta = float(conflict_effects.get("priority_delta", 0.0) or 0.0)
    if abs(conflict_delta) >= 1e-9:
        score = round(_normalize_score(score + conflict_delta), 4)
    conflict_cap = conflict_effects.get("score_cap")
    if isinstance(conflict_cap, (int, float)):
        score = round(min(score, _normalize_score(float(conflict_cap))), 4)
    breakdown = {
        **(breakdown if isinstance(breakdown, dict) else {}),
        "policy_conflict_resolution": policy_conflict_resolution,
    }
    if str(policy_conflict_resolution.get("conflict_state") or "").strip() in {
        "active_conflict",
        "cooldown_held",
    }:
        reason = (
            f"{reason}, policy_conflict_resolution"
            if reason
            else "policy_conflict_resolution"
        )

    proposal.priority_score = score
    proposal.priority_reason = reason
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "priority_policy_version": policy.get(
            "version", PROPOSAL_PRIORITY_POLICY_VERSION
        ),
        "priority_breakdown": breakdown,
        "preference_context": {
            "preferred_scan_zones": preferred_scan_zones
            if isinstance(preferred_scan_zones, list)
            else [],
            "auto_exec_tolerance": auto_exec_tolerance,
        },
        "execution_readiness": execution_readiness,
        "proposal_arbitration_learning": proposal_arbitration_learning,
        "proposal_policy_convergence": proposal_policy_convergence,
        "policy_conflict_resolution": policy_conflict_resolution,
    }
    return {
        "policy": policy,
        "score": score,
        "reason": reason,
        "breakdown": breakdown,
    }


def _workspace_proposal_payload(row: WorkspaceProposal) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
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
        "metadata_json": metadata,
        "arbitration_learning": (
            metadata.get("proposal_arbitration_learning", {})
            if isinstance(metadata.get("proposal_arbitration_learning", {}), dict)
            else {}
        ),
        "proposal_policy_convergence": (
            metadata.get("proposal_policy_convergence", {})
            if isinstance(metadata.get("proposal_policy_convergence", {}), dict)
            else {}
        ),
        "execution_readiness": (
            metadata.get("execution_readiness", {})
            if isinstance(metadata.get("execution_readiness", {}), dict)
            else {}
        ),
        "policy_conflict_resolution": (
            metadata.get("policy_conflict_resolution", {})
            if isinstance(metadata.get("policy_conflict_resolution", {}), dict)
            else {}
        ),
        "created_at": row.created_at,
    }


async def _notification_payload_for_proposal(
    *, db: AsyncSession, proposal: WorkspaceProposal, action: str
) -> dict:
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
    zone = (
        (
            await db.execute(
                select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped)
            )
        )
        .scalars()
        .first()
    )
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

    # Prefer the explicit simulation_gate_passed boolean written by the
    # safe-reach simulation endpoint over legacy outcome string checks.
    if "simulation_gate_passed" in trigger:
        gate = bool(trigger["simulation_gate_passed"])
        stored_outcome = str(trigger.get("simulation_outcome", "plan_safe" if gate else "plan_blocked"))
        return gate, stored_outcome

    # Legacy: outcome string in trigger root or preconditions sub-dict
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


def _autonomy_throttle_check(
    *, autonomy_state: dict, zone: str, capability_name: str, now: datetime
) -> tuple[bool, str, list[dict]]:
    recent_raw = autonomy_state.get("recent_auto_actions", [])
    recent_actions: list[dict] = []
    window_seconds = max(10, int(autonomy_state.get("auto_window_seconds", 60) or 60))
    if isinstance(recent_raw, list):
        for item in recent_raw:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", "")).strip()
            parsed = _parse_iso_to_utc(ts) if ts else None
            if not parsed:
                continue
            if (now - parsed).total_seconds() <= window_seconds:
                recent_actions.append({**item, "timestamp": parsed.isoformat()})

    max_per_min = max(
        1,
        int(
            autonomy_state.get(
                "max_auto_tasks_per_window",
                autonomy_state.get("max_auto_actions_per_minute", 6),
            )
            or 6
        ),
    )
    if len(recent_actions) >= max_per_min:
        return False, "max_auto_tasks_per_window", recent_actions

    cooldown = max(0, int(autonomy_state.get("cooldown_between_actions_seconds", 0)))
    if recent_actions:
        latest = max(
            (
                _parse_iso_to_utc(str(item.get("timestamp", "")))
                for item in recent_actions
            ),
            default=None,
        )
        if latest and (now - latest).total_seconds() < cooldown:
            return False, "cooldown_between_actions", recent_actions

    zone_limits = (
        autonomy_state.get("zone_action_limits", {})
        if isinstance(autonomy_state.get("zone_action_limits", {}), dict)
        else {}
    )
    if zone.strip() and zone in zone_limits:
        zone_count = sum(
            1 for item in recent_actions if str(item.get("zone", "")).strip() == zone
        )
        if zone_count >= max(1, int(zone_limits[zone])):
            return False, "zone_based_limit", recent_actions

    capability_cooldowns = (
        autonomy_state.get("capability_cooldown_seconds", {})
        if isinstance(autonomy_state.get("capability_cooldown_seconds", {}), dict)
        else {}
    )
    cooldown_for_capability = max(
        0, int(capability_cooldowns.get(capability_name, 0) or 0)
    )
    if cooldown_for_capability > 0:
        last_for_capability: datetime | None = None
        for item in recent_actions:
            if str(item.get("capability_name", "")).strip() != capability_name:
                continue
            parsed = _parse_iso_to_utc(str(item.get("timestamp", "")))
            if not parsed:
                continue
            if not last_for_capability or parsed > last_for_capability:
                last_for_capability = parsed
        if (
            last_for_capability
            and (now - last_for_capability).total_seconds() < cooldown_for_capability
        ):
            return False, "capability_cooldown", recent_actions

    return True, "allowed", recent_actions


def _autonomy_policy_outcome_for_tier(tier: str) -> str:
    if tier in {"manual_only", "operator_required"}:
        return tier
    return "auto_execute"


async def _autonomy_has_active_interruption(*, db: AsyncSession) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    stale_after_seconds = 45
    active = (
        (
            await db.execute(
                select(WorkspaceInterruptionEvent)
                .where(WorkspaceInterruptionEvent.status == "active")
                .order_by(WorkspaceInterruptionEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for item in active:
        if item.execution_id:
            execution = await db.get(CapabilityExecution, item.execution_id)
            if execution and execution.status in {"succeeded", "failed", "blocked"}:
                continue
        age_seconds = max((now - item.created_at).total_seconds(), 0.0)
        if age_seconds > stale_after_seconds:
            continue
        interruption_type = str(item.interruption_type or "").strip()
        if interruption_type in INTERRUPTION_BLOCKING_TYPES:
            return True, f"interruption:{interruption_type}"
        if str(item.applied_outcome or "").strip() in {
            "paused",
            "stopped",
            "require_operator_decision",
            "auto_pause",
            "auto_stop",
        }:
            return True, f"interruption_outcome:{item.applied_outcome}"
    return False, "clear"


def _autonomy_capability_for_proposal(proposal: WorkspaceProposal) -> str:
    if proposal.proposal_type in {
        "rescan_zone",
        "confirm_target_ready",
        MONITORING_RECHECK_PROPOSAL_TYPE,
        MONITORING_SEARCH_PROPOSAL_TYPE,
    }:
        return "workspace_scan"
    return ""


def _is_safe_capability_registration(row: CapabilityRegistration | None) -> bool:
    if row is None:
        return False
    if not row.enabled:
        return False
    if row.requires_confirmation:
        return False
    policy = row.safety_policy if isinstance(row.safety_policy, dict) else {}
    scope = str(policy.get("scope", "")).strip().lower()
    mode = str(policy.get("mode", "")).strip().lower()
    return scope in {"non-actuating", "read-only", "observe-only"} or mode in {
        "scan-only",
        "observe-only",
        "non-actuating",
    }


async def _autonomy_dispatch_scan_execution(
    *,
    proposal: WorkspaceProposal,
    trigger_reason: str,
    policy_rule_used: str,
    capability_name: str,
    db: AsyncSession,
) -> tuple[int, int]:
    task = Task(
        title=proposal.title,
        details=proposal.description,
        dependencies=[],
        acceptance_criteria="autonomous proposal dispatched for bounded execution",
        assigned_to="tod",
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    event = InputEvent(
        source="api",
        raw_input=f"autonomy dispatch proposal {proposal.id}",
        parsed_intent="observe_workspace",
        confidence=float(proposal.confidence),
        target_system="tod",
        requested_goal=f"autonomy:proposal:{proposal.id}",
        safety_flags=["autonomous", "bounded", "policy_safe"],
        metadata_json={
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "trigger_reason": trigger_reason,
            "policy_rule_used": policy_rule_used,
            "scan_area": proposal.related_zone or "workspace",
            "task_id": task.id,
        },
        normalized=True,
    )
    db.add(event)
    await db.flush()

    managed_scope = infer_managed_scope(
        proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {},
        {"related_zone": proposal.related_zone},
        proposal.related_zone,
    )
    gate_result = await evaluate_execution_policy_gate(
        db=db,
        capability_name=capability_name,
        requested_decision="auto_dispatch",
        requested_status="dispatched",
        requested_reason="objective41_autonomous_dispatch",
        requested_executor="tod",
        safety_mode="autonomous_bounded",
        managed_scope=managed_scope,
        actor="workspace",
        source="workspace_autonomy_dispatch",
        metadata_json=proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {},
    )

    execution = CapabilityExecution(
        input_event_id=event.id,
        resolution_id=None,
        goal_id=None,
        capability_name=capability_name,
        arguments_json={
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "scan_area": proposal.related_zone or "workspace",
            "target_zone": proposal.related_zone or "workspace",
            "trigger_json": proposal.trigger_json
            if isinstance(proposal.trigger_json, dict)
            else {},
        },
        safety_mode="autonomous_bounded",
        requested_executor=gate_result["requested_executor"],
        dispatch_decision=gate_result["dispatch_decision"],
        managed_scope=gate_result["managed_scope"],
        status=gate_result["status"],
        reason=gate_result["reason"],
        feedback_json={
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "autonomy": {
                "trigger_reason": trigger_reason,
                "policy_rule_used": policy_rule_used,
            },
            "task_id": task.id,
            "execution_policy_gate": gate_result,
        },
    )
    db.add(execution)
    await db.flush()

    await sync_execution_control_state(
        db=db,
        execution=execution,
        actor="workspace",
        source="workspace_autonomy_dispatch",
        requested_goal=f"autonomy:proposal:{proposal.id}",
        intent_key=build_intent_key(
            execution_source="workspace_proposal",
            subject_id=proposal.id,
            capability_name=capability_name,
        ),
        intent_type="workspace_proposal_execution",
        context_json={
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "related_zone": proposal.related_zone,
        },
        gate_result=gate_result,
    )

    proposal.source_execution_id = execution.id
    return task.id, execution.id


async def _evaluate_autonomous_execution_result(
    *,
    proposal: WorkspaceProposal,
    db: AsyncSession,
) -> dict:
    metadata = (
        proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}
    )
    execution_id_raw = metadata.get("active_execution_id")
    execution_id = (
        int(execution_id_raw)
        if str(execution_id_raw).isdigit()
        else int(proposal.source_execution_id or 0)
    )
    if execution_id <= 0:
        return {
            "updated": False,
            "result": "awaiting_execution",
            "execution_id": None,
            "memory_delta": {},
        }

    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        return {
            "updated": False,
            "result": "execution_missing",
            "execution_id": execution_id,
            "memory_delta": {},
        }

    status = str(execution.status or "").strip()
    feedback = (
        execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    )
    memory_delta = {
        "workspace_observation_ids": feedback.get("workspace_observation_ids", [])
        if isinstance(feedback.get("workspace_observation_ids", []), list)
        else [],
        "workspace_object_ids": feedback.get("workspace_object_ids", [])
        if isinstance(feedback.get("workspace_object_ids", []), list)
        else [],
        "workspace_proposal_ids": feedback.get("workspace_proposal_ids", [])
        if isinstance(feedback.get("workspace_proposal_ids", []), list)
        else [],
        "observation_count": len(feedback.get("observations", []))
        if isinstance(feedback.get("observations", []), list)
        else 0,
    }
    has_memory_delta = (
        bool(memory_delta["workspace_observation_ids"])
        or bool(memory_delta["workspace_object_ids"])
        or int(memory_delta["observation_count"]) > 0
    )

    if status in {"dispatched", "accepted", "running", "paused"}:
        return {
            "updated": False,
            "result": "in_progress",
            "execution_id": execution.id,
            "memory_delta": memory_delta,
            "execution_status": status,
        }

    if status == "succeeded" and has_memory_delta:
        proposal.status = "resolved"
        proposal.metadata_json = {
            **metadata,
            "verification": {
                "result": "success",
                "execution_status": status,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            },
            "memory_delta": memory_delta,
        }
        return {
            "updated": True,
            "result": "success",
            "execution_id": execution.id,
            "memory_delta": memory_delta,
            "execution_status": status,
        }

    retries = int(metadata.get("autonomy_retry_count", 0) or 0)
    monitoring = await _get_or_create_monitoring_state(db)
    autonomy_state = _autonomy_state_from_monitoring(monitoring)
    max_retries = max(0, int(autonomy_state.get("max_autonomy_retries", 1) or 1))

    if retries < max_retries:
        capability_name = _autonomy_capability_for_proposal(proposal)
        if capability_name:
            task_id, retry_execution_id = await _autonomy_dispatch_scan_execution(
                proposal=proposal,
                trigger_reason="objective41_retry",
                policy_rule_used="auto_execute_retry",
                capability_name=capability_name,
                db=db,
            )
            proposal.status = "accepted"
            proposal.metadata_json = {
                **metadata,
                "autonomy_retry_count": retries + 1,
                "active_execution_id": retry_execution_id,
                "linked_task_id": task_id,
                "verification": {
                    "result": "retry",
                    "execution_status": status,
                    "retry_execution_id": retry_execution_id,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                },
                "memory_delta": memory_delta,
            }
            return {
                "updated": True,
                "result": "retry",
                "execution_id": retry_execution_id,
                "memory_delta": memory_delta,
                "execution_status": status,
            }

    proposal.status = "pending"
    proposal.metadata_json = {
        **metadata,
        "verification": {
            "result": "escalate_to_operator",
            "execution_status": status,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        },
        "escalation_required": True,
        "memory_delta": memory_delta,
    }
    return {
        "updated": True,
        "result": "escalate_to_operator",
        "execution_id": execution.id,
        "memory_delta": memory_delta,
        "execution_status": status,
    }


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
    policy_outcome = _autonomy_policy_outcome_for_tier(tier)
    if policy_outcome not in AUTONOMY_POLICY_OUTCOMES:
        return False, "invalid_policy_outcome"
    if policy_outcome != "auto_execute":
        return False, f"policy_{policy_outcome}"

    interruption_blocked, interruption_reason = await _autonomy_has_active_interruption(
        db=db
    )
    if interruption_blocked:
        return False, interruption_reason

    human_aware = _human_aware_state_from_monitoring(monitoring)
    _, constraint_result = await evaluate_and_record_constraints(
        actor="workspace-autonomy-controller",
        source="objective44_autonomy_auto_execute",
        goal={
            "goal_type": "proposal_auto_execute",
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
        },
        action_plan={
            "action_type": "auto_execute_proposal",
            "proposal_type": proposal.proposal_type,
            "is_physical": proposal.proposal_type
            in HUMAN_AWARE_PHYSICAL_PROPOSAL_TYPES,
        },
        workspace_state={
            "human_in_workspace": human_aware.get("human_in_workspace", False),
            "human_near_target_zone": human_aware.get("human_near_target_zone", False),
            "human_near_motion_path": human_aware.get("human_near_motion_path", False),
            "shared_workspace_active": human_aware.get(
                "shared_workspace_active", False
            ),
            "target_confidence": float(proposal.confidence),
            "map_freshness_seconds": 0,
        },
        system_state={"throttle_blocked": False, "integrity_risk": False},
        policy_state={
            "min_target_confidence": float(
                autonomy_state.get("auto_safe_confidence_threshold", 0.8)
            ),
            "map_freshness_limit_seconds": MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
            "unlawful_action": False,
        },
        metadata_json={"trigger_reason": trigger_reason},
        db=db,
    )
    if constraint_result.get("decision") in {
        "requires_confirmation",
        "requires_replan",
        "blocked",
    }:
        return False, f"constraint_{constraint_result.get('decision')}:objective44"

    human_outcome, human_reason = _human_aware_policy_for_proposal(
        proposal=proposal, human_aware=human_aware
    )
    if human_outcome in {"pause", "require_operator_confirmation", "stop_replan"}:
        human_aware["last_policy_decision"] = {
            "outcome": human_outcome,
            "reason": human_reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _store_human_aware_state(monitoring, human_aware)
        return False, f"human_policy_{human_outcome}:{human_reason}"

    threshold = _autonomy_confidence_threshold(tier=tier, autonomy_state=autonomy_state)
    if float(proposal.confidence) < threshold:
        return False, "confidence_below_threshold"

    normalized_zone = _normalize_zone_for_map(proposal.related_zone)
    restricted_zones = (
        set(autonomy_state.get("restricted_zones", []))
        if isinstance(autonomy_state.get("restricted_zones", []), list)
        else set()
    )
    if normalized_zone and normalized_zone in restricted_zones:
        return False, "restricted_zone"

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
    capability_name = _autonomy_capability_for_proposal(proposal)
    if not capability_name:
        return False, "no_safe_capability_mapping"
    capability = (
        (
            await db.execute(
                select(CapabilityRegistration).where(
                    CapabilityRegistration.capability_name == capability_name
                )
            )
        )
        .scalars()
        .first()
    )
    if not _is_safe_capability_registration(capability):
        return False, "capability_not_safe"

    throttle_allowed, throttle_reason, recent_actions = _autonomy_throttle_check(
        autonomy_state=autonomy_state,
        zone=proposal.related_zone,
        capability_name=capability_name,
        now=now,
    )
    if not throttle_allowed:
        return False, throttle_reason

    task_id, execution_id = await _autonomy_dispatch_scan_execution(
        proposal=proposal,
        trigger_reason=trigger_reason,
        policy_rule_used=policy_outcome,
        capability_name=capability_name,
        db=db,
    )
    proposal.status = "accepted"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": "system-auto",
        "accept_reason": "objective41_auto_execute",
        "linked_task_id": task_id,
        "active_execution_id": execution_id,
        "auto_execution": True,
        "trigger_reason": trigger_reason,
        "policy_rule_used": policy_outcome,
        "confidence_score": float(proposal.confidence),
        "risk_score": risk_score,
        "simulation_result": simulation_result,
        "execution_outcome": "dispatched",
    }
    proposal.source_execution_id = execution_id

    recent_actions.append(
        {
            "timestamp": now.isoformat(),
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "zone": proposal.related_zone,
            "task_id": task_id,
            "execution_id": execution_id,
            "capability_name": capability_name,
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
            "policy_rule_used": policy_outcome,
            "autonomy_decision": "auto_execute",
            "confidence_score": float(proposal.confidence),
            "risk_score": risk_score,
            "simulation_result": simulation_result,
            "execution_outcome": "dispatched",
            "linked_task_id": task_id,
            "execution_id": execution_id,
            "proposal_id": proposal.id,
            "result": "dispatched",
            "memory_delta": {},
        },
    )
    return True, "auto_executed"


async def _run_autonomy_controller_step(
    *, db: AsyncSession, actor: str, reason: str, zone_filter: str = ""
) -> dict:
    verification_updates: list[dict] = []
    accepted_auto_rows = (
        (
            await db.execute(
                select(WorkspaceProposal)
                .where(WorkspaceProposal.status == "accepted")
                .order_by(WorkspaceProposal.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for row in accepted_auto_rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if not bool(metadata.get("auto_execution", False)):
            continue
        verification = await _evaluate_autonomous_execution_result(proposal=row, db=db)
        if verification.get("updated"):
            verification_updates.append(
                {
                    "proposal_id": row.id,
                    "result": verification.get("result"),
                    "execution_id": verification.get("execution_id"),
                    "memory_delta": verification.get("memory_delta", {}),
                    "execution_status": verification.get("execution_status", ""),
                }
            )
            await write_journal(
                db,
                actor="workspace-autonomy-controller",
                action="workspace_autonomy_result_verification",
                target_type="workspace_proposal",
                target_id=str(row.id),
                summary=f"Verified autonomous proposal {row.id}: {verification.get('result', 'unknown')}",
                metadata_json={
                    "trigger_reason": reason,
                    "policy_rule_used": metadata.get(
                        "policy_rule_used", "auto_execute"
                    ),
                    "proposal_id": row.id,
                    "execution_id": verification.get("execution_id"),
                    "result": verification.get("result", "unknown"),
                    "memory_delta": verification.get("memory_delta", {}),
                },
            )

    blocked, interruption_reason = await _autonomy_has_active_interruption(db=db)
    if blocked:
        await write_journal(
            db,
            actor=actor,
            action="workspace_autonomy_controller_paused",
            target_type="workspace_monitoring",
            target_id="1",
            summary="Autonomy controller paused due to interruption",
            metadata_json={
                "trigger_reason": reason,
                "policy_rule_used": "interruption_guard",
                "proposal_id": None,
                "execution_id": None,
                "result": "paused",
                "memory_delta": {},
                "interruption_reason": interruption_reason,
            },
        )
        return {
            "executed": False,
            "result": "paused_by_interruption",
            "reason": interruption_reason,
            "verification_updates": verification_updates,
            "proposal": None,
        }

    pending_rows = (
        (
            await db.execute(
                select(WorkspaceProposal)
                .where(WorkspaceProposal.status == "pending")
                .order_by(
                    WorkspaceProposal.created_at.asc(), WorkspaceProposal.id.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    if zone_filter.strip():
        token = zone_filter.strip().lower()
        pending_rows = [
            item
            for item in pending_rows
            if token in str(item.related_zone or "").strip().lower()
        ]
    if not pending_rows:
        return {
            "executed": False,
            "result": "no_pending_proposals",
            "verification_updates": verification_updates,
            "proposal": None,
        }

    scored: list[WorkspaceProposal] = []
    for row in pending_rows:
        await _refresh_workspace_proposal_priority(proposal=row, db=db)
        scored.append(row)

    scored.sort(
        key=lambda item: (
            float(item.priority_score),
            float(item.confidence),
            item.created_at,
            item.id,
        ),
        reverse=True,
    )
    selected = scored[0]
    for candidate in scored:
        tier = _autonomy_policy_tier(candidate.proposal_type)
        if _autonomy_policy_outcome_for_tier(tier) == "auto_execute":
            selected = candidate
            break
    executed, execute_reason = await _maybe_auto_execute_workspace_proposal(
        proposal=selected,
        trigger_reason=reason,
        db=db,
    )

    if executed:
        selected_meta = (
            selected.metadata_json if isinstance(selected.metadata_json, dict) else {}
        )
        await write_journal(
            db,
            actor=actor,
            action="workspace_autonomy_controller_execute",
            target_type="workspace_proposal",
            target_id=str(selected.id),
            summary=f"Autonomy controller executed proposal {selected.id}",
            metadata_json={
                "trigger_reason": reason,
                "policy_rule_used": selected_meta.get(
                    "policy_rule_used", "auto_execute"
                ),
                "proposal_id": selected.id,
                "execution_id": selected_meta.get("active_execution_id"),
                "result": "dispatched",
                "memory_delta": {},
            },
        )
    else:
        await write_journal(
            db,
            actor=actor,
            action="workspace_autonomy_controller_skip",
            target_type="workspace_proposal",
            target_id=str(selected.id),
            summary=f"Autonomy controller left proposal {selected.id} pending",
            metadata_json={
                "trigger_reason": reason,
                "policy_rule_used": "policy_or_safety_guard",
                "proposal_id": selected.id,
                "execution_id": None,
                "result": execute_reason,
                "memory_delta": {},
            },
        )

    return {
        "executed": executed,
        "result": execute_reason,
        "verification_updates": verification_updates,
        "proposal": _workspace_proposal_payload(selected),
    }


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
        "priority_zones": [
            item
            for item in (
                row.priority_zones if isinstance(row.priority_zones, list) else []
            )
            if str(item).strip()
        ],
    }


def _monitoring_state_payload(row: WorkspaceMonitoringState) -> dict:
    autonomy = _autonomy_state_from_monitoring(row)
    return {
        "desired_running": row.desired_running,
        "runtime_status": row.runtime_status,
        "is_running": bool(
            MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done()
        ),
        "task_started_at": MONITORING_RUNTIME.loop_started_at,
        "policy": _monitoring_policy_payload(row),
        "last_scan_at": row.last_scan_at,
        "scan_count": row.scan_count,
        "last_scan_reason": row.last_scan_reason,
        "last_deltas": row.last_deltas_json
        if isinstance(row.last_deltas_json, list)
        else [],
        "last_proposal_ids": row.last_proposal_ids
        if isinstance(row.last_proposal_ids, list)
        else [],
        "last_started_at": row.last_started_at,
        "last_stopped_at": row.last_stopped_at,
        "autonomy": {
            key: value
            for key, value in autonomy.items()
            if key != "recent_auto_actions"
        },
        "human_aware": _human_aware_inspectability_payload(row=row),
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
    }


def _managed_scope_from_autonomous_chain_metadata(metadata: dict | None) -> str:
    payload = metadata if isinstance(metadata, dict) else {}
    for key in ("managed_scope", "related_zone", "target_zone", "zone", "scan_area"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "global"


def _managed_scope_from_autonomous_chain_proposals(
    *,
    proposals: list[WorkspaceProposal],
    metadata: dict | None,
) -> str:
    from_metadata = _managed_scope_from_autonomous_chain_metadata(metadata)
    if from_metadata != "global":
        return from_metadata
    proposal_scopes: set[str] = set()
    for proposal in proposals:
        zone = str(getattr(proposal, "related_zone", "") or "").strip()
        if zone:
            proposal_scopes.add(zone)
            continue
        proposal_metadata = (
            proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}
        )
        zone = _managed_scope_from_autonomous_chain_metadata(proposal_metadata)
        if zone != "global":
            proposal_scopes.add(zone)
    if len(proposal_scopes) == 1:
        return next(iter(proposal_scopes))
    return "global"


async def _autonomous_chain_boundary_envelope(
    *,
    db: AsyncSession,
    managed_scope: str,
    requested_action: str,
    policy_source: str,
    reason: str,
) -> dict:
    scope = str(managed_scope or "").strip() or "global"
    boundary_row = await get_latest_autonomy_boundary_for_scope(scope=scope, db=db)
    boundary_profile = build_boundary_profile_snapshot(boundary_row)
    autonomy_context = build_autonomy_decision_context(
        boundary_profile=boundary_profile,
        requested_action=requested_action,
        policy_source=policy_source,
        auto_execution_allowed=False,
        reason=reason,
        policy_conflict=None,
    )
    action_controls = build_boundary_action_controls(boundary_profile)
    return {
        "managed_scope": scope,
        "boundary_profile": autonomy_context.get("boundary_profile", {}),
        "decision_basis": autonomy_context.get("decision_basis", {}),
        "allowed_actions": action_controls.get("allowed_actions", []),
        "approval_required": bool(action_controls.get("approval_required", False)),
        "retry_policy": action_controls.get("retry_policy", {}),
        "risk_level": str(action_controls.get("risk_level") or "").strip(),
        "boundary_enforced": bool(boundary_profile.get("boundary_id")),
    }


def _apply_autonomous_chain_boundary_metadata(
    metadata: dict | None,
    envelope: dict | None,
) -> dict:
    payload = dict(metadata) if isinstance(metadata, dict) else {}
    context = envelope if isinstance(envelope, dict) else {}
    payload["managed_scope"] = str(
        context.get("managed_scope")
        or payload.get("managed_scope")
        or "global"
    ).strip() or "global"
    payload["boundary_profile"] = (
        context.get("boundary_profile", {})
        if isinstance(context.get("boundary_profile", {}), dict)
        else {}
    )
    payload["decision_basis"] = (
        context.get("decision_basis", {})
        if isinstance(context.get("decision_basis", {}), dict)
        else {}
    )
    payload["allowed_actions"] = (
        context.get("allowed_actions", [])
        if isinstance(context.get("allowed_actions", []), list)
        else []
    )
    payload["approval_required"] = bool(context.get("approval_required", False))
    payload["retry_policy"] = (
        context.get("retry_policy", {})
        if isinstance(context.get("retry_policy", {}), dict)
        else {}
    )
    payload["risk_level"] = str(context.get("risk_level") or "").strip()
    return payload


def _to_workspace_autonomous_chain_out(row: WorkspaceAutonomousChain) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    boundary_profile = (
        metadata.get("boundary_profile", {})
        if isinstance(metadata.get("boundary_profile", {}), dict)
        else {}
    )
    decision_basis = (
        metadata.get("decision_basis", {})
        if isinstance(metadata.get("decision_basis", {}), dict)
        else {}
    )
    return {
        "chain_id": row.id,
        "chain_type": row.chain_type,
        "status": row.status,
        "source": row.source,
        "trigger_reason": row.trigger_reason,
        "proposal_ids": row.step_proposal_ids
        if isinstance(row.step_proposal_ids, list)
        else [],
        "step_policy_json": row.step_policy_json
        if isinstance(row.step_policy_json, dict)
        else {},
        "stop_on_failure": bool(row.stop_on_failure),
        "cooldown_seconds": int(row.cooldown_seconds),
        "requires_approval": bool(row.requires_approval),
        "managed_scope": str(metadata.get("managed_scope") or "global").strip() or "global",
        "boundary_profile": str(
            boundary_profile.get("current_level")
            or decision_basis.get("boundary_level")
            or ""
        ).strip(),
        "boundary_context": boundary_profile,
        "decision_basis": decision_basis,
        "allowed_actions": metadata.get("allowed_actions", []),
        "approval_required": bool(
            metadata.get("approval_required", row.requires_approval)
        ),
        "retry_policy": metadata.get("retry_policy", {}),
        "risk_level": str(metadata.get("risk_level") or "").strip(),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "last_advanced_at": row.last_advanced_at,
        "current_step_index": row.current_step_index,
        "completed_step_ids": row.completed_step_ids
        if isinstance(row.completed_step_ids, list)
        else [],
        "failed_step_ids": row.failed_step_ids
        if isinstance(row.failed_step_ids, list)
        else [],
        "audit_trail": _coerced_json_list(row.audit_trail_json),
        "metadata_json": metadata,
        "created_at": row.created_at,
    }


def _normalized_chain_step_policy(raw: dict | None) -> dict:
    policy = raw if isinstance(raw, dict) else {}
    terminal_statuses = [
        str(item).strip().lower()
        for item in (
            policy.get(
                "terminal_statuses", CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]
            )
            if isinstance(
                policy.get(
                    "terminal_statuses", CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]
                ),
                list,
            )
            else CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]
        )
        if str(item).strip()
    ]
    if not terminal_statuses:
        terminal_statuses = CHAIN_DEFAULT_STEP_POLICY["terminal_statuses"]
    failure_statuses = [
        str(item).strip().lower()
        for item in (
            policy.get(
                "failure_statuses", CHAIN_DEFAULT_STEP_POLICY["failure_statuses"]
            )
            if isinstance(
                policy.get(
                    "failure_statuses", CHAIN_DEFAULT_STEP_POLICY["failure_statuses"]
                ),
                list,
            )
            else CHAIN_DEFAULT_STEP_POLICY["failure_statuses"]
        )
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
    return INTERRUPTION_POLICY_OUTCOMES.get(
        interruption_type, "require_operator_decision"
    )


def _interruption_is_blocking(interruption_type: str) -> bool:
    return interruption_type in INTERRUPTION_BLOCKING_TYPES


async def _find_action_plan_for_execution(
    *, execution_id: int, db: AsyncSession
) -> WorkspaceActionPlan | None:
    return (
        (
            await db.execute(
                select(WorkspaceActionPlan)
                .where(WorkspaceActionPlan.execution_id == execution_id)
                .order_by(WorkspaceActionPlan.id.desc())
            )
        )
        .scalars()
        .first()
    )


async def _find_chains_for_execution(
    *, execution_id: int, db: AsyncSession
) -> list[WorkspaceAutonomousChain]:
    proposals = (
        (
            await db.execute(
                select(WorkspaceProposal).where(
                    WorkspaceProposal.source_execution_id == execution_id
                )
            )
        )
        .scalars()
        .all()
    )
    proposal_ids = {item.id for item in proposals}

    chains = (await db.execute(select(WorkspaceAutonomousChain))).scalars().all()
    matched: list[WorkspaceAutonomousChain] = []
    for chain in chains:
        metadata = chain.metadata_json if isinstance(chain.metadata_json, dict) else {}
        linked_execution_id = (
            int(metadata.get("active_execution_id", 0))
            if str(metadata.get("active_execution_id", "")).isdigit()
            else 0
        )
        if linked_execution_id == execution_id:
            matched.append(chain)
            continue
        step_ids = (
            chain.step_proposal_ids if isinstance(chain.step_proposal_ids, list) else []
        )
        if proposal_ids and any(int(item) in proposal_ids for item in step_ids):
            matched.append(chain)
    return matched


def _append_execution_interruption(
    execution: CapabilityExecution,
    *,
    event: str,
    actor: str,
    reason: str,
    metadata_json: dict,
) -> None:
    feedback = (
        execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    )
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


def _append_chain_audit(
    row: WorkspaceAutonomousChain,
    *,
    actor: str,
    event: str,
    reason: str,
    metadata_json: dict,
) -> None:
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


def _to_workspace_capability_chain_out(row: WorkspaceCapabilityChain) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    boundary_profile = (
        metadata.get("boundary_profile", {})
        if isinstance(metadata.get("boundary_profile", {}), dict)
        else {}
    )
    decision_basis = (
        metadata.get("decision_basis", {})
        if isinstance(metadata.get("decision_basis", {}), dict)
        else {}
    )
    return {
        "chain_id": row.id,
        "chain_name": row.chain_name,
        "chain_type": row.chain_type,
        "status": row.status,
        "source": row.source,
        "policy": row.policy_json if isinstance(row.policy_json, dict) else {},
        "steps": row.steps_json if isinstance(row.steps_json, list) else [],
        "managed_scope": str(metadata.get("managed_scope") or "global").strip()
        or "global",
        "boundary_profile": str(
            boundary_profile.get("current_level")
            or decision_basis.get("boundary_level")
            or ""
        ).strip(),
        "boundary_context": boundary_profile,
        "decision_basis": decision_basis,
        "allowed_actions": metadata.get("allowed_actions", []),
        "approval_required": bool(metadata.get("approval_required", False)),
        "retry_policy": metadata.get("retry_policy", {}),
        "risk_level": str(metadata.get("risk_level") or "").strip(),
        "current_step_index": int(row.current_step_index),
        "completed_step_ids": row.completed_step_ids
        if isinstance(row.completed_step_ids, list)
        else [],
        "failed_step_ids": row.failed_step_ids
        if isinstance(row.failed_step_ids, list)
        else [],
        "stop_on_failure": bool(row.stop_on_failure),
        "escalate_on_failure": bool(row.escalate_on_failure),
        "last_advanced_at": row.last_advanced_at,
        "audit_trail": _coerced_json_list(row.audit_trail_json),
        "metadata_json": metadata,
        "created_at": row.created_at,
    }


def _append_capability_chain_audit(
    row: WorkspaceCapabilityChain,
    *,
    actor: str,
    event: str,
    reason: str,
    metadata_json: dict,
) -> None:
    trail = list(_coerced_json_list(row.audit_trail_json))
    trail.append(
        {
            "actor": actor,
            "event": event,
            "reason": reason,
            "metadata_json": metadata_json,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.audit_trail_json = trail[-300:]
    flag_modified(row, "audit_trail_json")


def _managed_scope_from_capability_chain_steps(
    *,
    steps: list[dict],
    metadata: dict | None,
) -> str:
    from_metadata = _managed_scope_from_autonomous_chain_metadata(metadata)
    if from_metadata != "global":
        return from_metadata
    step_scopes: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        params = step.get("params", {}) if isinstance(step.get("params", {}), dict) else {}
        scope = _managed_scope_from_autonomous_chain_metadata(params)
        if scope != "global":
            step_scopes.add(scope)
    if len(step_scopes) == 1:
        return next(iter(step_scopes))
    return "global"


def _capability_chain_boundary_requires_confirmation(
    *,
    envelope: dict | None,
    capability: str,
) -> bool:
    context = envelope if isinstance(envelope, dict) else {}
    managed_scope = str(context.get("managed_scope") or "global").strip() or "global"
    if managed_scope == "global":
        return False
    boundary_profile = (
        context.get("boundary_profile", {})
        if isinstance(context.get("boundary_profile", {}), dict)
        else {}
    )
    boundary_scope = str(boundary_profile.get("scope") or "").strip()
    if boundary_scope != managed_scope:
        return False
    if not _is_physical_capability_step(str(capability or "").strip()):
        return False
    return bool(context.get("boundary_enforced", False)) and bool(
        context.get("approval_required", False)
    )


def _normalized_capability_chain_steps(raw_steps: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability", "")).strip()
        if not capability:
            continue
        step_id = (
            str(item.get("step_id", f"step-{index + 1}")).strip() or f"step-{index + 1}"
        )
        depends_on_raw = item.get("depends_on", [])
        depends_on = (
            [str(dep).strip() for dep in depends_on_raw if str(dep).strip()]
            if isinstance(depends_on_raw, list)
            else []
        )
        normalized.append(
            {
                "step_id": step_id,
                "capability": capability,
                "depends_on": depends_on,
                "params": item.get("params", {})
                if isinstance(item.get("params", {}), dict)
                else {},
                "verify": item.get("verify", {})
                if isinstance(item.get("verify", {}), dict)
                else {},
            }
        )
    return normalized


def _validate_capability_chain_policy(steps: list[dict]) -> tuple[bool, str]:
    if len(steps) < 2:
        return False, "capability chain must include at least two steps"
    capabilities = tuple(str(item.get("capability", "")).strip() for item in steps)
    if len(capabilities) != 2:
        return False, "objective42 supports two-step safe chains only"
    if capabilities not in SAFE_CAPABILITY_CHAIN_COMBINATIONS:
        return False, "capability chain combination is not allowed by safety policy"
    return True, "allowed"


def _validate_capability_chain_dependencies(steps: list[dict]) -> tuple[bool, str]:
    known_step_ids = [str(item.get("step_id", "")).strip() for item in steps]
    seen: set[str] = set()
    for item in steps:
        step_id = str(item.get("step_id", "")).strip()
        depends_on = (
            item.get("depends_on", [])
            if isinstance(item.get("depends_on", []), list)
            else []
        )
        for dependency in depends_on:
            if dependency not in known_step_ids:
                return False, f"dependency '{dependency}' is not present in chain"
            if dependency not in seen:
                return False, f"dependency '{dependency}' must refer to a previous step"
            if dependency == step_id:
                return False, "step cannot depend on itself"
        seen.add(step_id)
    return True, "valid"


async def _execute_capability_chain_step(
    *, chain: WorkspaceCapabilityChain, step: dict, db: AsyncSession
) -> tuple[bool, str, dict]:
    capability = str(step.get("capability", "")).strip()
    params = step.get("params", {}) if isinstance(step.get("params", {}), dict) else {}
    metadata = chain.metadata_json if isinstance(chain.metadata_json, dict) else {}
    context = (
        metadata.get("context", {})
        if isinstance(metadata.get("context", {}), dict)
        else {}
    )
    verification: dict = {"capability": capability, "step_id": step.get("step_id")}

    if capability == "workspace_scan":
        zone = str(params.get("zone", "workspace")).strip() or "workspace"
        label = (
            str(params.get("label", f"scan-{chain.id}")).strip() or f"scan-{chain.id}"
        )
        confidence = max(0.0, min(1.0, float(params.get("confidence", 0.9) or 0.9)))
        observation = WorkspaceObservation(
            zone=zone,
            label=label,
            confidence=confidence,
            source="objective42",
            lifecycle_status="active",
            metadata_json={"chain_id": chain.id, "step_id": step.get("step_id")},
        )
        db.add(observation)
        await db.flush()

        existing_object = (
            (
                await db.execute(
                    select(WorkspaceObjectMemory)
                    .where(WorkspaceObjectMemory.canonical_name == label)
                    .order_by(WorkspaceObjectMemory.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if existing_object:
            existing_object.zone = zone
            existing_object.confidence = max(
                float(existing_object.confidence), confidence
            )
            existing_object.status = "active"
            existing_object.last_seen_at = datetime.now(timezone.utc)
            object_memory_id = existing_object.id
        else:
            object_row = WorkspaceObjectMemory(
                canonical_name=label,
                candidate_labels=[label.lower()],
                confidence=confidence,
                zone=zone,
                status="active",
                location_history=[],
                metadata_json={"chain_id": chain.id, "step_id": step.get("step_id")},
            )
            db.add(object_row)
            await db.flush()
            object_memory_id = object_row.id

        context.update(
            {
                "last_scan_zone": zone,
                "last_scan_label": label,
                "last_observation_id": observation.id,
                "last_object_memory_id": object_memory_id,
            }
        )
        chain.metadata_json = {**metadata, "context": context}
        verification.update(
            {
                "observation_id": observation.id,
                "object_memory_id": object_memory_id,
                "observation_count": 1,
            }
        )
        return True, "scan_recorded", verification

    if capability == "observation_update":
        zone = (
            str(params.get("zone", context.get("last_scan_zone", "workspace"))).strip()
            or "workspace"
        )
        label = str(params.get("label", context.get("last_scan_label", ""))).strip()
        stmt = select(WorkspaceObservation).where(WorkspaceObservation.zone == zone)
        if label:
            stmt = stmt.where(WorkspaceObservation.label == label)
        rows = (
            (await db.execute(stmt.order_by(WorkspaceObservation.id.desc())))
            .scalars()
            .all()
        )
        if not rows:
            return (
                False,
                "observation_missing",
                {**verification, "zone": zone, "label": label},
            )
        verification.update(
            {
                "zone": zone,
                "label": label,
                "observation_count": len(rows),
                "latest_observation_id": rows[0].id,
            }
        )
        return True, "observation_verified", verification

    if capability == "target_resolution":
        target_label = str(
            params.get("target_label", context.get("last_scan_label", ""))
        ).strip()
        preferred_zone = str(
            params.get("preferred_zone", context.get("last_scan_zone", ""))
        ).strip()
        if not target_label:
            return False, "target_label_required", verification

        object_row = (
            (
                await db.execute(
                    select(WorkspaceObjectMemory)
                    .where(WorkspaceObjectMemory.canonical_name == target_label)
                    .order_by(WorkspaceObjectMemory.last_seen_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if not object_row:
            return (
                False,
                "target_not_found",
                {**verification, "target_label": target_label},
            )

        resolution = WorkspaceTargetResolution(
            requested_target=target_label,
            requested_zone=preferred_zone or object_row.zone,
            match_outcome="single_match",
            policy_outcome="target_ready_auto",
            status="confirmed",
            confidence=float(object_row.confidence),
            related_object_id=object_row.id,
            candidate_object_ids=[object_row.id],
            suggested_actions=["observe"],
            source="objective42",
            metadata_json={"chain_id": chain.id, "step_id": step.get("step_id")},
        )
        db.add(resolution)
        await db.flush()
        context.update(
            {
                "last_target_resolution_id": resolution.id,
                "last_target_label": target_label,
            }
        )
        chain.metadata_json = {**metadata, "context": context}
        verification.update(
            {
                "target_resolution_id": resolution.id,
                "target_label": target_label,
                "confidence": float(object_row.confidence),
            }
        )
        return True, "target_resolved", verification

    if capability == "speech_output":
        message = str(
            params.get(
                "message",
                f"Target {context.get('last_target_label', 'ready')} confirmed.",
            )
        ).strip()
        if not message:
            return False, "speech_message_required", verification
        suppress_speech = bool(params.get("human_policy_suppress", False))
        speech = SpeechOutputAction(
            requested_text=message,
            voice_profile="default",
            channel="system",
            priority="low" if suppress_speech else "normal",
            delivery_status="suppressed" if suppress_speech else "queued",
            metadata_json={
                "chain_id": chain.id,
                "step_id": step.get("step_id"),
                "human_policy_suppress": suppress_speech,
            },
        )
        db.add(speech)
        await db.flush()
        verification.update(
            {
                "speech_action_id": speech.id,
                "message": message,
                "suppressed": suppress_speech,
            }
        )
        return (
            True,
            "speech_suppressed" if suppress_speech else "speech_queued",
            verification,
        )

    if capability == "rescan_zone":
        zone = (
            str(params.get("zone", context.get("last_scan_zone", "workspace"))).strip()
            or "workspace"
        )
        confidence = max(0.0, min(1.0, float(params.get("confidence", 0.8) or 0.8)))
        proposal = WorkspaceProposal(
            proposal_type="rescan_zone",
            title=f"Rescan zone {zone}",
            description=f"Objective42 chain requests bounded rescan for {zone}.",
            status="pending",
            confidence=confidence,
            source="objective42",
            related_zone=zone,
            related_object_id=None,
            source_execution_id=None,
            trigger_json={"chain_id": chain.id, "step_id": step.get("step_id")},
            metadata_json={"chain_id": chain.id, "step_id": step.get("step_id")},
        )
        db.add(proposal)
        await db.flush()
        await _refresh_workspace_proposal_priority(proposal=proposal, db=db)
        context.update({"last_rescan_proposal_id": proposal.id, "last_scan_zone": zone})
        chain.metadata_json = {**metadata, "context": context}
        verification.update(
            {
                "proposal_id": proposal.id,
                "proposal_status": proposal.status,
                "zone": zone,
            }
        )
        return True, "rescan_proposal_created", verification

    if capability == "proposal_resolution":
        proposal_id_raw = params.get(
            "proposal_id", context.get("last_rescan_proposal_id")
        )
        proposal_id = int(proposal_id_raw) if str(proposal_id_raw).isdigit() else 0
        if proposal_id <= 0:
            return False, "proposal_id_required", verification
        proposal = await db.get(WorkspaceProposal, proposal_id)
        if not proposal:
            return (
                False,
                "proposal_not_found",
                {**verification, "proposal_id": proposal_id},
            )
        proposal.status = "resolved"
        proposal.metadata_json = {
            **(
                proposal.metadata_json
                if isinstance(proposal.metadata_json, dict)
                else {}
            ),
            "resolved_by": "objective42_chain",
            "chain_id": chain.id,
            "step_id": step.get("step_id"),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        verification.update(
            {"proposal_id": proposal.id, "proposal_status": proposal.status}
        )
        return True, "proposal_resolved", verification

    return False, "unsupported_capability", verification


async def _get_or_create_monitoring_state(db: AsyncSession) -> WorkspaceMonitoringState:
    row = (
        (
            await db.execute(
                select(WorkspaceMonitoringState).order_by(
                    WorkspaceMonitoringState.id.asc()
                )
            )
        )
        .scalars()
        .first()
    )
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _compute_object_deltas(
    *, previous_snapshot: dict, current_rows: list[WorkspaceObjectMemory]
) -> list[dict]:
    deltas: list[dict] = []
    current_snapshot = _snapshot_from_objects(current_rows)
    previous_by_name: dict[str, tuple[str, dict]] = {}
    if isinstance(previous_snapshot, dict):
        for previous_id, previous_payload in previous_snapshot.items():
            if not isinstance(previous_payload, dict):
                continue
            canonical = str(previous_payload.get("canonical_name", "")).strip().lower()
            if canonical:
                previous_by_name[canonical] = (str(previous_id), previous_payload)

    matched_previous_ids: set[str] = set()

    for row in current_rows:
        key = str(row.id)
        previous = (
            previous_snapshot.get(key, {})
            if isinstance(previous_snapshot, dict)
            else {}
        )
        if not previous:
            canonical_key = str(row.canonical_name or "").strip().lower()
            previous_alias = previous_by_name.get(canonical_key)
            if previous_alias:
                alias_previous_id, alias_previous_payload = previous_alias
                previous = (
                    alias_previous_payload
                    if isinstance(alias_previous_payload, dict)
                    else {}
                )
                matched_previous_ids.add(alias_previous_id)

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
        if row.status in {"missing", "stale"} and previous_status not in {
            "missing",
            "stale",
        }:
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
        if (
            abs(float(row.confidence) - previous_confidence)
            >= MONITORING_CONFIDENCE_DELTA_THRESHOLD
        ):
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

    previous_ids = (
        set(previous_snapshot.keys()) if isinstance(previous_snapshot, dict) else set()
    )
    current_ids = set(current_snapshot.keys())
    for orphan_id in sorted(previous_ids - current_ids - matched_previous_ids):
        previous = (
            previous_snapshot.get(orphan_id, {})
            if isinstance(previous_snapshot, dict)
            else {}
        )
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
    window_start = datetime.now(timezone.utc) - timedelta(
        seconds=MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS
    )
    existing = (
        (
            await db.execute(
                select(WorkspaceProposal)
                .where(WorkspaceProposal.proposal_type == proposal_type)
                .where(WorkspaceProposal.status == "pending")
                .where(WorkspaceProposal.created_at >= window_start)
                .where(WorkspaceProposal.related_zone == related_zone)
                .where(WorkspaceProposal.related_object_id == related_object_id)
                .order_by(WorkspaceProposal.id.desc())
            )
        )
        .scalars()
        .first()
    )
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
    last_started_at = _as_utc(row.last_started_at)
    last_scan_at = _as_utc(row.last_scan_at)
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
            if last_started_at and parsed < last_started_at:
                continue
            if (now - parsed).total_seconds() <= 60:
                recent_scans.append(parsed)

    if len(recent_scans) >= max(1, int(row.max_scan_rate)):
        return False, "max_scan_rate"

    if last_scan_at and (now - last_scan_at).total_seconds() < max(
        0, int(row.cooldown_seconds)
    ):
        return False, "cooldown"

    if row.scan_trigger_mode == "freshness":
        priority = {
            item.strip()
            for item in (
                row.priority_zones if isinstance(row.priority_zones, list) else []
            )
            if str(item).strip()
        }
        threshold = max(30, int(row.freshness_threshold_seconds))
        for item in objects:
            if priority and item.zone not in priority:
                continue
            age_seconds = max((now - item.last_seen_at).total_seconds(), 0.0)
            if age_seconds >= threshold:
                return True, "freshness_drop"
        return False, "freshness_not_due"

    if not last_scan_at:
        return True, "interval_bootstrap"
    if (now - last_scan_at).total_seconds() >= max(1, int(row.interval_seconds)):
        return True, "interval_tick"
    return False, "interval_wait"


def _monitoring_priority_zones(row: WorkspaceMonitoringState) -> list[str]:
    zones: list[str] = []
    raw = row.priority_zones if isinstance(row.priority_zones, list) else []
    for item in raw:
        value = str(item).strip()
        if value:
            zones.append(value)
    return zones


async def _run_monitoring_scan_cycle(
    *, db: AsyncSession, row: WorkspaceMonitoringState, reason: str
) -> None:
    now = datetime.now(timezone.utc)
    stmt = select(WorkspaceObjectMemory).order_by(WorkspaceObjectMemory.id.asc())
    priority_zones = _monitoring_priority_zones(row)
    if priority_zones:
        stmt = stmt.where(WorkspaceObjectMemory.zone.in_(priority_zones))
    objects = (await db.execute(stmt)).scalars().all()

    previous_snapshot = (
        row.last_snapshot_json if isinstance(row.last_snapshot_json, dict) else {}
    )
    deltas = _compute_object_deltas(
        previous_snapshot=previous_snapshot, current_rows=objects
    )

    proposal_ids: list[int] = []
    for delta in deltas:
        event = str(delta.get("event", ""))
        object_id = (
            int(delta.get("object_memory_id", 0))
            if str(delta.get("object_memory_id", "")).isdigit()
            else None
        )
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
    recent_scan_strings = (
        [item for item in recent_scans_raw if isinstance(item, str)]
        if isinstance(recent_scans_raw, list)
        else []
    )
    recent_scan_strings.append(now.isoformat())
    filtered_recent: list[str] = []
    last_started_at = _as_utc(row.last_started_at)
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
        if last_started_at and parsed < last_started_at:
            continue
        if (now - parsed).total_seconds() <= 60:
            filtered_recent.append(item)
    recent_scan_strings = filtered_recent

    delta_with_timestamps: list[dict] = []
    for item in deltas:
        if not isinstance(item, dict):
            continue
        delta_with_timestamps.append(
            {
                **item,
                "detected_at": str(item.get("detected_at") or now.isoformat()),
            }
        )

    existing_delta_history = (
        row.last_deltas_json if isinstance(row.last_deltas_json, list) else []
    )
    merged_delta_history: list[dict] = []
    for item in [*existing_delta_history, *delta_with_timestamps]:
        if not isinstance(item, dict):
            continue
        raw_detected_at = str(item.get("detected_at", "")).strip()
        if not raw_detected_at:
            raw_detected_at = now.isoformat()
        candidate = (
            raw_detected_at[:-1] + "+00:00"
            if raw_detected_at.endswith("Z")
            else raw_detected_at
        )
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = now
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        if (now - parsed).total_seconds() <= MONITORING_DELTA_HISTORY_SECONDS:
            merged_delta_history.append({**item, "detected_at": parsed.isoformat()})

    if len(merged_delta_history) > MONITORING_DELTA_HISTORY_MAX_ITEMS:
        merged_delta_history = merged_delta_history[
            -MONITORING_DELTA_HISTORY_MAX_ITEMS:
        ]

    row.last_scan_at = now
    row.scan_count = int(row.scan_count) + 1
    row.last_scan_reason = reason
    row.last_deltas_json = merged_delta_history
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

                objects_stmt = select(WorkspaceObjectMemory)
                priority_zones = _monitoring_priority_zones(row)
                if priority_zones:
                    objects_stmt = objects_stmt.where(
                        WorkspaceObjectMemory.zone.in_(priority_zones)
                    )
                objects = (await db.execute(objects_stmt)).scalars().all()
                should_scan, reason = _monitoring_should_scan(
                    row=row, objects=objects, now=datetime.now(timezone.utc)
                )
                if should_scan:
                    await _run_monitoring_scan_cycle(db=db, row=row, reason=reason)
                else:
                    row.runtime_status = "running"
                await db.commit()
                await _run_monitoring_autonomy_controller_step()
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


async def _run_monitoring_autonomy_controller_step() -> None:
    async with SessionLocal() as autonomy_db:
        try:
            await asyncio.wait_for(
                _run_autonomy_controller_step(
                    db=autonomy_db,
                    actor="workspace-autonomy-loop",
                    reason="objective41_monitoring_loop_tick",
                ),
                timeout=MONITORING_AUTONOMY_STEP_TIMEOUT_SECONDS,
            )
            await autonomy_db.commit()
        except Exception:
            await autonomy_db.rollback()


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

    collision_risk = (
        float((row.simulation_json or {}).get("collision_risk", 1.0))
        if isinstance(row.simulation_json, dict)
        else 1.0
    )
    if collision_risk >= collision_risk_threshold:
        violations.append("collision_risk_threshold_exceeded")

    if float(target.confidence) < target_confidence_minimum:
        violations.append("target_confidence_below_minimum")
    return violations


async def _ensure_execution_capability_registered(
    *, capability_name: str, db: AsyncSession
) -> CapabilityRegistration:
    row = (
        (
            await db.execute(
                select(CapabilityRegistration).where(
                    CapabilityRegistration.capability_name == capability_name
                )
            )
        )
        .scalars()
        .first()
    )
    safety_policy = {
        "scope": "actuating",
        "mode": "operator_guarded",
        "requires_simulation_safe": True,
    }
    if row:
        row.category = "manipulation"
        row.description = (
            "Execute guarded reach/approach motion from simulated workspace action plan"
        )
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


def _execution_safety_score(
    *, collision_risk: float, target_confidence: float
) -> float:
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
    row = (
        (
            await db.execute(
                select(WorkspaceZone).where(WorkspaceZone.zone_name == zone_name)
            )
        )
        .scalars()
        .first()
    )
    if not row:
        return []

    relations = (
        (
            await db.execute(
                select(WorkspaceZoneRelation).where(
                    WorkspaceZoneRelation.from_zone_id == row.id,
                    WorkspaceZoneRelation.relation_type == "adjacent_to",
                )
            )
        )
        .scalars()
        .all()
    )

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

    if target.policy_outcome in {
        "target_requires_confirmation",
        "target_stale_reobserve",
    }:
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
    boundary_profile = metadata.get("boundary_profile", {}) if isinstance(metadata.get("boundary_profile", {}), dict) else {}
    decision_basis = metadata.get("decision_basis", {}) if isinstance(metadata.get("decision_basis", {}), dict) else {}
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
        "motion_plan": row.motion_plan_json
        if isinstance(row.motion_plan_json, dict)
        else {},
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
        "simulation": row.simulation_json
        if isinstance(row.simulation_json, dict)
        else {},
        "simulation_gate_passed": row.simulation_gate_passed,
        "execution_capability": row.execution_capability,
        "execution_status": row.execution_status,
        "execution_id": row.execution_id,
        "execution": row.execution_json if isinstance(row.execution_json, dict) else {},
        "abort_status": row.abort_status,
        "abort_reason": row.abort_reason,
        "queued_task_id": row.queued_task_id,
        "source": row.source,
        "boundary_profile": str(boundary_profile.get("current_level") or decision_basis.get("boundary_level") or "").strip(),
        "boundary_context": boundary_profile,
        "decision_basis": decision_basis,
        "allowed_actions": metadata.get("allowed_actions", []),
        "approval_required": bool(metadata.get("approval_required", False)),
        "retry_policy": metadata.get("retry_policy", {}),
        "risk_level": str(metadata.get("risk_level") or "").strip(),
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
            "zones": [*([adjacent[0]] if adjacent else []), mapped_zone or target_zone]
            if target_zone
            else [],
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
    zone_row = (
        (
            await db.execute(
                select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped_zone)
            )
        )
        .scalars()
        .first()
        if mapped_zone
        else None
    )
    unknown_zone = bool(zone_name) and zone_row is None
    unsafe_zone = bool(zone_row and zone_row.hazard_level > 0)

    related_object = (
        await db.get(WorkspaceObjectMemory, target.related_object_id)
        if target.related_object_id
        else None
    )
    uncertain_identity = bool(
        related_object and related_object.status in {"uncertain", "stale", "missing"}
    )

    obstacles: list[WorkspaceObjectMemory] = []
    if zone_name:
        candidates = (
            (
                await db.execute(
                    select(WorkspaceObjectMemory).where(
                        WorkspaceObjectMemory.zone == zone_name,
                        WorkspaceObjectMemory.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
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
    path_length = round(
        0.8 + (0.35 * len(obstacles)) + (0.25 if uncertain_identity else 0.0), 3
    )
    confidence = round(
        max(0.0, min(1.0, target.confidence * (1.0 - collision_risk))), 3
    )

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
        "approach_direction": (
            row.motion_plan_json.get("approach_vector", {})
            if isinstance(row.motion_plan_json, dict)
            else {}
        ).get("direction", "unknown"),
        "clearance": (
            row.motion_plan_json.get("clearance_zone", {})
            if isinstance(row.motion_plan_json, dict)
            else {}
        ).get("required_clearance_m", 0.35),
        "obstacle_warnings": [
            *(["unknown_zone"] if unknown_zone else []),
            *(["unsafe_zone"] if unsafe_zone else []),
            *(["uncertain_object_identity"] if uncertain_identity else []),
            *([f"obstacle:{item.canonical_name}" for item in obstacles]),
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


def _to_workspace_observation_out(
    observation: WorkspaceObservation, now: datetime
) -> dict:
    freshness = _freshness_state(observation.last_seen_at, now)
    return {
        "observation_id": observation.id,
        "timestamp": observation.last_seen_at,
        "zone": observation.zone,
        "detected_object": observation.label,
        "confidence": observation.confidence,
        "effective_confidence": _effective_confidence(
            observation.confidence, freshness
        ),
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
        "aliases": item.candidate_labels
        if isinstance(item.candidate_labels, list)
        else [],
        "confidence": item.confidence,
        "effective_confidence": _effective_confidence(item.confidence, freshness),
        "zone": item.zone,
        "first_seen_at": item.first_seen_at,
        "last_seen_at": item.last_seen_at,
        "status": item.status,
        "last_execution_id": item.last_execution_id,
        "location_history": item.location_history
        if isinstance(item.location_history, list)
        else [],
        "metadata_json": item.metadata_json,
    }


def _object_library_semantic_fields(item: WorkspaceObjectMemory) -> list[str]:
    metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    semantic_keys = [
        "description",
        "purpose",
        "owner",
        "meaning",
        "category",
        "user_notes",
        "explanation",
        "expected_home_zone",
        "expected_zone",
        "home_zone",
    ]
    return [key for key in semantic_keys if str(metadata.get(key) or "").strip()]


def _object_library_profile(item: WorkspaceObjectMemory, now: datetime) -> dict:
    semantic_fields = _object_library_semantic_fields(item)
    aliases = item.candidate_labels if isinstance(item.candidate_labels, list) else []
    location_history = (
        item.location_history if isinstance(item.location_history, list) else []
    )
    metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
    score = 0.0
    promotion_reasons: list[str] = []

    if item.status == "active":
        score += 0.35
        promotion_reasons.append("currently active")
    elif item.status == "uncertain":
        score += 0.22
        promotion_reasons.append("recent but uncertain")
    elif item.status == "missing":
        score += 0.15
        promotion_reasons.append("tracked as missing")

    if semantic_fields:
        score += min(0.36, 0.12 * len(semantic_fields))
        promotion_reasons.append("has semantic memory")

    if item.last_execution_id is not None:
        score += 0.15
        promotion_reasons.append("linked to execution")

    observation_source = str(metadata.get("last_observation_source") or "").strip()
    if observation_source == "live_camera":
        score += 0.1
        promotion_reasons.append("seen by live camera")
    elif observation_source:
        score += 0.05
        promotion_reasons.append("seen by recorded observation")

    if len(location_history) > 1:
        score += 0.05
        promotion_reasons.append("has movement history")

    alias_count = len([str(item).strip() for item in aliases if str(item).strip()])
    if alias_count > 1:
        score += 0.05
        promotion_reasons.append("has aliases")

    if item.confidence >= 0.9:
        score += 0.08
    elif item.confidence >= 0.75:
        score += 0.05
    elif item.confidence >= 0.6:
        score += 0.02

    promoted = (
        item.status in {"active", "uncertain", "missing"}
        and score >= 0.45
        and (
            bool(semantic_fields)
            or item.last_execution_id is not None
            or len(location_history) > 1
            or alias_count > 1
        )
    )
    if promoted:
        promotion_reasons.append("passes promotion threshold")

    return {
        "promoted": promoted,
        "library_score": round(min(score, 1.0), 3),
        "semantic_fields": semantic_fields,
        "promotion_reasons": promotion_reasons,
    }


def _to_workspace_object_library_out(
    item: WorkspaceObjectMemory, now: datetime
) -> dict:
    return {
        **_to_workspace_object_out(item, now),
        **_object_library_profile(item, now),
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
        for relation_type in [
            "adjacent_to",
            "left_of",
            "right_of",
            "in_front_of",
            "behind",
        ]:
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
    stmt = select(WorkspaceObservation).order_by(
        WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc()
    )
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
async def get_workspace_observation(
    observation_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
    stmt = select(WorkspaceObjectMemory).order_by(
        WorkspaceObjectMemory.last_seen_at.desc(), WorkspaceObjectMemory.id.desc()
    )
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
            aliases = (
                row.candidate_labels if isinstance(row.candidate_labels, list) else []
            )
            candidates = {
                row.canonical_name.lower(),
                *[str(item).lower() for item in aliases],
            }
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


@router.get("/object-library")
async def list_workspace_object_library(
    label: str = "",
    zone: str = "",
    include_stale: bool = Query(default=False),
    promoted_only: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _ensure_default_zone_map(db)
    stmt = select(WorkspaceObjectMemory).order_by(
        WorkspaceObjectMemory.last_seen_at.desc(), WorkspaceObjectMemory.id.desc()
    )
    if zone.strip():
        stmt = stmt.where(WorkspaceObjectMemory.zone == zone.strip())

    rows = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)

    changed = False
    wanted = label.strip().lower()
    filtered: list[WorkspaceObjectMemory] = []
    summary = {
        "total_objects": 0,
        "promoted_objects": 0,
        "semantic_objects": 0,
        "active_objects": 0,
        "uncertain_objects": 0,
        "missing_objects": 0,
        "stale_objects": 0,
        "execution_backed_objects": 0,
    }
    library_objects: list[dict] = []

    for row in rows:
        before = row.status
        _apply_object_status_aging(row, now)
        if before != row.status:
            changed = True

        summary["total_objects"] += 1
        summary_key = f"{row.status}_objects"
        if summary_key in summary:
            summary[summary_key] += 1
        if row.last_execution_id is not None:
            summary["execution_backed_objects"] += 1

        profile = _object_library_profile(row, now)
        if profile["semantic_fields"]:
            summary["semantic_objects"] += 1
        if profile["promoted"]:
            summary["promoted_objects"] += 1

        if not include_stale and row.status == "stale":
            continue

        if promoted_only and not profile["promoted"]:
            continue

        if wanted:
            aliases = (
                row.candidate_labels if isinstance(row.candidate_labels, list) else []
            )
            candidates = {
                row.canonical_name.lower(),
                *[str(item).lower() for item in aliases],
            }
            owner = str(
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "owner"
                )
                or ""
            ).lower()
            if owner:
                candidates.add(owner)
                candidates.update(f"{owner} {value}" for value in list(candidates))
            if not any(wanted in value for value in candidates):
                continue

        filtered.append(row)
        library_objects.append(_to_workspace_object_library_out(row, now))

    if changed:
        await db.commit()
        for row in filtered:
            await db.refresh(row)

    library_objects.sort(
        key=lambda item: (
            float(item.get("library_score", 0.0)),
            str(item.get("last_seen_at", "")),
            int(item.get("object_memory_id", 0)),
        ),
        reverse=True,
    )

    return {
        "summary": summary,
        "objects": library_objects,
    }


@router.get("/objects/{object_memory_id}")
async def get_workspace_object(
    object_memory_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
async def get_workspace_object_relations(
    object_memory_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    await _ensure_default_zone_map(db)
    row = await db.get(WorkspaceObjectMemory, object_memory_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace object not found")

    relations = (
        (
            await db.execute(
                select(WorkspaceObjectRelation)
                .where(
                    (WorkspaceObjectRelation.subject_object_id == object_memory_id)
                    | (WorkspaceObjectRelation.object_object_id == object_memory_id)
                )
                .order_by(
                    WorkspaceObjectRelation.last_seen_at.desc(),
                    WorkspaceObjectRelation.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )

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
    zones = (
        (
            await db.execute(
                select(WorkspaceZone).order_by(WorkspaceZone.zone_name.asc())
            )
        )
        .scalars()
        .all()
    )
    relations = (
        (
            await db.execute(
                select(WorkspaceZoneRelation).order_by(WorkspaceZoneRelation.id.asc())
            )
        )
        .scalars()
        .all()
    )

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
    zones = (
        (
            await db.execute(
                select(WorkspaceZone).order_by(WorkspaceZone.zone_name.asc())
            )
        )
        .scalars()
        .all()
    )
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
    if row.desired_running and not (
        MONITORING_RUNTIME.task and not MONITORING_RUNTIME.task.done()
    ):
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
    await _stop_monitoring_runtime()
    row = await _get_or_create_monitoring_state(db)
    existing_metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.desired_running = True
    row.runtime_status = "running"
    row.scan_trigger_mode = payload.trigger_mode
    row.interval_seconds = payload.interval_seconds
    row.freshness_threshold_seconds = payload.freshness_threshold_seconds
    row.cooldown_seconds = payload.cooldown_seconds
    row.max_scan_rate = payload.max_scan_rate
    row.priority_zones = [
        item.strip() for item in payload.priority_zones if str(item).strip()
    ]
    row.last_scan_at = None
    row.last_scan_reason = ""
    row.scan_count = 0
    row.last_deltas_json = []
    row.last_proposal_ids = []
    row.last_snapshot_json = {}
    row.last_started_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **existing_metadata,
        "recent_scans": [],
        "started_by": payload.actor,
        "start_reason": payload.reason,
        **payload.metadata_json,
    }

    await _run_monitoring_scan_cycle(db=db, row=row, reason="start_bootstrap")

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


@router.get("/human-aware/state")
async def get_workspace_human_aware_state(db: AsyncSession = Depends(get_db)) -> dict:
    row = await _get_or_create_monitoring_state(db)
    return _human_aware_inspectability_payload(row=row)


@router.post("/human-aware/signals")
async def update_workspace_human_aware_signals(
    payload: WorkspaceHumanAwareSignalUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_or_create_monitoring_state(db)
    state = _human_aware_state_from_monitoring(row)

    for key in [
        "human_in_workspace",
        "human_near_target_zone",
        "human_near_motion_path",
        "shared_workspace_active",
        "operator_present",
    ]:
        value = getattr(payload, key)
        if value is not None:
            state[key] = bool(value)

    if payload.occupied_zones:
        state["occupied_zones"] = [
            normalized
            for normalized in (
                _normalize_zone_for_map(item) for item in payload.occupied_zones
            )
            if normalized
        ]
    if payload.high_proximity_zones:
        state["high_proximity_zones"] = [
            normalized
            for normalized in (
                _normalize_zone_for_map(item) for item in payload.high_proximity_zones
            )
            if normalized
        ]

    state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    state["last_updated_by"] = payload.actor
    state["last_reason"] = payload.reason
    state["last_policy_decision"] = {
        "outcome": "continue",
        "reason": "signals_updated",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _store_human_aware_state(row, state)

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_human_aware_signals_update",
        target_type="workspace_monitoring",
        target_id=str(row.id),
        summary="Updated human-aware workspace signals",
        metadata_json={
            "reason": payload.reason,
            "signals": {
                "human_in_workspace": state.get("human_in_workspace", False),
                "human_near_target_zone": state.get("human_near_target_zone", False),
                "human_near_motion_path": state.get("human_near_motion_path", False),
                "shared_workspace_active": state.get("shared_workspace_active", False),
                "operator_present": state.get("operator_present", False),
                "occupied_zones": state.get("occupied_zones", []),
                "high_proximity_zones": state.get("high_proximity_zones", []),
            },
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return _human_aware_inspectability_payload(row=row)


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
            "policy_version": proposal_priority_policy.get(
                "version", PROPOSAL_PRIORITY_POLICY_VERSION
            ),
            "weights": proposal_priority_policy.get("weights", {}),
            "urgency_map": proposal_priority_policy.get("urgency_map", {}),
            "zone_importance": proposal_priority_policy.get("zone_importance", {}),
            "operator_preference": proposal_priority_policy.get(
                "operator_preference", {}
            ),
            "age_saturation_minutes": proposal_priority_policy.get(
                "age_saturation_minutes", 120
            ),
        },
        "autonomy": {
            key: value
            for key, value in autonomy.items()
            if key != "recent_auto_actions"
        },
        "human_aware": _human_aware_inspectability_payload(row=row),
        "policy_outcomes": sorted(list(AUTONOMY_POLICY_OUTCOMES)),
    }


@router.post("/autonomy/loop/step")
async def run_workspace_autonomy_loop_step(
    actor: str = "workspace-autonomy-controller",
    reason: str = "objective41_controller_step",
    zone_filter: str = "",
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await _run_autonomy_controller_step(
        db=db, actor=actor, reason=reason, zone_filter=zone_filter
    )
    await db.commit()
    return {
        "actor": actor,
        "reason": reason,
        "zone_filter": zone_filter,
        **result,
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
        autonomy["cooldown_between_actions_seconds"] = (
            payload.cooldown_between_actions_seconds
        )
    if payload.max_auto_tasks_per_window is not None:
        autonomy["max_auto_tasks_per_window"] = payload.max_auto_tasks_per_window
    if payload.auto_window_seconds is not None:
        autonomy["auto_window_seconds"] = payload.auto_window_seconds
    if payload.capability_cooldown_seconds:
        autonomy["capability_cooldown_seconds"] = {
            str(key).strip(): max(0, int(value))
            for key, value in payload.capability_cooldown_seconds.items()
            if str(key).strip()
        }
    if payload.zone_action_limits:
        autonomy["zone_action_limits"] = {
            str(key).strip(): max(1, int(value))
            for key, value in payload.zone_action_limits.items()
            if str(key).strip()
        }
    if payload.restricted_zones:
        autonomy["restricted_zones"] = [
            _normalize_zone_for_map(str(item))
            for item in payload.restricted_zones
            if _normalize_zone_for_map(str(item))
        ]
    if payload.auto_safe_confidence_threshold is not None:
        autonomy["auto_safe_confidence_threshold"] = (
            payload.auto_safe_confidence_threshold
        )
    if payload.auto_preferred_confidence_threshold is not None:
        autonomy["auto_preferred_confidence_threshold"] = (
            payload.auto_preferred_confidence_threshold
        )
    if payload.low_risk_score_max is not None:
        autonomy["low_risk_score_max"] = payload.low_risk_score_max
    if payload.max_autonomy_retries is not None:
        autonomy["max_autonomy_retries"] = payload.max_autonomy_retries
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
        or payload.max_auto_tasks_per_window is not None
        or payload.auto_window_seconds is not None
        or payload.auto_safe_confidence_threshold is not None
        or payload.auto_preferred_confidence_threshold is not None
        or payload.low_risk_score_max is not None
        or payload.max_autonomy_retries is not None
    ):
        await apply_learning_signal(
            db=db, signal="policy_override", user_id=payload.actor or DEFAULT_USER_ID
        )

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


@router.post("/autonomy/boundaries/evaluate")
async def evaluate_workspace_adaptive_autonomy_boundaries(
    payload: AdaptiveAutonomyBoundaryEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await evaluate_adaptive_autonomy_boundaries(
        actor=payload.actor,
        source=payload.source,
        scope=payload.scope,
        lookback_hours=payload.lookback_hours,
        min_samples=payload.min_samples,
        apply_recommended_boundaries=payload.apply_recommended_boundaries,
        hard_ceiling_overrides=payload.hard_ceiling_overrides,
        evidence_inputs_override=payload.evidence_inputs_override,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_adaptive_autonomy_boundaries_evaluated",
        target_type="workspace_autonomy_boundary_profile",
        target_id=str(row.id),
        summary=f"Evaluated adaptive autonomy boundaries profile {row.id}",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_samples": payload.min_samples,
            "scope": payload.scope,
            "apply_recommended_boundaries": payload.apply_recommended_boundaries,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "profile": to_autonomy_boundary_profile_out(row),
    }


@router.get("/autonomy/boundaries")
async def list_workspace_adaptive_autonomy_boundary_profiles(
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_autonomy_boundary_profiles(db=db, status=status, limit=limit)
    return {
        "profiles": [to_autonomy_boundary_profile_out(item) for item in rows],
    }


@router.get("/autonomy/boundaries/{profile_id}")
async def get_workspace_adaptive_autonomy_boundary_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_autonomy_boundary_profile(profile_id=profile_id, db=db)
    if not row:
        raise HTTPException(
            status_code=404, detail="autonomy_boundary_profile_not_found"
        )
    return {
        "profile": to_autonomy_boundary_profile_out(row),
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


@router.get("/capability-chains")
async def list_workspace_capability_chains(
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspaceCapabilityChain).order_by(WorkspaceCapabilityChain.id.desc())
    if status.strip():
        stmt = stmt.where(WorkspaceCapabilityChain.status == status.strip())
    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {"chains": [_to_workspace_capability_chain_out(row) for row in rows]}


@router.post("/capability-chains")
async def create_workspace_capability_chain(
    payload: WorkspaceCapabilityChainCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    steps = _normalized_capability_chain_steps(payload.steps)
    if len(steps) < 2:
        raise HTTPException(
            status_code=422,
            detail="capability chain must include at least two valid steps",
        )

    allowed, policy_reason = _validate_capability_chain_policy(steps)
    if not allowed:
        raise HTTPException(status_code=422, detail=policy_reason)

    deps_ok, deps_reason = _validate_capability_chain_dependencies(steps)
    if not deps_ok:
        raise HTTPException(status_code=422, detail=deps_reason)

    policy = {
        "version": CAPABILITY_CHAIN_POLICY_VERSION,
        "allowed_combinations": [
            list(item) for item in sorted(SAFE_CAPABILITY_CHAIN_COMBINATIONS)
        ],
        "combination": [steps[0].get("capability"), steps[1].get("capability")],
        **(payload.policy_json if isinstance(payload.policy_json, dict) else {}),
    }
    managed_scope = _managed_scope_from_capability_chain_steps(
        steps=steps,
        metadata=payload.metadata_json,
    )
    boundary_envelope = await _autonomous_chain_boundary_envelope(
        db=db,
        managed_scope=managed_scope,
        requested_action=payload.chain_type,
        policy_source="workspace_capability_chain",
        reason=(
            f"Creating workspace capability chain {payload.chain_name} for scope {managed_scope}."
        ),
    )

    row = WorkspaceCapabilityChain(
        chain_name=payload.chain_name,
        chain_type=payload.chain_type,
        status="pending",
        source=payload.source,
        policy_json=policy,
        steps_json=steps,
        current_step_index=0,
        completed_step_ids=[],
        failed_step_ids=[],
        stop_on_failure=payload.stop_on_failure,
        escalate_on_failure=payload.escalate_on_failure,
        audit_trail_json=[],
        metadata_json=_apply_autonomous_chain_boundary_metadata(
            {
                "created_by": payload.actor,
                "reason": payload.reason,
                **payload.metadata_json,
            },
            boundary_envelope,
        ),
    )
    db.add(row)
    await db.flush()

    _append_capability_chain_audit(
        row,
        actor=payload.actor,
        event="capability_chain_created",
        reason=payload.reason,
        metadata_json={
            "policy_version": CAPABILITY_CHAIN_POLICY_VERSION,
            "steps": steps,
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_capability_chain_create",
        target_type="workspace_capability_chain",
        target_id=str(row.id),
        summary=f"Created workspace capability chain {row.id}",
        metadata_json={
            "chain_name": row.chain_name,
            "policy": policy,
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )
    await db.commit()
    await db.refresh(row)
    return _to_workspace_capability_chain_out(row)


@router.get("/capability-chains/{chain_id}")
async def get_workspace_capability_chain(
    chain_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceCapabilityChain, chain_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace capability chain not found"
        )
    return _to_workspace_capability_chain_out(row)


@router.get("/capability-chains/{chain_id}/audit")
async def get_workspace_capability_chain_audit(
    chain_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceCapabilityChain, chain_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace capability chain not found"
        )
    return {
        "chain_id": row.id,
        "status": row.status,
        "audit_trail": _coerced_json_list(row.audit_trail_json),
    }


@router.post("/capability-chains/{chain_id}/advance")
async def advance_workspace_capability_chain(
    chain_id: int,
    payload: WorkspaceCapabilityChainAdvanceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(WorkspaceCapabilityChain, chain_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace capability chain not found"
        )
    if row.status in {"completed", "failed", "canceled"}:
        raise HTTPException(
            status_code=422, detail="workspace capability chain is terminal"
        )

    steps = row.steps_json if isinstance(row.steps_json, list) else []
    if row.current_step_index >= len(steps):
        row.status = "completed"
        await db.commit()
        await db.refresh(row)
        return _to_workspace_capability_chain_out(row)

    step = (
        steps[row.current_step_index]
        if isinstance(steps[row.current_step_index], dict)
        else {}
    )
    step_id = str(step.get("step_id", f"step-{row.current_step_index + 1}")).strip()
    capability = str(step.get("capability", "")).strip()
    boundary_envelope = await _autonomous_chain_boundary_envelope(
        db=db,
        managed_scope=_managed_scope_from_autonomous_chain_metadata(
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        ),
        requested_action=capability or row.chain_type,
        policy_source="workspace_capability_chain_advance",
        reason=(
            f"Advancing workspace capability chain {row.id} step {step_id} ({capability or row.chain_type})."
        ),
    )
    if _capability_chain_boundary_requires_confirmation(
        envelope=boundary_envelope,
        capability=capability,
    ) and not payload.force:
        row.status = "pending_confirmation"
        row.metadata_json = _apply_autonomous_chain_boundary_metadata(
            {
                **(
                    row.metadata_json if isinstance(row.metadata_json, dict) else {}
                ),
                "last_boundary_hold": {
                    "actor": payload.actor,
                    "reason": payload.reason,
                    "step_id": step_id,
                    "capability": capability,
                    **payload.metadata_json,
                },
            },
            boundary_envelope,
        )
        _append_capability_chain_audit(
            row,
            actor=payload.actor,
            event="capability_step_blocked_boundary_policy",
            reason=payload.reason,
            metadata_json={
                "step_id": step_id,
                "capability": capability,
                "result": "operator_confirmation_required",
                "status": row.status,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
        await write_journal(
            db,
            actor=payload.actor,
            action="workspace_capability_chain_boundary_policy",
            target_type="workspace_capability_chain",
            target_id=str(row.id),
            summary=f"Boundary policy gated workspace capability chain {row.id}",
            metadata_json={
                "step_id": step_id,
                "capability": capability,
                "status": row.status,
                "result": "operator_confirmation_required",
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
        await db.commit()
        await db.refresh(row)
        return {
            **_to_workspace_capability_chain_out(row),
            "last_step": {
                "step_id": step_id,
                "capability": capability,
                "result": "operator_confirmation_required",
                "success": False,
                "verification": {
                    "policy_outcome": "require_operator_confirmation",
                    "policy_reason": "autonomy_boundary_operator_required",
                    "boundary_context": boundary_envelope.get("boundary_profile", {}),
                },
            },
        }

    monitoring = await _get_or_create_monitoring_state(db)
    human_aware = _human_aware_state_from_monitoring(monitoring)
    _, chain_constraint_result = await evaluate_and_record_constraints(
        actor=payload.actor,
        source="objective44_capability_chain_advance",
        goal={
            "goal_type": "advance_capability_chain",
            "chain_id": row.id,
            "step_id": step_id,
        },
        action_plan={
            "action_type": str(step.get("capability", "")).strip(),
            "chain_id": row.id,
            "step_id": step_id,
            "is_physical": _is_physical_capability_step(
                str(step.get("capability", "")).strip()
            ),
        },
        workspace_state={
            "human_in_workspace": human_aware.get("human_in_workspace", False),
            "human_near_target_zone": human_aware.get("human_near_target_zone", False),
            "human_near_motion_path": human_aware.get("human_near_motion_path", False),
            "shared_workspace_active": human_aware.get(
                "shared_workspace_active", False
            ),
            "target_confidence": 1.0,
            "map_freshness_seconds": 0,
        },
        system_state={"throttle_blocked": False, "integrity_risk": False},
        policy_state={
            "min_target_confidence": 0.7,
            "map_freshness_limit_seconds": MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
            "unlawful_action": False,
        },
        metadata_json={"reason": payload.reason, **payload.metadata_json},
        db=db,
    )
    chain_decision = str(chain_constraint_result.get("decision", "allowed"))
    if chain_decision in {"requires_replan", "blocked"} and not payload.force:
        row.status = "failed" if chain_decision == "blocked" else "pending_replan"
        _append_capability_chain_audit(
            row,
            actor=payload.actor,
            event="capability_step_blocked_constraint_engine",
            reason=f"decision={chain_decision}",
            metadata_json={
                "step_id": step_id,
                "constraint_result": chain_constraint_result,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
        row.metadata_json = _apply_autonomous_chain_boundary_metadata(
            {
                **(
                    row.metadata_json if isinstance(row.metadata_json, dict) else {}
                ),
                "last_constraint_block": {
                    "actor": payload.actor,
                    "reason": payload.reason,
                    "step_id": step_id,
                    "constraint_result": chain_constraint_result,
                    **payload.metadata_json,
                },
            },
            boundary_envelope,
        )
        await db.commit()
        await db.refresh(row)
        return {
            **_to_workspace_capability_chain_out(row),
            "last_step": {
                "step_id": step_id,
                "capability": step.get("capability", ""),
                "result": f"constraint_{chain_decision}",
                "success": False,
                "verification": {"constraint_result": chain_constraint_result},
            },
        }

    human_outcome, human_reason = _human_aware_policy_for_capability_step(
        step=step, human_aware=human_aware
    )
    if (
        human_outcome in {"pause", "require_operator_confirmation", "stop_replan"}
        and not payload.force
    ):
        if human_outcome == "pause":
            row.status = "paused"
            result = "paused_by_human_presence"
        elif human_outcome == "require_operator_confirmation":
            row.status = "pending_confirmation"
            result = "operator_confirmation_required"
        else:
            row.status = "failed"
            result = "stopped_for_replan"
            if row.escalate_on_failure:
                metadata = (
                    row.metadata_json if isinstance(row.metadata_json, dict) else {}
                )
                row.metadata_json = {
                    **metadata,
                    "escalation": {
                        "required": True,
                        "reason": human_reason,
                        "step_id": step_id,
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                }
        _append_capability_chain_audit(
            row,
            actor=payload.actor,
            event="capability_step_blocked_human_policy",
            reason=human_reason,
            metadata_json={
                "step_id": step_id,
                "outcome": human_outcome,
                "result": result,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
        human_aware["last_policy_decision"] = {
            "outcome": human_outcome,
            "reason": human_reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _store_human_aware_state(monitoring, human_aware)
        row.metadata_json = _apply_autonomous_chain_boundary_metadata(
            {
                **(
                    row.metadata_json if isinstance(row.metadata_json, dict) else {}
                ),
                "last_human_policy": {
                    "actor": payload.actor,
                    "reason": payload.reason,
                    "step_id": step_id,
                    "outcome": human_outcome,
                    "policy_reason": human_reason,
                    **payload.metadata_json,
                },
            },
            boundary_envelope,
        )

        await write_journal(
            db,
            actor=payload.actor,
            action="workspace_capability_chain_human_policy",
            target_type="workspace_capability_chain",
            target_id=str(row.id),
            summary=f"Human-aware policy applied for workspace capability chain {row.id}",
            metadata_json={
                "step_id": step_id,
                "outcome": human_outcome,
                "reason": human_reason,
                "status": row.status,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
        await db.commit()
        await db.refresh(row)
        return {
            **_to_workspace_capability_chain_out(row),
            "last_step": {
                "step_id": step_id,
                "capability": step.get("capability", ""),
                "result": result,
                "success": False,
                "verification": {
                    "policy_outcome": human_outcome,
                    "policy_reason": human_reason,
                },
            },
        }

    if (
        human_outcome == "slow_suppress"
        and str(step.get("capability", "")).strip() == "speech_output"
    ):
        params = (
            step.get("params", {}) if isinstance(step.get("params", {}), dict) else {}
        )
        step["params"] = {
            **params,
            "human_policy_suppress": True,
        }
        human_aware["last_policy_decision"] = {
            "outcome": human_outcome,
            "reason": human_reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _store_human_aware_state(monitoring, human_aware)

    depends_on = (
        step.get("depends_on", [])
        if isinstance(step.get("depends_on", []), list)
        else []
    )
    completed_step_ids = (
        row.completed_step_ids if isinstance(row.completed_step_ids, list) else []
    )
    unmet = [dep for dep in depends_on if dep not in completed_step_ids]
    if unmet and not payload.force:
        raise HTTPException(
            status_code=422,
            detail=f"capability chain dependency unmet: {', '.join(unmet)}",
        )

    success, result, verification = await _execute_capability_chain_step(
        chain=row, step=step, db=db
    )
    row.last_advanced_at = datetime.now(timezone.utc)

    if success:
        completed = list(completed_step_ids)
        if step_id not in completed:
            completed.append(step_id)
        row.completed_step_ids = completed
        row.current_step_index = int(row.current_step_index) + 1
        row.status = "completed" if row.current_step_index >= len(steps) else "active"
        _append_capability_chain_audit(
            row,
            actor=payload.actor,
            event="capability_step_completed",
            reason=payload.reason or result,
            metadata_json={
                "step_id": step_id,
                "verification": verification,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )
    else:
        failed = row.failed_step_ids if isinstance(row.failed_step_ids, list) else []
        if step_id not in failed:
            failed.append(step_id)
        row.failed_step_ids = failed
        if row.stop_on_failure and not payload.force:
            row.status = "failed"
        else:
            row.current_step_index = int(row.current_step_index) + 1
            row.status = (
                "completed" if row.current_step_index >= len(steps) else "active"
            )
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if row.escalate_on_failure:
            row.metadata_json = {
                **metadata,
                "escalation": {
                    "required": True,
                    "reason": result,
                    "step_id": step_id,
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            }
        _append_capability_chain_audit(
            row,
            actor=payload.actor,
            event="capability_step_failed",
            reason=payload.reason or result,
            metadata_json={
                "step_id": step_id,
                "verification": verification,
                "escalated": row.escalate_on_failure,
                **payload.metadata_json,
                **{
                    key: value
                    for key, value in boundary_envelope.items()
                    if key != "boundary_enforced"
                },
            },
        )

    row.metadata_json = _apply_autonomous_chain_boundary_metadata(
        {
            **(
                row.metadata_json if isinstance(row.metadata_json, dict) else {}
            ),
            "last_advance": {
                "actor": payload.actor,
                "reason": payload.reason,
                "step_id": step_id,
                "capability": capability,
                "force": payload.force,
                "result": result,
                "success": success,
                **payload.metadata_json,
            },
        },
        boundary_envelope,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_capability_chain_advance",
        target_type="workspace_capability_chain",
        target_id=str(row.id),
        summary=f"Advanced workspace capability chain {row.id}: {result}",
        metadata_json={
            "step_id": step_id,
            "result": result,
            "success": success,
            "status": row.status,
            "verification": verification,
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        **_to_workspace_capability_chain_out(row),
        "last_step": {
            "step_id": step_id,
            "capability": step.get("capability", ""),
            "result": result,
            "success": success,
            "verification": verification,
        },
    }


@router.post("/chains")
async def create_workspace_autonomous_chain(
    payload: WorkspaceAutonomousChainCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposal_ids = [int(item) for item in payload.proposal_ids if int(item) > 0]
    if not proposal_ids:
        raise HTTPException(
            status_code=422, detail="proposal_ids must include at least one proposal"
        )

    valid_ids: list[int] = []
    matched_proposals: list[WorkspaceProposal] = []
    for proposal_id in proposal_ids:
        proposal = await db.get(WorkspaceProposal, proposal_id)
        if proposal and proposal.status in {"pending", "accepted"}:
            valid_ids.append(proposal_id)
            matched_proposals.append(proposal)

    if not valid_ids:
        raise HTTPException(
            status_code=422, detail="no valid proposals found for chain"
        )

    step_policy = _normalized_chain_step_policy(payload.step_policy_json)
    managed_scope = _managed_scope_from_autonomous_chain_proposals(
        proposals=matched_proposals,
        metadata=payload.metadata_json,
    )
    boundary_envelope = await _autonomous_chain_boundary_envelope(
        db=db,
        managed_scope=managed_scope,
        requested_action=payload.chain_type,
        policy_source="workspace_autonomous_chain",
        reason=(
            f"Creating workspace autonomous chain for scope {managed_scope}."
        ),
    )
    requires_approval = bool(payload.requires_approval)
    if bool(boundary_envelope.get("boundary_enforced", False)) and bool(
        boundary_envelope.get("approval_required", False)
    ):
        requires_approval = True

    row = WorkspaceAutonomousChain(
        chain_type=payload.chain_type,
        status="pending_approval" if requires_approval else "active",
        source=payload.source,
        trigger_reason=payload.reason,
        step_proposal_ids=valid_ids,
        step_policy_json=step_policy,
        stop_on_failure=payload.stop_on_failure,
        cooldown_seconds=payload.cooldown_seconds,
        requires_approval=requires_approval,
        current_step_index=0,
        completed_step_ids=[],
        failed_step_ids=[],
        audit_trail_json=[],
        metadata_json=_apply_autonomous_chain_boundary_metadata(
            {
                "created_by": payload.actor,
                **payload.metadata_json,
            },
            boundary_envelope,
        ),
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
            "requires_approval": requires_approval,
            "cooldown_seconds": payload.cooldown_seconds,
            "stop_on_failure": payload.stop_on_failure,
            "step_policy_json": step_policy,
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )

    _append_chain_audit(
        row,
        actor=payload.actor,
        event="chain_created",
        reason=payload.reason,
        metadata_json={
            "requires_approval": requires_approval,
            "cooldown_seconds": payload.cooldown_seconds,
            "stop_on_failure": payload.stop_on_failure,
            "step_policy_json": step_policy,
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_autonomous_chain_out(row)


@router.get("/chains/{chain_id}")
async def get_workspace_autonomous_chain(
    chain_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace autonomous chain not found"
        )
    return _to_workspace_autonomous_chain_out(row)


@router.get("/chains/{chain_id}/audit")
async def get_workspace_autonomous_chain_audit(
    chain_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceAutonomousChain, chain_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace autonomous chain not found"
        )
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
        raise HTTPException(
            status_code=404, detail="workspace autonomous chain not found"
        )
    if row.status in {"completed", "failed", "canceled"}:
        raise HTTPException(
            status_code=422,
            detail="workspace autonomous chain is terminal and cannot be approved",
        )

    row.approved_by = payload.actor
    row.approved_at = datetime.now(timezone.utc)
    if row.status == "pending_approval":
        row.status = "active"
    boundary_envelope = await _autonomous_chain_boundary_envelope(
        db=db,
        managed_scope=_managed_scope_from_autonomous_chain_metadata(
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        ),
        requested_action=row.chain_type,
        policy_source="workspace_autonomous_chain_approve",
        reason=(
            f"Approving workspace autonomous chain {row.id} for active execution."
        ),
    )
    row.metadata_json = _apply_autonomous_chain_boundary_metadata(
        {
            **(
                row.metadata_json if isinstance(row.metadata_json, dict) else {}
            ),
            "last_approval": {
                "actor": payload.actor,
                "reason": payload.reason,
                **payload.metadata_json,
            },
        },
        boundary_envelope,
    )

    _append_chain_audit(
        row,
        actor=payload.actor,
        event="chain_approved",
        reason=payload.reason,
        metadata_json={
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
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
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
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
        raise HTTPException(
            status_code=404, detail="workspace autonomous chain not found"
        )
    if row.status not in {"active", "pending"}:
        raise HTTPException(
            status_code=422, detail="workspace autonomous chain is not advanceable"
        )

    boundary_envelope = await _autonomous_chain_boundary_envelope(
        db=db,
        managed_scope=_managed_scope_from_autonomous_chain_metadata(
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        ),
        requested_action=row.chain_type,
        policy_source="workspace_autonomous_chain_advance",
        reason=(
            f"Advancing workspace autonomous chain {row.id} through its current proposal step."
        ),
    )

    if row.requires_approval and not row.approved_at:
        raise HTTPException(
            status_code=422,
            detail="workspace autonomous chain requires approval before advance",
        )

    now = datetime.now(timezone.utc)
    if row.last_advanced_at and int(row.cooldown_seconds) > 0:
        elapsed = (now - row.last_advanced_at).total_seconds()
        if elapsed < int(row.cooldown_seconds):
            raise HTTPException(
                status_code=429, detail="workspace autonomous chain cooldown active"
            )

    step_policy = _normalized_chain_step_policy(
        row.step_policy_json if isinstance(row.step_policy_json, dict) else {}
    )

    proposal_ids = (
        row.step_proposal_ids if isinstance(row.step_proposal_ids, list) else []
    )
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
        failed = (
            list(row.failed_step_ids) if isinstance(row.failed_step_ids, list) else []
        )
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
            failed = (
                list(row.failed_step_ids)
                if isinstance(row.failed_step_ids, list)
                else []
            )
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
            completed = (
                list(row.completed_step_ids)
                if isinstance(row.completed_step_ids, list)
                else []
            )
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
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
        },
    )

    if proposal and (proposal.status in {"accepted", "rejected"} or payload.force):
        completed = (
            list(row.completed_step_ids)
            if isinstance(row.completed_step_ids, list)
            else []
        )
        if current_proposal_id not in completed:
            completed.append(current_proposal_id)
            row.completed_step_ids = completed

    row.metadata_json = _apply_autonomous_chain_boundary_metadata(
        {
            **(
                row.metadata_json if isinstance(row.metadata_json, dict) else {}
            ),
            "last_advance": {
                "actor": payload.actor,
                "reason": payload.reason,
                "force": payload.force,
                **payload.metadata_json,
            },
        },
        boundary_envelope,
    )

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
            **payload.metadata_json,
            **{
                key: value
                for key, value in boundary_envelope.items()
                if key != "boundary_enforced"
            },
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
        if status.strip() == "pending" or (
            not status.strip() and row.status == "pending"
        ):
            await _refresh_workspace_proposal_priority(proposal=row, db=db)
            refresh_needed = True
    if refresh_needed:
        await db.commit()
    return {"proposals": [_workspace_proposal_payload(row) for row in rows]}


@router.get("/proposals/priority-policy")
async def get_workspace_proposal_priority_policy(
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    existing = (
        metadata.get("proposal_priority_policy", {})
        if isinstance(metadata.get("proposal_priority_policy", {}), dict)
        else {}
    )

    updated = {
        **existing,
        "weights": {
            **(
                existing.get("weights", {})
                if isinstance(existing.get("weights", {}), dict)
                else {}
            ),
            **{
                key: _normalize_score(value)
                for key, value in payload.weights.items()
                if str(key).strip()
            },
        },
        "urgency_map": {
            **(
                existing.get("urgency_map", {})
                if isinstance(existing.get("urgency_map", {}), dict)
                else {}
            ),
            **{
                str(key).strip(): _normalize_score(value)
                for key, value in payload.urgency_map.items()
                if str(key).strip()
            },
        },
        "zone_importance": {
            **(
                existing.get("zone_importance", {})
                if isinstance(existing.get("zone_importance", {}), dict)
                else {}
            ),
            **{
                str(key).strip(): _normalize_score(value)
                for key, value in payload.zone_importance.items()
                if str(key).strip()
            },
        },
        "operator_preference": {
            **(
                existing.get("operator_preference", {})
                if isinstance(existing.get("operator_preference", {}), dict)
                else {}
            ),
            **{
                str(key).strip(): _normalize_score(value)
                for key, value in payload.operator_preference.items()
                if str(key).strip()
            },
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
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    status_filter = status.strip() or "pending"
    rows = (
        (
            await db.execute(
                select(WorkspaceProposal)
                .where(WorkspaceProposal.status == status_filter)
                .order_by(WorkspaceProposal.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
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
            "policy_version": selected_priority.get("policy", {}).get(
                "version", PROPOSAL_PRIORITY_POLICY_VERSION
            ),
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
        "policy_version": selected_priority.get("policy", {}).get(
            "version", PROPOSAL_PRIORITY_POLICY_VERSION
        ),
        "priority_breakdown": selected_priority.get("breakdown", {}),
        "notification": notification,
    }


@router.post("/proposals/arbitration-outcomes")
async def record_workspace_proposal_arbitration_outcome_endpoint(
    payload: WorkspaceProposalArbitrationOutcomeRecordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await record_workspace_proposal_arbitration_outcome(
            actor=payload.actor,
            source=payload.source,
            proposal_id=payload.proposal_id,
            proposal_type=payload.proposal_type,
            related_zone=payload.related_zone,
            arbitration_decision=payload.arbitration_decision,
            arbitration_posture=payload.arbitration_posture,
            trust_chain_status=payload.trust_chain_status,
            downstream_execution_outcome=payload.downstream_execution_outcome,
            confidence=payload.confidence,
            arbitration_reason=payload.reason,
            conflict_context_json=payload.conflict_context_json,
            commitment_state_json=payload.commitment_state_json,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "workspace_proposal_not_found":
            raise HTTPException(status_code=404, detail="workspace proposal not found")
        if code == "proposal_type_required":
            raise HTTPException(status_code=422, detail="proposal_type_required")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_proposal_arbitration_outcome_recorded",
        target_type="workspace_proposal_arbitration_outcome",
        target_id=str(row.id),
        summary=(
            f"Recorded proposal arbitration outcome {row.id} for {row.proposal_type} "
            f"decision={row.arbitration_decision}"
        ),
        metadata_json={
            "proposal_id": row.proposal_id,
            "proposal_type": row.proposal_type,
            "related_zone": row.related_zone,
            "trust_chain_status": row.trust_chain_status,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "outcome": WorkspaceProposalArbitrationOutcomeOut(
            **to_workspace_proposal_arbitration_out(row)
        ).model_dump()
    }


@router.get("/proposals/arbitration-outcomes")
async def list_workspace_proposal_arbitration_outcomes_endpoint(
    proposal_type: str = Query(default=""),
    related_zone: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_workspace_proposal_arbitration_outcomes(
        db=db,
        proposal_type=proposal_type,
        related_zone=related_zone,
        limit=limit,
    )
    return {
        "outcomes": [
            WorkspaceProposalArbitrationOutcomeOut(
                **to_workspace_proposal_arbitration_out(item)
            ).model_dump()
            for item in rows
        ]
    }


@router.get("/proposals/arbitration-learning")
async def list_workspace_proposal_arbitration_learning_endpoint(
    proposal_type: str = Query(default=""),
    related_zone: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if str(proposal_type or "").strip():
        summary = await workspace_proposal_arbitration_learning_bias(
            proposal_type=proposal_type,
            related_zone=related_zone,
            db=db,
        )
        return {
            "learning": [
                WorkspaceProposalArbitrationLearningOut(**summary).model_dump()
            ]
        }
    payload = await list_workspace_proposal_arbitration_learning(
        db=db,
        related_zone=related_zone,
        limit=limit,
    )
    return {
        "learning": [
            WorkspaceProposalArbitrationLearningOut(**item).model_dump()
            for item in payload
        ]
    }


@router.get("/proposals/policy-preferences")
async def list_workspace_proposal_policy_preferences_endpoint(
    proposal_type: str = Query(default=""),
    related_zone: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if str(proposal_type or "").strip():
        payload = await converge_workspace_proposal_policy_preference(
            proposal_type=proposal_type,
            related_zone=related_zone,
            db=db,
        )
        await db.commit()
        return {
            "preferences": [
                WorkspaceProposalPolicyPreferenceOut(**payload).model_dump()
            ]
        }
    payload = await list_workspace_proposal_policy_preferences(
        db=db,
        related_zone=related_zone,
        proposal_type=proposal_type,
        limit=limit,
    )
    return {
        "preferences": [
            WorkspaceProposalPolicyPreferenceOut(**item).model_dump()
            for item in payload
        ]
    }


@router.get("/proposals/policy-conflicts")
async def list_workspace_policy_conflicts_endpoint(
    proposal_type: str = Query(default=""),
    related_zone: str = Query(default=""),
    conflict_state: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    payload = await list_workspace_policy_conflict_profiles(
        db=db,
        managed_scope=related_zone,
        decision_family="workspace_proposal_shaping",
        proposal_type=proposal_type,
        conflict_state=conflict_state,
        limit=limit,
    )
    return {
        "conflicts": [
            WorkspacePolicyConflictProfileOut(**item).model_dump()
            for item in payload
        ]
    }


@router.get("/proposals/{proposal_id}")
async def get_workspace_proposal(
    proposal_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
    await apply_learning_signal(
        db=db, signal="proposal_accept", user_id=payload.actor or DEFAULT_USER_ID
    )
    notification = await _notification_payload_for_proposal(
        db=db, proposal=proposal, action="accepted"
    )

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
    await apply_learning_signal(
        db=db, signal="proposal_reject", user_id=payload.actor or DEFAULT_USER_ID
    )
    notification = await _notification_payload_for_proposal(
        db=db, proposal=proposal, action="rejected"
    )

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
async def resolve_workspace_target(
    payload: WorkspaceTargetResolveRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    await _ensure_default_zone_map(db)

    rows = (
        (
            await db.execute(
                select(WorkspaceObjectMemory).order_by(
                    WorkspaceObjectMemory.last_seen_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    requested_zone = payload.preferred_zone.strip()
    unsafe_set = {item.strip() for item in payload.unsafe_zones if item.strip()}
    preferred_threshold_raw = await get_user_preference_value(
        db=db,
        preference_type="preferred_confirmation_threshold",
        user_id=DEFAULT_USER_ID,
    )
    preferred_confirmation_threshold = max(
        0.5, min(0.99, float(preferred_threshold_raw or 0.9))
    )
    auto_exec_safe_tasks = bool(
        await get_user_preference_value(
            db=db,
            preference_type="auto_exec_safe_tasks",
            user_id=DEFAULT_USER_ID,
        )
    )
    if auto_exec_safe_tasks:
        preferred_confirmation_threshold = max(
            0.5, preferred_confirmation_threshold - 0.05
        )

    scored: list[tuple[WorkspaceObjectMemory, float]] = []
    for row in rows:
        aliases = row.candidate_labels if isinstance(row.candidate_labels, list) else []
        candidate_labels = [row.canonical_name, *[str(item) for item in aliases]]
        best_label_score = max(
            (_label_score(payload.target_label, item) for item in candidate_labels),
            default=0.0,
        )
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
            elif (
                match_outcome == "exact_match"
                and top_row.status == "active"
                and top_score >= preferred_confirmation_threshold
            ):
                policy_outcome = "target_confirmed"
                status = "confirmed"
                suggested_actions = ["create proposal", "queue safely"]
            else:
                policy_outcome = "target_requires_confirmation"
                status = "pending_confirmation"
                suggested_actions = [
                    "request operator confirmation",
                    "rescan target zone",
                ]

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
        metadata_json={
            "unsafe_zones": sorted(list(unsafe_set)),
            "trigger": trigger_json,
        },
    )
    db.add(target)
    await db.flush()

    proposal_id: int | None = None
    if payload.create_proposal and policy_outcome in {
        "target_confirmed",
        "target_requires_confirmation",
        "target_stale_reobserve",
    }:
        proposal_type = (
            "target_confirmed"
            if policy_outcome == "target_confirmed"
            else "target_reobserve"
            if policy_outcome == "target_stale_reobserve"
            else "target_confirmation"
        )
        proposal = await _create_workspace_proposal_for_target(
            title=f"Target resolution: {payload.target_label}",
            description=f"Policy outcome: {policy_outcome}",
            proposal_type=proposal_type,
            confidence=confidence,
            related_zone=requested_zone,
            related_object_id=related_object_id,
            trigger_json={
                "target_resolution_id": target.id,
                "policy_outcome": policy_outcome,
            },
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
async def get_workspace_target_resolution(
    target_resolution_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceTargetResolution, target_resolution_id)
    if not row:
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )
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
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

    if row.status not in {"pending_confirmation", "confirmed"}:
        raise HTTPException(
            status_code=422, detail="workspace target resolution is not confirmable"
        )

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
        metadata_json={
            "proposal_id": proposal.id,
            "related_object_id": row.related_object_id,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        "target_resolution_id": row.id,
        "status": row.status,
        "policy_outcome": row.policy_outcome,
        "proposal_id": proposal.id,
    }


@router.post("/targets/{target_resolution_id}/simulate")
async def simulate_workspace_target(
    target_resolution_id: int,
    payload: WorkspaceTargetSimulateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Run safe-reach simulation for a directed target resolution.

    Computes reachability and collision risk using the safe_reach_simulation_service,
    persists a WorkspaceReachSimulation record, optionally updates any linked
    WorkspaceActionPlan simulation fields, and returns a structured result with
    blocked_reason and recovery_action when the gate does not pass.
    """
    target = await db.get(WorkspaceTargetResolution, target_resolution_id)
    if not target:
        raise HTTPException(status_code=404, detail="workspace target resolution not found")

    # Resolve zone hazard level
    zone_name = str(target.requested_zone or "").strip()
    mapped_zone = _normalize_zone_for_map(zone_name)
    zone_row: WorkspaceZone | None = (
        (
            await db.execute(
                select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped_zone)
            )
        )
        .scalars()
        .first()
        if mapped_zone
        else None
    )
    zone_hazard_level: int | None = int(zone_row.hazard_level) if zone_row else None

    # Check explicit unsafe_zones list from payload
    explicit_unsafe = {str(z).strip() for z in payload.unsafe_zones if z}
    if mapped_zone and mapped_zone in explicit_unsafe:
        zone_hazard_level = max(zone_hazard_level or 0, 1)

    # Resolve target object status
    related_object: WorkspaceObjectMemory | None = (
        await db.get(WorkspaceObjectMemory, target.related_object_id)
        if target.related_object_id
        else None
    )
    target_object_status = str(related_object.status) if related_object else "active"

    # Collect obstacles in target zone (active, high-confidence, not the target)
    nearby_objects: list[dict] = []
    if zone_name:
        candidates = (
            (
                await db.execute(
                    select(WorkspaceObjectMemory).where(
                        WorkspaceObjectMemory.zone == zone_name,
                        WorkspaceObjectMemory.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        target_obj_id = target.related_object_id
        for item in candidates:
            if target_obj_id and item.id == target_obj_id:
                continue
            if item.confidence < 0.8:
                continue
            nearby_objects.append(
                {
                    "id": item.id,
                    "canonical_name": item.canonical_name,
                    "zone": item.zone,
                    "status": item.status,
                    "confidence": item.confidence,
                }
            )

    # Run simulation
    sim_result = _run_safe_reach_simulation(
        target_zone=zone_name,
        target_object_status=target_object_status,
        target_confidence=float(target.confidence),
        zone_hazard_level=zone_hazard_level,
        safety_envelope=payload.safety_envelope,
        nearby_objects=nearby_objects,
        collision_risk_threshold=payload.collision_risk_threshold,
    )

    # Persist WorkspaceReachSimulation record
    sim_record = WorkspaceReachSimulation(
        target_resolution_id=target_resolution_id,
        target_zone=zone_name,
        simulation_outcome=sim_result.simulation_outcome,
        simulation_status=sim_result.simulation_status,
        simulation_gate_passed=sim_result.simulation_gate_passed,
        reachability_result=sim_result.reachability.reason,
        reachability_confidence=sim_result.reachability.confidence,
        collision_risk_score=sim_result.collision_risk.risk_score,
        blocked_reason=sim_result.blocked_reason,
        recovery_action=sim_result.recovery_action,
        simulation_json={
            **sim_result.simulation_json,
            "simulated_by": payload.actor,
            "simulate_reason": payload.reason,
            "simulated_at": datetime.now(timezone.utc).isoformat(),
        },
        actor=payload.actor,
        source="target_simulate",
        metadata_json=payload.metadata_json,
    )
    db.add(sim_record)
    await db.flush()  # get sim_record.id before commit

    # If a WorkspaceActionPlan exists for this target, update its simulation fields too.
    linked_plan: WorkspaceActionPlan | None = (
        (
            await db.execute(
                select(WorkspaceActionPlan)
                .where(WorkspaceActionPlan.target_resolution_id == target_resolution_id)
                .order_by(WorkspaceActionPlan.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if linked_plan and linked_plan.status not in {"queued", "rejected"}:
        linked_plan.simulation_outcome = sim_result.plan_outcome
        linked_plan.simulation_status = "completed"
        linked_plan.simulation_gate_passed = sim_result.simulation_gate_passed
        linked_plan.simulation_json = {
            **sim_result.simulation_json,
            "simulated_by": payload.actor,
            "simulate_reason": payload.reason,
            "simulated_at": datetime.now(timezone.utc).isoformat(),
            "reach_simulation_id": sim_record.id,
        }
        sim_record.action_plan_id = linked_plan.id

    # Update linked proposals' trigger_json with gate result so
    # _autonomy_simulation_safe can read it without a DB query.
    linked_proposals = (
        (
            await db.execute(
                select(WorkspaceProposal).where(
                    WorkspaceProposal.related_object_id == target.related_object_id
                    if target.related_object_id
                    else WorkspaceProposal.id == -1  # no match
                )
            )
        )
        .scalars()
        .all()
        if target.related_object_id
        else []
    )
    for proposal in linked_proposals:
        if proposal.status in {"accepted", "rejected"}:
            continue
        existing_trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
        proposal.trigger_json = {
            **existing_trigger,
            "simulation_gate_passed": sim_result.simulation_gate_passed,
            "simulation_outcome": sim_result.plan_outcome,
            "reach_simulation_id": sim_record.id,
        }
        flag_modified(proposal, "trigger_json")

    await write_journal(
        db,
        actor=payload.actor,
        action="workspace_target_simulate",
        target_type="workspace_target_resolution",
        target_id=str(target_resolution_id),
        summary=(
            f"Safe-reach simulation for target {target_resolution_id}: "
            f"{sim_result.simulation_outcome} (gate={'passed' if sim_result.simulation_gate_passed else 'blocked'})"
        ),
        metadata_json={
            "outcome": sim_result.simulation_outcome,
            "gate_passed": sim_result.simulation_gate_passed,
            "collision_risk": sim_result.collision_risk.risk_score,
            "collision_risk_threshold": payload.collision_risk_threshold,
            "blocked_reason": sim_result.blocked_reason,
            "recovery_action": sim_result.recovery_action,
            "reach_simulation_id": sim_record.id,
        },
    )

    await db.commit()
    await db.refresh(sim_record)

    return {
        "target_resolution_id": target_resolution_id,
        "reach_simulation_id": sim_record.id,
        "simulation_outcome": sim_result.simulation_outcome,
        "plan_outcome": sim_result.plan_outcome,
        "simulation_status": sim_result.simulation_status,
        "simulation_gate_passed": sim_result.simulation_gate_passed,
        "blocked_reason": sim_result.blocked_reason,
        "recovery_action": sim_result.recovery_action,
        "reachability": {
            "reachable": sim_result.reachability.reachable,
            "confidence": sim_result.reachability.confidence,
            "reason": sim_result.reachability.reason,
        },
        "collision_risk": {
            "risk_score": sim_result.collision_risk.risk_score,
            "obstacle_count": sim_result.collision_risk.obstacle_count,
            "obstacle_names": sim_result.collision_risk.obstacle_names,
            "warnings": sim_result.collision_risk.warnings,
            "blocked_vectors": sim_result.collision_risk.blocked_vectors,
        },
        "simulation": sim_record.simulation_json,
        "action_plan_id": linked_plan.id if linked_plan else None,
    }


@router.post("/action-plans")
async def create_workspace_action_plan(
    payload: WorkspaceActionPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    target = await db.get(WorkspaceTargetResolution, payload.target_resolution_id)
    if not target:
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

    planning_outcome, steps, status = _action_plan_policy(
        target=target, action_type=payload.action_type
    )
    managed_scope = str(target.requested_zone or "").strip() or "global"
    boundary_row = await get_latest_autonomy_boundary_for_scope(scope=managed_scope, db=db)
    boundary_profile = build_boundary_profile_snapshot(boundary_row)
    autonomy_context = build_autonomy_decision_context(
        boundary_profile=boundary_profile,
        requested_action=payload.action_type,
        policy_source="workspace_action_plan",
        auto_execution_allowed=False,
        reason=f"Planning action {payload.action_type} for scope {managed_scope}.",
        policy_conflict=None,
    )
    action_controls = build_boundary_action_controls(boundary_profile)
    steps = [
        {
            **step,
            "boundary_profile": str(boundary_profile.get("current_level") or action_controls.get("boundary_profile") or "").strip(),
            "decision_basis": autonomy_context.get("decision_basis", {}),
            "allowed_actions": action_controls.get("allowed_actions", []),
            "approval_required": bool(action_controls.get("approval_required", True)),
            "retry_policy": action_controls.get("retry_policy", {}),
            "risk_level": str(action_controls.get("risk_level") or "medium").strip(),
        }
        for step in steps
    ]

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
            "managed_scope": managed_scope,
            "boundary_profile": autonomy_context.get("boundary_profile", {}),
            "decision_basis": autonomy_context.get("decision_basis", {}),
            "allowed_actions": action_controls.get("allowed_actions", []),
            "approval_required": bool(action_controls.get("approval_required", True)),
            "retry_policy": action_controls.get("retry_policy", {}),
            "risk_level": str(action_controls.get("risk_level") or "medium").strip(),
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
        **(
            payload.motion_plan_overrides
            if isinstance(payload.motion_plan_overrides, dict)
            else {}
        ),
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
            "boundary_profile": autonomy_context.get("boundary_profile", {}),
            "decision_basis": autonomy_context.get("decision_basis", {}),
            "allowed_actions": action_controls.get("allowed_actions", []),
            "approval_required": bool(action_controls.get("approval_required", True)),
            "retry_policy": action_controls.get("retry_policy", {}),
            "risk_level": str(action_controls.get("risk_level") or "medium").strip(),
        },
    )

    await db.commit()
    await db.refresh(row)
    return _to_workspace_action_plan_out(row)


@router.get("/action-plans/{plan_id}")
async def get_workspace_action_plan(
    plan_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
        raise HTTPException(
            status_code=422, detail="workspace action plan cannot be approved"
        )

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
        metadata_json={
            "prior_status": prior,
            "boundary_profile": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("boundary_profile", {}),
            "decision_basis": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("decision_basis", {}),
            "allowed_actions": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("allowed_actions", []),
            "approval_required": bool((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("approval_required", False)),
            "retry_policy": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("retry_policy", {}),
            "risk_level": str((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("risk_level") or "").strip(),
        },
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
        raise HTTPException(
            status_code=422, detail="workspace action plan cannot be rejected"
        )

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
        metadata_json={
            "prior_status": prior,
            "boundary_profile": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("boundary_profile", {}),
            "decision_basis": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("decision_basis", {}),
            "allowed_actions": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("allowed_actions", []),
            "approval_required": bool((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("approval_required", False)),
            "retry_policy": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("retry_policy", {}),
            "risk_level": str((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("risk_level") or "").strip(),
        },
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
        raise HTTPException(
            status_code=422,
            detail="workspace action plan must be approved before queue",
        )
    if row.simulation_status == "completed" and (
        row.simulation_outcome != "plan_safe" or not row.simulation_gate_passed
    ):
        raise HTTPException(
            status_code=422,
            detail="workspace action plan simulation must pass before queue",
        )

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
        metadata_json={
            "task_id": task.id,
            "requested_executor": payload.requested_executor,
            "boundary_profile": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("boundary_profile", {}),
            "decision_basis": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("decision_basis", {}),
            "allowed_actions": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("allowed_actions", []),
            "approval_required": bool((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("approval_required", False)),
            "retry_policy": (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("retry_policy", {}),
            "risk_level": str((row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("risk_level") or "").strip(),
        },
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
        raise HTTPException(
            status_code=422,
            detail="workspace action plan must be approved before execute",
        )
    if (
        row.simulation_outcome != "plan_safe"
        or row.simulation_status != "completed"
        or not row.simulation_gate_passed
    ):
        raise HTTPException(
            status_code=422,
            detail="workspace action plan simulation_status must be plan_safe before execute",
        )

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

    monitoring = await _get_or_create_monitoring_state(db)
    human_aware = _human_aware_state_from_monitoring(monitoring)
    _, constraint_result = await evaluate_and_record_constraints(
        actor=payload.actor,
        source="objective44_action_plan_execute",
        goal={
            "goal_type": "execute_action_plan",
            "plan_id": row.id,
            "desired_state": "execution_dispatched",
        },
        action_plan={
            "action_type": "execute_action_plan",
            "plan_id": row.id,
            "capability_name": payload.capability_name,
            "is_physical": True,
            "managed_scope": row.target_zone or target.requested_zone or "global",
            "zone": row.target_zone or target.requested_zone or "global",
        },
        workspace_state={
            "human_in_workspace": human_aware.get("human_in_workspace", False),
            "human_near_target_zone": human_aware.get("human_near_target_zone", False),
            "human_near_motion_path": human_aware.get("human_near_motion_path", False),
            "shared_workspace_active": human_aware.get(
                "shared_workspace_active", False
            ),
            "target_confidence": float(target.confidence),
            "map_freshness_seconds": 0,
            "managed_scope": row.target_zone or target.requested_zone or "global",
            "zone": row.target_zone or target.requested_zone or "global",
        },
        system_state={"throttle_blocked": False, "integrity_risk": False},
        policy_state={
            "min_target_confidence": payload.target_confidence_minimum,
            "map_freshness_limit_seconds": MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
            "unlawful_action": False,
        },
        metadata_json={
            "reason": payload.reason,
            "managed_scope": row.target_zone or target.requested_zone or "global",
            "zone": row.target_zone or target.requested_zone or "global",
            **payload.metadata_json,
        },
        db=db,
    )
    decision = str(constraint_result.get("decision", "allowed"))
    if decision in {"requires_confirmation", "requires_replan", "blocked"}:
        raise HTTPException(
            status_code=422, detail=f"constraint engine blocked execution: {decision}"
        )

    predictive_freshness = await _evaluate_predictive_freshness(
        action_plan=row, target=target, db=db
    )
    predictive_outcome = predictive_freshness.get(
        "recommended_outcome", "continue_monitor"
    )
    if predictive_outcome == "pause_and_resimulate":
        raise HTTPException(
            status_code=422,
            detail="workspace action plan requires resimulation before execute",
        )
    if predictive_outcome == "require_replan":
        raise HTTPException(
            status_code=422,
            detail="workspace action plan requires replan before execute",
        )
    if predictive_outcome == "abort_chain":
        raise HTTPException(
            status_code=422,
            detail="workspace action plan blocked by severe predictive drift",
        )

    violations = _execution_precondition_violations(
        row=row,
        target=target,
        collision_risk_threshold=payload.collision_risk_threshold,
        target_confidence_minimum=payload.target_confidence_minimum,
    )
    if violations:
        raise HTTPException(
            status_code=422,
            detail=f"workspace action plan execution preconditions failed: {', '.join(violations)}",
        )

    collision_risk = (
        float((row.simulation_json or {}).get("collision_risk", 1.0))
        if isinstance(row.simulation_json, dict)
        else 1.0
    )

    if payload.capability_name not in EXECUTION_ALLOWED_CAPABILITIES:
        raise HTTPException(
            status_code=422, detail="workspace action plan capability is not allowed"
        )

    capability = await _ensure_execution_capability_registered(
        capability_name=payload.capability_name, db=db
    )
    if not capability.enabled:
        raise HTTPException(
            status_code=422, detail="workspace action plan capability is disabled"
        )

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
    target_pose = (
        motion_plan.get("target_pose", {})
        if isinstance(motion_plan.get("target_pose", {}), dict)
        else {}
    )
    approach_vector = (
        motion_plan.get("approach_vector", {})
        if isinstance(motion_plan.get("approach_vector", {}), dict)
        else {}
    )
    clearance = (
        motion_plan.get("clearance_zone", {})
        if isinstance(motion_plan.get("clearance_zone", {}), dict)
        else {}
    )
    safety_score = _execution_safety_score(
        collision_risk=collision_risk, target_confidence=target.confidence
    )
    managed_scope = infer_managed_scope(
        row.metadata_json if isinstance(row.metadata_json, dict) else {},
        row.execution_json if isinstance(row.execution_json, dict) else {},
        {"target_zone": getattr(target, "zone", "")},
    )
    health_gate = _physical_execution_health_gate()
    gate_result = await evaluate_execution_policy_gate(
        db=db,
        capability_name=payload.capability_name,
        requested_decision=str(health_gate.get("requested_decision") or "queued_for_executor"),
        requested_status=str(health_gate.get("requested_status") or "dispatched"),
        requested_reason=payload.reason
        or str(health_gate.get("requested_reason") or "workspace_action_plan_execute"),
        requested_executor=payload.requested_executor,
        safety_mode="operator_controlled",
        managed_scope=managed_scope,
        actor=payload.actor,
        source="workspace_action_plan_execute",
        metadata_json=payload.metadata_json,
    )
    boundary_profile = gate_result.get("boundary_profile", {}) if isinstance(gate_result.get("boundary_profile", {}), dict) else {}
    decision_basis = gate_result.get("decision_basis", {}) if isinstance(gate_result.get("decision_basis", {}), dict) else {}

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
        requested_executor=gate_result["requested_executor"],
        dispatch_decision=gate_result["dispatch_decision"],
        managed_scope=gate_result["managed_scope"],
        status=gate_result["status"],
        reason=gate_result["reason"],
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
            "health_gate": health_gate,
            "boundary_profile": boundary_profile,
            "decision_basis": decision_basis,
            "allowed_actions": gate_result.get("allowed_actions", []),
            "approval_required": bool(gate_result.get("approval_required", False)),
            "retry_policy": gate_result.get("retry_policy", {}),
            "risk_level": str(gate_result.get("risk_level") or "").strip(),
            "execution_policy_gate": json.loads(json.dumps(gate_result, default=str)),
        },
    )
    db.add(execution)
    await db.flush()

    control_state = await sync_execution_control_state(
        db=db,
        execution=execution,
        actor=payload.actor,
        source="workspace_action_plan_execute",
        requested_goal=f"workspace_action_plan:{row.id}",
        intent_key=build_intent_key(
            execution_source="workspace_action_plan",
            subject_id=row.id,
            capability_name=payload.capability_name,
        ),
        intent_type="workspace_action_plan_execution",
        context_json={
            "plan_id": row.id,
            "target_resolution_id": row.target_resolution_id,
            "target_zone": getattr(target, "zone", ""),
        },
        gate_result=gate_result,
    )

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
        "trace_id": control_state["trace_id"],
        "managed_scope": control_state["managed_scope"],
        "task_id": task.id,
        "health_gate": health_gate,
        "boundary_profile": boundary_profile,
        "decision_basis": decision_basis,
        "allowed_actions": gate_result.get("allowed_actions", []),
        "approval_required": bool(gate_result.get("approval_required", False)),
        "retry_policy": gate_result.get("retry_policy", {}),
        "risk_level": str(gate_result.get("risk_level") or "").strip(),
    }
    row.status = "executing" if str(execution.status or "") == "dispatched" else "approved"
    row.planning_outcome = (
        "plan_executing"
        if str(execution.status or "") == "dispatched"
        else "plan_waiting_health_confirmation"
    )
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "boundary_profile": boundary_profile,
        "decision_basis": decision_basis,
        "allowed_actions": gate_result.get("allowed_actions", []),
        "approval_required": bool(gate_result.get("approval_required", False)),
        "retry_policy": gate_result.get("retry_policy", {}),
        "risk_level": str(gate_result.get("risk_level") or "").strip(),
        "execution": {
            "queued_by": payload.actor,
            "queue_reason": payload.reason,
            "capability_name": payload.capability_name,
            "requested_executor": payload.requested_executor,
            "collision_risk_threshold": payload.collision_risk_threshold,
            "target_confidence_minimum": payload.target_confidence_minimum,
            "health_gate": health_gate,
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
            "boundary_profile": boundary_profile,
            "decision_basis": decision_basis,
            "allowed_actions": gate_result.get("allowed_actions", []),
            "approval_required": bool(gate_result.get("approval_required", False)),
            "retry_policy": gate_result.get("retry_policy", {}),
            "risk_level": str(gate_result.get("risk_level") or "").strip(),
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
    stmt = (
        select(WorkspaceProposal)
        .where(WorkspaceProposal.proposal_type == EXECUTION_PROPOSAL_TYPE)
        .order_by(WorkspaceProposal.id.desc())
    )
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
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

    pending_rows = (
        (
            await db.execute(
                select(WorkspaceProposal).where(
                    WorkspaceProposal.proposal_type == EXECUTION_PROPOSAL_TYPE,
                    WorkspaceProposal.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    for existing in pending_rows:
        trigger = (
            existing.trigger_json if isinstance(existing.trigger_json, dict) else {}
        )
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
        raise HTTPException(
            status_code=422,
            detail=f"workspace action plan cannot be proposed for execution: {', '.join(violations)}",
        )

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
        raise HTTPException(
            status_code=404, detail="workspace execution proposal not found"
        )
    if proposal.status != "pending":
        raise HTTPException(
            status_code=422, detail="workspace execution proposal is not pending"
        )

    trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
    plan_id = int(trigger.get("plan_id", 0))
    if plan_id <= 0:
        raise HTTPException(
            status_code=422,
            detail="workspace execution proposal missing plan reference",
        )

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
        raise HTTPException(
            status_code=404, detail="workspace execution proposal not found"
        )
    if proposal.status != "pending":
        raise HTTPException(
            status_code=422, detail="workspace execution proposal is not pending"
        )

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
        raise HTTPException(
            status_code=422, detail="workspace action plan has no active execution"
        )

    execution = await db.get(CapabilityExecution, row.execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    if execution.status in {"succeeded", "failed", "blocked"}:
        raise HTTPException(
            status_code=422,
            detail="workspace action plan execution cannot be aborted in current state",
        )

    prior_execution_status = execution.status
    history = (
        list(execution.feedback_json.get("history", []))
        if isinstance(execution.feedback_json, dict)
        else []
    )
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
        **(
            execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
        ),
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
        raise HTTPException(
            status_code=422,
            detail="workspace action plan cannot be simulated in current state",
        )

    target = await db.get(WorkspaceTargetResolution, row.target_resolution_id)
    if not target:
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

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
                **(
                    row.motion_plan_json.get("estimated_path", {})
                    if isinstance(row.motion_plan_json.get("estimated_path", {}), dict)
                    else {}
                ),
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
async def get_workspace_action_plan_simulation(
    plan_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    row = await db.get(WorkspaceActionPlan, plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="workspace action plan not found")
    return {
        "plan_id": row.id,
        "target_resolution_id": row.target_resolution_id,
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
        "simulation_gate_passed": row.simulation_gate_passed,
        "motion_plan": row.motion_plan_json
        if isinstance(row.motion_plan_json, dict)
        else {},
        "simulation": row.simulation_json
        if isinstance(row.simulation_json, dict)
        else {},
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
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
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
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "created_at": row.created_at,
    }


async def _active_replan_signals_for_execution(
    *, execution_id: int, db: AsyncSession
) -> list[WorkspaceReplanSignal]:
    return (
        (
            await db.execute(
                select(WorkspaceReplanSignal)
                .where(WorkspaceReplanSignal.execution_id == execution_id)
                .where(WorkspaceReplanSignal.status == "active")
                .order_by(WorkspaceReplanSignal.id.desc())
            )
        )
        .scalars()
        .all()
    )


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
            related_object = await db.get(
                WorkspaceObjectMemory, target.related_object_id
            )

    if related_object is None:
        freshness["target_memory_fresh"] = False
        reasons.append("target_memory_missing")
        recommended_outcome = _merge_replan_outcomes(
            primary=recommended_outcome, secondary="require_replan"
        )
        confidence = max(confidence, 0.75)
    else:
        age_seconds = max((now - related_object.last_seen_at).total_seconds(), 0.0)
        if related_object.status in {"missing", "stale"}:
            freshness["target_memory_fresh"] = False
            reasons.append("target_no_longer_valid")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="abort_chain"
            )
            confidence = max(confidence, 0.92)
            operator_confirmation_required = True
        elif related_object.status == "uncertain":
            freshness["target_memory_fresh"] = False
            reasons.append("target_identity_uncertain")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="pause_and_resimulate"
            )
            confidence = max(confidence, 0.7)
        elif age_seconds > OUTDATED_WINDOW_SECONDS:
            freshness["target_memory_fresh"] = False
            reasons.append("target_memory_stale")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="pause_and_resimulate"
            )
            confidence = max(confidence, 0.62)

        if target_zone and related_object.zone and related_object.zone != target_zone:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("object_moved_since_plan")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="require_replan"
            )
            confidence = max(confidence, 0.8)

        if target and float(related_object.confidence) + 0.05 < float(
            target.confidence
        ):
            reasons.append("confidence_drop")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="require_replan"
            )
            confidence = max(confidence, 0.78)
            operator_confirmation_required = True

    mapped_zone = _normalize_zone_for_map(target_zone)
    if mapped_zone:
        zone = (
            (
                await db.execute(
                    select(WorkspaceZone).where(WorkspaceZone.zone_name == mapped_zone)
                )
            )
            .scalars()
            .first()
        )
        if zone and int(zone.hazard_level) > 0:
            freshness["map_context_stable"] = False
            reasons.append("zone_state_changed")
            severe_outcome = (
                "abort_chain" if int(zone.hazard_level) >= 2 else "require_replan"
            )
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary=severe_outcome
            )
            confidence = max(confidence, 0.8)
            operator_confirmation_required = True

    if action_plan and isinstance(action_plan.simulation_json, dict):
        simulation = action_plan.simulation_json
        sim_target_zone = str(simulation.get("target_zone", "")).strip()
        if not sim_target_zone and isinstance(action_plan.motion_plan_json, dict):
            sim_target_zone = str(
                (
                    action_plan.motion_plan_json.get("target_pose", {})
                    if isinstance(
                        action_plan.motion_plan_json.get("target_pose", {}), dict
                    )
                    else {}
                ).get("zone", "")
            ).strip()

        current_zone = related_object.zone if related_object else ""
        if sim_target_zone and current_zone and sim_target_zone != current_zone:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("simulation_target_zone_drift")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="require_replan"
            )
            confidence = max(confidence, 0.79)

        try:
            simulation_collision_risk = float(simulation.get("collision_risk", 0.0))
        except (TypeError, ValueError):
            simulation_collision_risk = 0.0
        if simulation_collision_risk >= SIMULATION_BLOCK_THRESHOLD_DEFAULT:
            freshness["simulation_assumptions_stable"] = False
            reasons.append("simulation_collision_risk_elevated")
            recommended_outcome = _merge_replan_outcomes(
                primary=recommended_outcome, secondary="require_replan"
            )
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

    action_plan = await _find_action_plan_for_execution(
        execution_id=execution.id, db=db
    )
    chains = await _find_chains_for_execution(execution_id=execution.id, db=db)

    signal = WorkspaceReplanSignal(
        execution_id=execution.id,
        action_plan_id=action_plan.id if action_plan else None,
        chain_id=chains[0].id if chains else None,
        signal_type=payload.signal_type,
        predicted_outcome=REPLAN_OUTCOME_MAP.get(
            payload.predicted_outcome, "continue_monitor"
        ),
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
    if predicted in {
        "pause_and_resimulate",
        "require_replan",
        "abort_chain",
    } and execution.status in {"dispatched", "accepted", "running"}:
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
                **(
                    action_plan.metadata_json
                    if isinstance(action_plan.metadata_json, dict)
                    else {}
                ),
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

    feedback = (
        execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    )
    predictive_signals = list(feedback.get("predictive_signals", []))
    predictive_signals.append(
        {
            "signal_id": signal.id,
            "signal_type": signal.signal_type,
            "predicted_outcome": signal.predicted_outcome,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "timestamp": signal.created_at.isoformat()
            if signal.created_at
            else datetime.now(timezone.utc).isoformat(),
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
async def get_workspace_replan_signal(
    signal_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
        raise HTTPException(
            status_code=404, detail="workspace target resolution not found"
        )

    signal: WorkspaceReplanSignal | None = None
    if payload.signal_id is not None:
        signal = await db.get(WorkspaceReplanSignal, payload.signal_id)
        if not signal:
            raise HTTPException(
                status_code=404, detail="workspace replan signal not found"
            )
        if (
            signal.action_plan_id not in {None, row.id}
            and signal.execution_id != row.execution_id
        ):
            raise HTTPException(
                status_code=422,
                detail="workspace replan signal does not belong to this plan",
            )
    else:
        signal = (
            (
                await db.execute(
                    select(WorkspaceReplanSignal)
                    .where(WorkspaceReplanSignal.action_plan_id == row.id)
                    .where(WorkspaceReplanSignal.status == "active")
                    .order_by(WorkspaceReplanSignal.id.desc())
                )
            )
            .scalars()
            .first()
        )

    freshness = await _evaluate_predictive_freshness(
        action_plan=row, target=target, db=db
    )
    selected_outcome = freshness["recommended_outcome"]
    if signal:
        selected_outcome = _merge_replan_outcomes(
            primary=selected_outcome, secondary=signal.predicted_outcome
        )
    if payload.force and selected_outcome == "abort_chain":
        selected_outcome = "require_replan"

    prior_snapshot = {
        "plan_id": row.id,
        "status": row.status,
        "planning_outcome": row.planning_outcome,
        "steps": row.steps_json if isinstance(row.steps_json, list) else [],
        "motion_plan": row.motion_plan_json
        if isinstance(row.motion_plan_json, dict)
        else {},
        "simulation_outcome": row.simulation_outcome,
        "simulation_status": row.simulation_status,
    }

    planning_outcome, steps, _ = _action_plan_policy(
        target=target, action_type=row.action_type
    )
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
        **(
            payload.motion_plan_overrides
            if isinstance(payload.motion_plan_overrides, dict)
            else {}
        ),
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
            "motion_plan": row.motion_plan_json
            if isinstance(row.motion_plan_json, dict)
            else {},
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
            feedback = (
                execution.feedback_json
                if isinstance(execution.feedback_json, dict)
                else {}
            )
            execution.feedback_json = {
                **feedback,
                "replan_required": selected_outcome
                in {"require_replan", "abort_chain"},
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
async def get_workspace_action_plan_replan_history(
    plan_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
    stmt = select(WorkspaceInterruptionEvent).order_by(
        WorkspaceInterruptionEvent.id.desc()
    )
    if execution_id is not None:
        stmt = stmt.where(WorkspaceInterruptionEvent.execution_id == execution_id)
    if status.strip():
        stmt = stmt.where(WorkspaceInterruptionEvent.status == status.strip())

    rows = (await db.execute(stmt)).scalars().all()[:limit]
    return {
        "interruptions": [_to_workspace_interruption_out(row) for row in rows],
    }


@router.get("/interruptions/{interruption_id}")
async def get_workspace_interruption(
    interruption_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
        raise HTTPException(
            status_code=422, detail="execution is not in a pausable state"
        )

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

    action_plan = await _find_action_plan_for_execution(
        execution_id=execution.id, db=db
    )
    if action_plan:
        action_plan.status = "paused"
        action_plan.execution_status = "paused"
        action_plan.planning_outcome = "plan_paused"
        action_plan.metadata_json = {
            **(
                action_plan.metadata_json
                if isinstance(action_plan.metadata_json, dict)
                else {}
            ),
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
        raise HTTPException(
            status_code=422, detail="execution is already in a terminal state"
        )

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

    action_plan = await _find_action_plan_for_execution(
        execution_id=execution.id, db=db
    )
    if action_plan:
        action_plan.status = "aborted"
        action_plan.execution_status = "stopped"
        action_plan.abort_status = "aborted"
        action_plan.abort_reason = execution.reason
        action_plan.planning_outcome = "plan_aborted"
        action_plan.metadata_json = {
            **(
                action_plan.metadata_json
                if isinstance(action_plan.metadata_json, dict)
                else {}
            ),
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
        raise HTTPException(
            status_code=422, detail="resume requires explicit safety_ack"
        )

    action_plan = await _find_action_plan_for_execution(
        execution_id=execution.id, db=db
    )
    if action_plan:
        if (
            action_plan.simulation_outcome != "plan_safe"
            or action_plan.simulation_status != "completed"
            or not action_plan.simulation_gate_passed
        ):
            raise HTTPException(
                status_code=422,
                detail="resume blocked: action plan simulation is no longer safe",
            )

    unresolved_blocking = (
        (
            await db.execute(
                select(WorkspaceInterruptionEvent)
                .where(WorkspaceInterruptionEvent.execution_id == execution.id)
                .where(WorkspaceInterruptionEvent.status == "active")
                .order_by(WorkspaceInterruptionEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    unresolved_blocking = [
        item
        for item in unresolved_blocking
        if _interruption_is_blocking(item.interruption_type)
    ]

    if unresolved_blocking and not payload.conditions_restored:
        raise HTTPException(
            status_code=422,
            detail="resume blocked: workspace changed and conditions are not restored",
        )

    active_replan_signals = await _active_replan_signals_for_execution(
        execution_id=execution.id, db=db
    )
    highest_replan_outcome = "continue_monitor"
    for signal in active_replan_signals:
        highest_replan_outcome = _merge_replan_outcomes(
            primary=highest_replan_outcome, secondary=signal.predicted_outcome
        )

    target = (
        await db.get(WorkspaceTargetResolution, action_plan.target_resolution_id)
        if action_plan
        else None
    )
    predictive_freshness = await _evaluate_predictive_freshness(
        action_plan=action_plan, target=target, db=db
    )
    highest_replan_outcome = _merge_replan_outcomes(
        primary=highest_replan_outcome,
        secondary=predictive_freshness.get("recommended_outcome", "continue_monitor"),
    )

    if (
        highest_replan_outcome
        in {"pause_and_resimulate", "require_replan", "abort_chain"}
        and not payload.conditions_restored
    ):
        raise HTTPException(
            status_code=422,
            detail=f"resume blocked: predictive drift requires {highest_replan_outcome}",
        )

    monitoring = await _get_or_create_monitoring_state(db)
    human_aware = _human_aware_state_from_monitoring(monitoring)
    _, resume_constraint_result = await evaluate_and_record_constraints(
        actor=payload.actor,
        source="objective44_resume_execution",
        goal={"goal_type": "resume_execution", "execution_id": execution.id},
        action_plan={
            "action_type": "resume_execution",
            "execution_id": execution.id,
            "is_physical": True,
            "managed_scope": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
            "zone": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
        },
        workspace_state={
            "human_in_workspace": human_aware.get("human_in_workspace", False),
            "human_near_target_zone": human_aware.get("human_near_target_zone", False),
            "human_near_motion_path": human_aware.get("human_near_motion_path", False),
            "shared_workspace_active": human_aware.get(
                "shared_workspace_active", False
            ),
            "target_confidence": float((target.confidence if target else 1.0) or 1.0),
            "map_freshness_seconds": 0,
            "managed_scope": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
            "zone": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
        },
        system_state={"throttle_blocked": False, "integrity_risk": False},
        policy_state={
            "min_target_confidence": EXECUTION_TARGET_CONFIDENCE_MINIMUM_DEFAULT,
            "map_freshness_limit_seconds": MONITORING_DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
            "unlawful_action": False,
        },
        metadata_json={
            "reason": payload.reason,
            "managed_scope": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
            "zone": (
                action_plan.target_zone if action_plan and action_plan.target_zone else (target.requested_zone if target else "global")
            ),
            **payload.metadata_json,
        },
        db=db,
    )
    resume_decision = str(resume_constraint_result.get("decision", "allowed"))
    if (
        resume_decision in {"requires_confirmation", "requires_replan", "blocked"}
        and not payload.conditions_restored
    ):
        raise HTTPException(
            status_code=422,
            detail=f"resume blocked by constraint engine: {resume_decision}",
        )

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
                **(
                    signal.metadata_json
                    if isinstance(signal.metadata_json, dict)
                    else {}
                ),
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
            **(
                action_plan.metadata_json
                if isinstance(action_plan.metadata_json, dict)
                else {}
            ),
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
