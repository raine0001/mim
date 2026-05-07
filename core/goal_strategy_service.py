from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import build_cross_domain_reasoning_context, to_cross_domain_reasoning_out
from core.execution_truth_governance_service import latest_execution_truth_governance_snapshot
from core.execution_truth_service import execution_truth_freshness, summarize_execution_truth_signal_types
from core.horizon_planning_service import create_horizon_plan
from core.improvement_service import generate_improvement_proposals as generate_improvement_proposals_for_strategy
from core.maintenance_service import run_environment_maintenance_cycle
from core.operator_commitment_outcome_service import latest_scope_commitment_outcome_profile
from core.operator_preference_convergence_service import (
    learned_preference_strategy_influence,
    latest_scope_learned_preference,
)
from core.proposal_arbitration_learning_service import workspace_proposal_arbitration_family_influence
from core.operator_resolution_service import commitment_downstream_effects, commitment_snapshot, latest_active_operator_resolution_commitment
from datetime import datetime, timedelta, timezone

from core.models import UserPreference, WorkspaceStrategyGoal, WorkspaceStrategyGoalReview


STRATEGY_GOAL_STATUSES = {
    "proposed",
    "active",
    "deferred",
    "superseded",
    "completed",
    "rejected",
}
STRATEGY_PERSISTENCE_STATES = {
    "session",
    "candidate",
    "persistent",
    "archived",
}
STRATEGY_REVIEW_STATUSES = {
    "unreviewed",
    "needs_review",
    "approved",
    "deferred",
    "archived",
}
STRATEGY_REVIEW_DECISIONS = {
    "carry_forward",
    "activate",
    "defer",
    "archive",
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _priority_label(score: float) -> str:
    if score >= 0.8:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.45:
        return "normal"
    return "low"


def _risk_baseline(strategy_type: str) -> float:
    risk_by_type = {
        "maintain_workspace_readiness": 0.25,
        "reduce_operator_interruption_load": 0.2,
        "stabilize_uncertain_zones_before_action": 0.35,
        "prioritize_development_improvements_affecting_active_workflows": 0.3,
    }
    return float(risk_by_type.get(strategy_type, 0.35))


def _operator_recommendations(strategy_type: str) -> list[str]:
    by_type = {
        "maintain_workspace_readiness": [
            "Review top workspace readiness checkpoint before direct-action execution windows.",
        ],
        "reduce_operator_interruption_load": [
            "Approve bundled status updates to lower conversational interruption overhead.",
        ],
        "stabilize_uncertain_zones_before_action": [
            "Confirm uncertain zones for pre-action rescan prioritization.",
        ],
        "prioritize_development_improvements_affecting_active_workflows": [
            "Review highest-friction development improvements before policy changes.",
        ],
    }
    return by_type.get(strategy_type, ["Review strategic goal before downstream execution."])


def _strategy_related_proposal_types(strategy_type: str) -> list[str]:
    mapping = {
        "maintain_workspace_readiness": [
            "rescan_zone",
            "verify_moved_object",
            "confirm_target_ready",
            "monitor_recheck_workspace",
            "monitor_search_adjacent_zone",
            "target_confirmed",
        ],
        "stabilize_uncertain_zones_before_action": [
            "rescan_zone",
            "verify_moved_object",
            "confirm_target_ready",
            "monitor_search_adjacent_zone",
        ],
    }
    proposal_types = mapping.get(str(strategy_type or "").strip(), [])
    return [item for item in proposal_types if str(item).strip()]


async def _proposal_arbitration_strategy_influence(
    *,
    strategy_type: str,
    related_zone: str,
    db: AsyncSession,
) -> dict:
    proposal_types = _strategy_related_proposal_types(strategy_type)
    influence = await workspace_proposal_arbitration_family_influence(
        proposal_types=proposal_types,
        related_zone=related_zone,
        db=db,
        max_abs_bias=0.12,
    )
    if not proposal_types or not isinstance(influence, dict):
        return {
            "strategy_weight": 0.0,
            "rationale": "",
            "related_zone": str(related_zone or "").strip() or "global",
            "proposal_types": [],
            "sample_count": 0,
            "learning": [],
            "applied": False,
        }
    learning_rows = influence.get("learning", []) if isinstance(influence.get("learning", []), list) else []
    sample_count = int(influence.get("sample_count", 0) or 0)
    strategy_weight = float(influence.get("aggregate_priority_bias", 0.0) or 0.0)
    applied = sample_count >= 2 and abs(strategy_weight) >= 0.01
    if abs(strategy_weight) < 1e-9:
        return {
            "strategy_weight": 0.0,
            "rationale": "",
            "related_zone": str(related_zone or "").strip() or "global",
            "proposal_types": proposal_types,
            "sample_count": sample_count,
            "learning": learning_rows,
            "applied": False,
        }

    direction = "boosted" if strategy_weight > 0 else "downweighted"
    rationale = (
        f"Proposal arbitration outcomes {direction} this strategy in zone "
        f"{str(related_zone or '').strip() or 'global'} across {sample_count} related proposal outcomes."
    )
    return {
        "strategy_weight": round(strategy_weight, 6),
        "rationale": rationale,
        "related_zone": str(influence.get("related_zone", related_zone) or "").strip() or "global",
        "proposal_types": influence.get("proposal_types", proposal_types),
        "sample_count": sample_count,
        "learning": learning_rows,
        "applied": applied,
    }


def _operator_resolution_strategy_influence(
    *,
    strategy_type: str,
    commitment: object | None,
) -> dict:
    if commitment is None:
        return {"strategy_weight": 0.0, "rationale": "", "decision_type": ""}

    effects = commitment_downstream_effects(commitment)
    decision_type = str(getattr(commitment, "decision_type", "") or "").strip()
    mode = str(effects.get("strategy_priority_mode", "") or "").strip()
    delta = float(effects.get("strategy_priority_delta", 0.0) or 0.0)
    weight = delta

    if decision_type in {"require_additional_evidence", "defer_action"}:
        if strategy_type in {"stabilize_uncertain_zones_before_action", "maintain_workspace_readiness"}:
            weight += 0.12
        else:
            weight -= 0.04

    if mode == "prefer_stabilization":
        if strategy_type in {"stabilize_uncertain_zones_before_action", "maintain_workspace_readiness"}:
            weight += 0.1
        else:
            weight -= 0.03
    elif mode == "defer_noncritical":
        if strategy_type in {"stabilize_uncertain_zones_before_action", "maintain_workspace_readiness"}:
            weight += 0.04
        else:
            weight -= 0.08

    weight = max(-0.2, min(0.2, weight))
    if abs(weight) < 1e-9:
        return {"strategy_weight": 0.0, "rationale": "", "decision_type": decision_type}

    direction = "boosted" if weight > 0 else "downweighted"
    rationale = f"Operator resolution commitment {decision_type or 'active'} {direction} this strategy for the managed scope."
    return {
        "strategy_weight": round(weight, 6),
        "rationale": rationale,
        "decision_type": decision_type,
    }


def _operator_resolution_outcome_strategy_influence(
    *,
    strategy_type: str,
    outcome: object | None,
) -> dict:
    if outcome is None:
        return {
            "strategy_weight": 0.0,
            "rationale": "",
            "outcome_status": "",
            "decision_type": "",
        }

    learning = (
        outcome.learning_signals_json
        if isinstance(getattr(outcome, "learning_signals_json", {}), dict)
        else {}
    )
    outcome_status = str(getattr(outcome, "outcome_status", "") or "").strip()
    decision_type = str(getattr(outcome, "decision_type", "") or "").strip()
    weight = float(learning.get("strategy_priority_delta", 0.0) or 0.0)

    if outcome_status in {"ineffective", "harmful", "abandoned"}:
        if strategy_type in {
            "stabilize_uncertain_zones_before_action",
            "maintain_workspace_readiness",
        } and decision_type in {"require_additional_evidence", "defer_action"}:
            weight -= 0.06
        if strategy_type == "prioritize_development_improvements_affecting_active_workflows":
            weight += 0.1
    elif outcome_status == "satisfied":
        if strategy_type in {
            "stabilize_uncertain_zones_before_action",
            "maintain_workspace_readiness",
        } and decision_type in {"require_additional_evidence", "defer_action"}:
            weight += 0.05

    weight = max(-0.2, min(0.2, weight))
    if abs(weight) < 1e-9:
        return {
            "strategy_weight": 0.0,
            "rationale": "",
            "outcome_status": outcome_status,
            "decision_type": decision_type,
        }

    direction = "boosted" if weight > 0 else "downweighted"
    rationale = (
        f"Recent commitment outcome {outcome_status or 'unknown'} {direction} this strategy"
        f" after {decision_type or 'operator guidance'} in the managed scope."
    )
    return {
        "strategy_weight": round(weight, 6),
        "rationale": rationale,
        "outcome_status": outcome_status,
        "decision_type": decision_type,
    }


def _execution_truth_summary(*, context: dict) -> dict:
    reasoning = (
        context.get("reasoning", {})
        if isinstance(context.get("reasoning", {}), dict)
        else {}
    )
    summary = (
        reasoning.get("execution_truth_influence", {})
        if isinstance(reasoning.get("execution_truth_influence", {}), dict)
        else {}
    )
    return _scoped_execution_truth_summary(context=context, summary=summary)


def _context_managed_scopes(*, context: dict) -> list[str]:
    metadata = (
        context.get("metadata_json", {})
        if isinstance(context.get("metadata_json", {}), dict)
        else {}
    )
    requested_scope = str(
        metadata.get("managed_scope", metadata.get("target_scope", ""))
    ).strip()
    if requested_scope:
        return [requested_scope]

    workspace = (
        context.get("workspace_state", {})
        if isinstance(context.get("workspace_state", {}), dict)
        else {}
    )
    zones = workspace.get("zones", []) if isinstance(workspace.get("zones", []), list) else []
    scopes: list[str] = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        scope = str(zone.get("zone", "")).strip()
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


def _scoped_execution_truth_summary(*, context: dict, summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    scopes = _context_managed_scopes(context=context)
    if not scopes:
        return summary

    recent_executions = (
        summary.get("recent_executions", [])
        if isinstance(summary.get("recent_executions", []), list)
        else []
    )
    filtered_recent_executions = [
        item
        for item in recent_executions
        if isinstance(item, dict)
        and any(
            str(scope_ref).strip() in scopes
            for scope_ref in (
                item.get("scope_refs", []) if isinstance(item.get("scope_refs", []), list) else []
            )
        )
    ]
    filtered_execution_ids = {
        _safe_int(item.get("execution_id", 0))
        for item in filtered_recent_executions
        if isinstance(item, dict)
    }
    filtered_capabilities = {
        str(item.get("capability_name", "")).strip()
        for item in filtered_recent_executions
        if isinstance(item, dict) and str(item.get("capability_name", "")).strip()
    }

    deviation_signals = (
        summary.get("deviation_signals", [])
        if isinstance(summary.get("deviation_signals", []), list)
        else []
    )
    filtered_deviation_signals = [
        item
        for item in deviation_signals
        if isinstance(item, dict)
        and (
            str(item.get("target_scope", "")).strip() in scopes
            or _safe_int(item.get("execution_id", 0)) in filtered_execution_ids
            or str(item.get("target_scope", "")).strip() in filtered_capabilities
        )
    ]

    if not filtered_recent_executions and not filtered_deviation_signals:
        return summary

    scoped_summary = {
        **summary,
        "execution_count": len(filtered_recent_executions),
        "deviation_signal_count": len(filtered_deviation_signals),
        "deviation_signals": filtered_deviation_signals,
        "recent_executions": filtered_recent_executions,
        "signal_types": sorted(
            {
                str(item.get("signal_type", "")).strip()
                for item in filtered_deviation_signals
                if isinstance(item, dict) and str(item.get("signal_type", "")).strip()
            }
        ),
        "managed_scope": scopes[0] if len(scopes) == 1 else ",".join(scopes),
    }
    scoped_summary["freshness"] = execution_truth_freshness(
        scoped_summary,
        decay_window_hours=24,
    )
    return scoped_summary


def _execution_truth_signal_types(*, summary: dict) -> list[str]:
    return summarize_execution_truth_signal_types(summary)


def _execution_truth_strategy_influence(*, strategy_type: str, summary: dict) -> dict:
    execution_count = _safe_int(summary.get("execution_count", 0))
    signal_count = _safe_int(summary.get("deviation_signal_count", 0))
    signal_types = _execution_truth_signal_types(summary=summary)
    freshness = execution_truth_freshness(summary, decay_window_hours=24)
    freshness_weight = float(freshness.get("freshness_weight", 0.0) or 0.0)
    if execution_count <= 0 or signal_count <= 0 or not signal_types:
        return {
            "execution_count": execution_count,
            "signal_count": signal_count,
            "signal_types": signal_types,
            "strategy_weight": 0.0,
            "freshness": freshness,
            "rationale": "",
        }

    per_strategy_weights = {
        "maintain_workspace_readiness": {
            "execution_slower_than_expected": 0.14,
            "retry_instability_detected": 0.18,
            "fallback_path_used": 0.16,
            "simulation_reality_mismatch": 0.18,
            "environment_shift_during_execution": 0.18,
            "base_pressure": 0.12,
        },
        "reduce_operator_interruption_load": {
            "execution_slower_than_expected": 0.03,
            "retry_instability_detected": 0.04,
            "fallback_path_used": 0.04,
            "simulation_reality_mismatch": 0.05,
            "environment_shift_during_execution": 0.05,
            "base_pressure": 0.03,
        },
        "stabilize_uncertain_zones_before_action": {
            "execution_slower_than_expected": 0.12,
            "retry_instability_detected": 0.18,
            "fallback_path_used": 0.14,
            "simulation_reality_mismatch": 0.24,
            "environment_shift_during_execution": 0.24,
            "base_pressure": 0.15,
        },
        "prioritize_development_improvements_affecting_active_workflows": {
            "execution_slower_than_expected": 0.08,
            "retry_instability_detected": 0.14,
            "fallback_path_used": 0.16,
            "simulation_reality_mismatch": 0.2,
            "environment_shift_during_execution": 0.16,
            "base_pressure": 0.08,
        },
    }
    weights = per_strategy_weights.get(strategy_type, {})
    weight = float(weights.get("base_pressure", 0.05)) * _bounded(signal_count / 5.0)
    for signal_type in signal_types:
        weight += float(weights.get(signal_type, 0.0))
    weight = _bounded(weight * freshness_weight)

    rationale_parts: list[str] = []
    signal_set = set(signal_types)
    if signal_set.intersection({"retry_instability_detected", "fallback_path_used"}):
        rationale_parts.append(
            "Retry and fallback pressure lower confidence in aggressive execution strategies."
        )
    if signal_set.intersection({"simulation_reality_mismatch", "environment_shift_during_execution"}):
        rationale_parts.append(
            "Runtime mismatch indicates environment assumptions should be reconfirmed before committing strategy weight."
        )
    if "execution_slower_than_expected" in signal_set:
        rationale_parts.append(
            "Latency drift suggests current readiness assumptions are too optimistic."
        )

    return {
        "execution_count": execution_count,
        "signal_count": signal_count,
        "signal_types": signal_types,
        "strategy_weight": round(weight, 6),
        "freshness": freshness,
        "rationale": " ".join(rationale_parts).strip(),
    }


def _execution_truth_governance_strategy_influence(*, strategy_type: str, governance: dict) -> dict:
    decision = str(governance.get("governance_decision", "monitor_only") or "monitor_only").strip()
    downstream_actions = (
        governance.get("downstream_actions", {})
        if isinstance(governance.get("downstream_actions", {}), dict)
        else {}
    )
    base_delta = _bounded(float(downstream_actions.get("strategy_weight_delta", 0.0) or 0.0))
    if decision == "monitor_only" or base_delta <= 0.0:
        return {
            "governance_decision": decision,
            "strategy_weight": 0.0,
            "rationale": "",
        }

    per_strategy_multiplier = {
        "maintain_workspace_readiness": 1.0,
        "stabilize_uncertain_zones_before_action": 1.0,
        "prioritize_development_improvements_affecting_active_workflows": 0.9,
        "reduce_operator_interruption_load": 0.55,
    }
    weight = _bounded(base_delta * float(per_strategy_multiplier.get(strategy_type, 0.7)))
    rationale = str(governance.get("governance_reason", "") or "").strip()
    return {
        "governance_decision": decision,
        "strategy_weight": round(weight, 6),
        "rationale": rationale,
    }


async def _operator_preference_weight(*, strategy_type: str, db: AsyncSession) -> float:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.in_([
                f"strategy_priority:{strategy_type}",
                "strategy_priority:default",
            ]))
            .order_by(UserPreference.last_updated.desc())
            .limit(10)
        )
    ).scalars().all()

    for row in rows:
        value = row.value
        if isinstance(value, (int, float)):
            return _bounded(float(value))
        if isinstance(value, dict):
            for key in ["weight", "priority_weight", "value"]:
                if isinstance(value.get(key), (int, float)):
                    return _bounded(float(value.get(key)))
    return 0.5


def _domains_from_context(*, context: dict) -> dict[str, int]:
    workspace = context.get("workspace_state", {}) if isinstance(context.get("workspace_state", {}), dict) else {}
    communication = context.get("communication_state", {}) if isinstance(context.get("communication_state", {}), dict) else {}
    external = context.get("external_information", {}) if isinstance(context.get("external_information", {}), dict) else {}
    development = context.get("development_state", {}) if isinstance(context.get("development_state", {}), dict) else {}
    self_improvement = context.get("self_improvement_state", {}) if isinstance(context.get("self_improvement_state", {}), dict) else {}

    return {
        "workspace_state": _safe_int(workspace.get("observation_count", 0)),
        "communication": _safe_int(communication.get("input_event_count", 0)) + _safe_int(communication.get("output_event_count", 0)),
        "external_information": _safe_int(external.get("external_item_count", 0)),
        "development": _safe_int(development.get("pattern_count", 0)),
        "self_improvement": _safe_int(self_improvement.get("backlog_item_count", 0)),
    }


def _strategy_candidates(*, context: dict) -> list[dict]:
    domains = _domains_from_context(context=context)
    workspace = domains["workspace_state"]
    communication = domains["communication"]
    external = domains["external_information"]
    development = domains["development"]
    self_improvement = domains["self_improvement"]
    execution_truth = _execution_truth_summary(context=context)
    execution_truth_signal_count = _safe_int(
        execution_truth.get("deviation_signal_count", 0)
    )
    execution_truth_signal_types = _execution_truth_signal_types(summary=execution_truth)

    candidates: list[dict] = []

    candidates.append(
        {
            "strategy_type": "maintain_workspace_readiness",
            "success_criteria": "Critical workspace zones remain observation-fresh and action-ready over the next planning horizon.",
            "why_formed": "Workspace activity and communication demand indicate readiness should be preserved before downstream execution.",
            "supporting_evidence": {
                "workspace_observation_count": workspace,
                "communication_signal_count": communication,
                "external_signal_count": external,
                "execution_truth_execution_count": _safe_int(
                    execution_truth.get("execution_count", 0)
                ),
                "execution_truth_signal_count": execution_truth_signal_count,
                "execution_truth_signal_types": execution_truth_signal_types,
            },
            "urgency": _bounded(0.4 + (communication / 60.0) + (workspace / 200.0)),
            "expected_impact": _bounded(0.55 + (workspace / 250.0)),
            "risk": _risk_baseline("maintain_workspace_readiness"),
        }
    )

    if communication > 0 or self_improvement > 0:
        candidates.append(
            {
                "strategy_type": "reduce_operator_interruption_load",
                "success_criteria": "Operator-facing interrupts are reduced while maintaining execution quality and situational awareness.",
                "why_formed": "Communication demand and backlog pressure indicate interruption load should be strategically reduced.",
                "supporting_evidence": {
                    "communication_signal_count": communication,
                    "self_improvement_backlog_count": self_improvement,
                    "execution_truth_execution_count": _safe_int(
                        execution_truth.get("execution_count", 0)
                    ),
                    "execution_truth_signal_count": execution_truth_signal_count,
                    "execution_truth_signal_types": execution_truth_signal_types,
                },
                "urgency": _bounded(0.35 + (communication / 40.0)),
                "expected_impact": _bounded(0.5 + (self_improvement / 80.0)),
                "risk": _risk_baseline("reduce_operator_interruption_load"),
            }
        )

    if workspace > 0:
        candidates.append(
            {
                "strategy_type": "stabilize_uncertain_zones_before_action",
                "success_criteria": "High-uncertainty zones receive stabilization scans before action plans that depend on them.",
                "why_formed": "Workspace state indicates zone uncertainty should be stabilized before downstream physical decisions.",
                "supporting_evidence": {
                    "workspace_observation_count": workspace,
                    "development_pattern_count": development,
                    "execution_truth_execution_count": _safe_int(
                        execution_truth.get("execution_count", 0)
                    ),
                    "execution_truth_signal_count": execution_truth_signal_count,
                    "execution_truth_signal_types": execution_truth_signal_types,
                },
                "urgency": _bounded(0.3 + (workspace / 150.0)),
                "expected_impact": _bounded(0.45 + (workspace / 220.0)),
                "risk": _risk_baseline("stabilize_uncertain_zones_before_action"),
            }
        )

    if development > 0 or self_improvement > 0:
        candidates.append(
            {
                "strategy_type": "prioritize_development_improvements_affecting_active_workflows",
                "success_criteria": "High-friction development patterns tied to active workflows are translated into prioritized improvement actions.",
                "why_formed": "Development patterns and improvement backlog indicate strategic improvement prioritization should guide near-term planning.",
                "supporting_evidence": {
                    "development_pattern_count": development,
                    "self_improvement_backlog_count": self_improvement,
                    "execution_truth_execution_count": _safe_int(
                        execution_truth.get("execution_count", 0)
                    ),
                    "execution_truth_signal_count": execution_truth_signal_count,
                    "execution_truth_signal_types": execution_truth_signal_types,
                },
                "urgency": _bounded(0.35 + (development / 30.0)),
                "expected_impact": _bounded(0.5 + (self_improvement / 70.0)),
                "risk": _risk_baseline("prioritize_development_improvements_affecting_active_workflows"),
            }
        )

    return candidates


def _horizon_goal_candidates_for_strategy(*, goal: WorkspaceStrategyGoal) -> list[dict]:
    title = str(goal.strategy_type or "strategy_goal").replace("_", " ").strip().title()
    return [
        {
            "goal_key": f"strategy:{goal.id}:primary",
            "title": title,
            "priority": str(goal.priority or "normal"),
            "goal_type": "strategy_goal",
            "dependencies": [],
            "estimated_steps": 3,
            "expected_value": _bounded(float(goal.priority_score or 0.0) + 0.1),
            "urgency": _bounded(float(goal.priority_score or 0.0)),
            "requires_fresh_map": True,
            "requires_high_confidence": False,
            "is_physical": False,
            "metadata_json": {
                "strategy_goal_id": int(goal.id),
                "strategy_type": goal.strategy_type,
            },
        },
        {
            "goal_key": f"strategy:{goal.id}:support",
            "title": "Collect supporting context",
            "priority": "normal",
            "goal_type": "supporting_context",
            "dependencies": [f"strategy:{goal.id}:primary"],
            "estimated_steps": 2,
            "expected_value": 0.5,
            "urgency": 0.5,
            "requires_fresh_map": False,
            "requires_high_confidence": False,
            "is_physical": False,
            "metadata_json": {
                "strategy_goal_id": int(goal.id),
            },
        },
    ]


async def _apply_downstream_bridge(
    *,
    goal: WorkspaceStrategyGoal,
    actor: str,
    source: str,
    lookback_hours: int,
    generate_horizon_plans: bool,
    generate_improvement_proposals: bool,
    generate_maintenance_cycles: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceStrategyGoal:
    linked_horizon_plan_ids: list[int] = []
    linked_improvement_proposal_ids: list[int] = []
    linked_maintenance_run_ids: list[int] = []

    if generate_horizon_plans:
        plan, _ = await create_horizon_plan(
            actor=actor,
            source=f"{source}:strategy_bridge",
            planning_horizon_minutes=120,
            goal_candidates=_horizon_goal_candidates_for_strategy(goal=goal),
            expected_future_constraints=[],
            priority_policy={
                "map_freshness_limit_seconds": 900,
                "min_target_confidence": 0.75,
            },
            map_freshness_seconds=240,
            object_confidence=0.9,
            human_aware_state={
                "human_in_workspace": False,
                "shared_workspace_active": False,
            },
            operator_preferences={},
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "strategy_goal_id": int(goal.id),
                "strategy_type": goal.strategy_type,
                "objective57_strategy_bridge": True,
            },
            db=db,
        )
        linked_horizon_plan_ids.append(int(plan.id))

    if generate_improvement_proposals:
        proposals = await generate_improvement_proposals_for_strategy(
            actor=actor,
            source=f"{source}:strategy_bridge",
            lookback_hours=max(24, int(lookback_hours)),
            min_occurrence_count=2,
            max_proposals=3,
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "strategy_goal_id": int(goal.id),
                "strategy_type": goal.strategy_type,
                "objective57_strategy_bridge": True,
            },
            db=db,
        )
        linked_improvement_proposal_ids = [int(item.id) for item in proposals[:3]]

    if generate_maintenance_cycles:
        run, _, _, _ = await run_environment_maintenance_cycle(
            actor=actor,
            source=f"{source}:strategy_bridge",
            stale_after_seconds=900,
            max_strategies=2,
            max_actions=1,
            auto_execute=False,
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "strategy_goal_id": int(goal.id),
                "strategy_type": goal.strategy_type,
                "objective57_strategy_bridge": True,
            },
            db=db,
        )
        linked_maintenance_run_ids.append(int(run.id))

    goal.linked_horizon_plan_ids_json = linked_horizon_plan_ids
    goal.linked_improvement_proposal_ids_json = linked_improvement_proposal_ids
    goal.linked_maintenance_run_ids_json = linked_maintenance_run_ids
    goal.operator_recommendations_json = _operator_recommendations(goal.strategy_type)
    goal.reasoning_json = {
        **(goal.reasoning_json if isinstance(goal.reasoning_json, dict) else {}),
        "downstream_influence": {
            "horizon_plan_ids": linked_horizon_plan_ids,
            "improvement_proposal_ids": linked_improvement_proposal_ids,
            "maintenance_run_ids": linked_maintenance_run_ids,
            "operator_recommendations": goal.operator_recommendations_json,
        },
    }
    await db.flush()
    return goal


async def build_strategy_goals(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_items_per_domain: int,
    max_goals: int,
    min_context_confidence: float,
    min_domains_required: int,
    min_cross_domain_links: int,
    generate_horizon_plans: bool,
    generate_improvement_proposals: bool,
    generate_maintenance_cycles: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[dict, list[WorkspaceStrategyGoal], dict]:
    context_row = await build_cross_domain_reasoning_context(
        actor=actor,
        source=source,
        lookback_hours=lookback_hours,
        max_items_per_domain=max_items_per_domain,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective57_strategy_context": True,
        },
        db=db,
    )
    context = to_cross_domain_reasoning_out(context_row)
    domain_counts = _domains_from_context(context=context)
    domains_present = [name for name, count in domain_counts.items() if count > 0]
    links = context.get("reasoning", {}).get("cross_domain_links", []) if isinstance(context.get("reasoning", {}), dict) else []
    links_count = len(links) if isinstance(links, list) else 0
    confidence = float(context.get("confidence", 0.0) or 0.0)

    gating_reasons: list[str] = []
    if confidence < float(min_context_confidence):
        gating_reasons.append(
            f"context_confidence_below_threshold:{confidence:.3f}<{float(min_context_confidence):.3f}"
        )
    if len(domains_present) < int(min_domains_required):
        gating_reasons.append(
            f"insufficient_domains:{len(domains_present)}<{int(min_domains_required)}"
        )
    if links_count < int(min_cross_domain_links):
        gating_reasons.append(
            f"insufficient_cross_domain_links:{links_count}<{int(min_cross_domain_links)}"
        )

    if gating_reasons:
        return context, [], {
            "generated": 0,
            "domain_counts": domain_counts,
            "domains_present": domains_present,
            "cross_domain_links": links_count,
            "context_confidence": confidence,
            "gating_reasons": gating_reasons,
        }

    candidates = _strategy_candidates(context=context)
    ranked_candidates: list[dict] = []
    developmental_friction = _bounded(float(domain_counts.get("development", 0)) / 8.0)
    execution_truth_summary = _execution_truth_summary(context=context)
    managed_scopes = _context_managed_scopes(context=context)
    governance_scope = managed_scopes[0] if managed_scopes else "global"
    execution_truth_governance = await latest_execution_truth_governance_snapshot(
        managed_scope=governance_scope,
        db=db,
    )
    operator_resolution_commitment = await latest_active_operator_resolution_commitment(
        scope=governance_scope,
        db=db,
    )
    operator_resolution_outcome = await latest_scope_commitment_outcome_profile(
        managed_scope=governance_scope,
        db=db,
    )
    learned_operator_preference = await latest_scope_learned_preference(
        managed_scope=governance_scope,
        db=db,
        operator_commitment=operator_resolution_commitment,
    )

    for candidate in candidates:
        strategy_type = str(candidate.get("strategy_type", "strategy_goal"))
        urgency = _bounded(float(candidate.get("urgency", 0.5) or 0.5))
        expected_impact = _bounded(float(candidate.get("expected_impact", 0.5) or 0.5))
        risk = _bounded(float(candidate.get("risk", _risk_baseline(strategy_type)) or _risk_baseline(strategy_type)))
        operator_pref = await _operator_preference_weight(strategy_type=strategy_type, db=db)
        execution_truth_influence = _execution_truth_strategy_influence(
            strategy_type=strategy_type,
            summary=execution_truth_summary,
        )
        governance_influence = _execution_truth_governance_strategy_influence(
            strategy_type=strategy_type,
            governance=execution_truth_governance,
        )
        operator_resolution_influence = _operator_resolution_strategy_influence(
            strategy_type=strategy_type,
            commitment=operator_resolution_commitment,
        )
        operator_resolution_outcome_influence = _operator_resolution_outcome_strategy_influence(
            strategy_type=strategy_type,
            outcome=operator_resolution_outcome,
        )
        learned_preference_influence = learned_preference_strategy_influence(
            strategy_type=strategy_type,
            preference=learned_operator_preference,
        )
        proposal_arbitration_influence = await _proposal_arbitration_strategy_influence(
            strategy_type=strategy_type,
            related_zone=governance_scope,
            db=db,
        )

        score = _bounded(
            (urgency * 0.25)
            + (confidence * 0.2)
            + (expected_impact * 0.2)
            + ((1.0 - risk) * 0.1)
            + (operator_pref * 0.1)
            + (developmental_friction * 0.15)
            + (float(execution_truth_influence.get("strategy_weight", 0.0) or 0.0) * 0.15)
            + (float(governance_influence.get("strategy_weight", 0.0) or 0.0) * 0.15)
            + float(operator_resolution_influence.get("strategy_weight", 0.0) or 0.0)
            + float(operator_resolution_outcome_influence.get("strategy_weight", 0.0) or 0.0)
            + float(learned_preference_influence.get("strategy_weight", 0.0) or 0.0)
            + float(proposal_arbitration_influence.get("strategy_weight", 0.0) or 0.0)
        )

        reasoning_summary = str(candidate.get("why_formed", ""))
        rationale = str(execution_truth_influence.get("rationale", "")).strip()
        if rationale:
            reasoning_summary = f"{reasoning_summary} {rationale}".strip()
        governance_rationale = str(governance_influence.get("rationale", "")).strip()
        if governance_rationale:
            reasoning_summary = f"{reasoning_summary} {governance_rationale}".strip()
        operator_rationale = str(operator_resolution_influence.get("rationale", "")).strip()
        if operator_rationale:
            reasoning_summary = f"{reasoning_summary} {operator_rationale}".strip()
        operator_outcome_rationale = str(
            operator_resolution_outcome_influence.get("rationale", "")
        ).strip()
        if operator_outcome_rationale:
            reasoning_summary = f"{reasoning_summary} {operator_outcome_rationale}".strip()
        learned_preference_rationale = str(
            learned_preference_influence.get("rationale", "")
        ).strip()
        if learned_preference_rationale:
            reasoning_summary = f"{reasoning_summary} {learned_preference_rationale}".strip()
        proposal_arbitration_rationale = str(
            proposal_arbitration_influence.get("rationale", "")
        ).strip()
        if proposal_arbitration_rationale:
            reasoning_summary = f"{reasoning_summary} {proposal_arbitration_rationale}".strip()

        ranked_candidates.append(
            {
                **candidate,
                "priority_score": score,
                "priority": _priority_label(score),
                "reasoning_summary": reasoning_summary,
                "ranking_factors": {
                    "urgency": urgency,
                    "confidence": confidence,
                    "expected_impact": expected_impact,
                    "risk": risk,
                    "operator_preference_influence": operator_pref,
                    "developmental_friction_patterns": developmental_friction,
                    "execution_truth_execution_count": int(
                        execution_truth_influence.get("execution_count", 0) or 0
                    ),
                    "execution_truth_signal_count": int(
                        execution_truth_influence.get("signal_count", 0) or 0
                    ),
                    "execution_truth_signal_types": execution_truth_influence.get(
                        "signal_types", []
                    ),
                    "execution_truth_strategy_weight": float(
                        execution_truth_influence.get("strategy_weight", 0.0) or 0.0
                    ),
                    "execution_truth_freshness": execution_truth_influence.get(
                        "freshness", {}
                    ),
                    "execution_truth_governance_decision": str(
                        governance_influence.get("governance_decision", "monitor_only")
                    ),
                    "execution_truth_governance_weight": float(
                        governance_influence.get("strategy_weight", 0.0) or 0.0
                    ),
                    "operator_resolution_strategy_weight": float(
                        operator_resolution_influence.get("strategy_weight", 0.0) or 0.0
                    ),
                    "operator_resolution_decision_type": str(
                        operator_resolution_influence.get("decision_type", "") or ""
                    ),
                    "operator_resolution_outcome_weight": float(
                        operator_resolution_outcome_influence.get("strategy_weight", 0.0)
                        or 0.0
                    ),
                    "operator_resolution_outcome_status": str(
                        operator_resolution_outcome_influence.get("outcome_status", "")
                        or ""
                    ),
                    "operator_learned_preference_weight": float(
                        learned_preference_influence.get("strategy_weight", 0.0) or 0.0
                    ),
                    "operator_learned_preference_key": str(
                        learned_preference_influence.get("preference_key", "") or ""
                    ),
                    "proposal_arbitration_strategy_weight": float(
                        proposal_arbitration_influence.get("strategy_weight", 0.0) or 0.0
                    ),
                    "proposal_arbitration_sample_count": int(
                        proposal_arbitration_influence.get("sample_count", 0) or 0
                    ),
                    "proposal_arbitration_related_zone": str(
                        proposal_arbitration_influence.get("related_zone", "") or ""
                    ),
                    "proposal_arbitration_proposal_types": proposal_arbitration_influence.get(
                        "proposal_types", []
                    ),
                },
                "execution_truth_influence": execution_truth_influence,
                "execution_truth_governance": execution_truth_governance,
                "operator_resolution_influence": operator_resolution_influence,
                "operator_resolution_outcome_influence": operator_resolution_outcome_influence,
                "operator_learned_preference_influence": learned_preference_influence,
                "proposal_arbitration_influence": proposal_arbitration_influence,
            }
        )

    ranked_candidates.sort(
        key=lambda item: (
            -float(item.get("priority_score", 0.0) or 0.0),
            str(item.get("strategy_type", "")),
        )
    )

    created: list[WorkspaceStrategyGoal] = []
    for candidate in ranked_candidates[: max(1, min(20, int(max_goals)))]:
        row = WorkspaceStrategyGoal(
            source=source,
            actor=actor,
            strategy_type=str(candidate.get("strategy_type", "strategy_goal")),
            origin_context_id=int(context_row.id),
            priority=str(candidate.get("priority", "normal")),
            priority_score=float(candidate.get("priority_score", 0.0) or 0.0),
            success_criteria=str(candidate.get("success_criteria", "")),
            status="proposed",
            evidence_summary=str(candidate.get("reasoning_summary", "")),
            supporting_evidence_json=candidate.get("supporting_evidence", {}) if isinstance(candidate.get("supporting_evidence", {}), dict) else {},
            contributing_domains_json=domains_present,
            ranking_factors_json=candidate.get("ranking_factors", {}) if isinstance(candidate.get("ranking_factors", {}), dict) else {},
            reasoning_summary=str(candidate.get("reasoning_summary", "")),
            reasoning_json={
                "domains_contributed": domains_present,
                "cross_domain_links": links if isinstance(links, list) else [],
                "origin_context_confidence": confidence,
                "execution_truth_influence": {
                    **(
                        execution_truth_summary
                        if isinstance(execution_truth_summary, dict)
                        else {}
                    ),
                    "signal_types": _execution_truth_signal_types(
                        summary=execution_truth_summary
                    ),
                    "strategy_weight": float(
                        candidate.get("execution_truth_influence", {}).get(
                            "strategy_weight", 0.0
                        )
                        if isinstance(candidate.get("execution_truth_influence", {}), dict)
                        else 0.0
                    ),
                    "strategy_rationale": str(
                        candidate.get("execution_truth_influence", {}).get(
                            "rationale", ""
                        )
                        if isinstance(candidate.get("execution_truth_influence", {}), dict)
                        else ""
                    ).strip(),
                },
                "execution_truth_governance": (
                    candidate.get("execution_truth_governance", {})
                    if isinstance(candidate.get("execution_truth_governance", {}), dict)
                    else {}
                ),
                "operator_resolution_commitment": commitment_snapshot(
                    operator_resolution_commitment
                ),
                "operator_resolution_influence": (
                    candidate.get("operator_resolution_influence", {})
                    if isinstance(candidate.get("operator_resolution_influence", {}), dict)
                    else {}
                ),
                "operator_resolution_outcome": (
                    candidate.get("operator_resolution_outcome_influence", {})
                    if isinstance(
                        candidate.get("operator_resolution_outcome_influence", {}), dict
                    )
                    else {}
                ),
                "operator_learned_preference": (
                    candidate.get("operator_learned_preference_influence", {})
                    if isinstance(
                        candidate.get("operator_learned_preference_influence", {}), dict
                    )
                    else {}
                ),
                "proposal_arbitration_learning": (
                    candidate.get("proposal_arbitration_influence", {})
                    if isinstance(candidate.get("proposal_arbitration_influence", {}), dict)
                    else {}
                ),
            },
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective57_strategy_goal": True,
                "objective80_execution_truth_strategy": bool(
                    _safe_int(execution_truth_summary.get("deviation_signal_count", 0))
                ),
            },
        )
        db.add(row)
        await db.flush()

        row = await _apply_downstream_bridge(
            goal=row,
            actor=actor,
            source=source,
            lookback_hours=lookback_hours,
            generate_horizon_plans=generate_horizon_plans,
            generate_improvement_proposals=generate_improvement_proposals,
            generate_maintenance_cycles=generate_maintenance_cycles,
            metadata_json=metadata_json,
            db=db,
        )
        created.append(row)

    return context, created, {
        "generated": len(created),
        "domain_counts": domain_counts,
        "domains_present": domains_present,
        "cross_domain_links": links_count,
        "context_confidence": confidence,
        "gating_reasons": [],
    }


async def list_strategy_goals(
    *,
    db: AsyncSession,
    status: str = "",
    strategy_type: str = "",
    limit: int = 50,
) -> list[WorkspaceStrategyGoal]:
    rows = (
        await db.execute(
            select(WorkspaceStrategyGoal)
            .order_by(WorkspaceStrategyGoal.priority_score.desc(), WorkspaceStrategyGoal.id.desc())
        )
    ).scalars().all()
    filtered = rows
    if status:
        requested = status.strip().lower()
        if requested in STRATEGY_GOAL_STATUSES:
            filtered = [item for item in filtered if str(item.status or "").strip().lower() == requested]
    if strategy_type:
        requested_type = strategy_type.strip().lower()
        filtered = [item for item in filtered if str(item.strategy_type or "").strip().lower() == requested_type]
    return filtered[: max(1, min(500, int(limit)))]


async def get_strategy_goal(*, strategy_goal_id: int, db: AsyncSession) -> WorkspaceStrategyGoal | None:
    return (
        await db.execute(
            select(WorkspaceStrategyGoal).where(WorkspaceStrategyGoal.id == strategy_goal_id)
        )
    ).scalars().first()


async def recompute_strategy_goal_persistence(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_support_count: int,
    min_persistence_confidence: float,
    limit: int,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[list[WorkspaceStrategyGoal], dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    rows = (
        await db.execute(
            select(WorkspaceStrategyGoal)
            .where(WorkspaceStrategyGoal.created_at >= since)
            .order_by(WorkspaceStrategyGoal.created_at.desc(), WorkspaceStrategyGoal.id.desc())
            .limit(max(1, min(1000, int(limit))))
        )
    ).scalars().all()

    by_type: dict[str, list[WorkspaceStrategyGoal]] = {}
    for row in rows:
        key = str(row.strategy_type or "").strip() or "unknown"
        by_type.setdefault(key, []).append(row)

    updated: list[WorkspaceStrategyGoal] = []
    threshold_count = max(1, int(min_support_count))
    threshold_confidence = _bounded(float(min_persistence_confidence))

    for strategy_type, items in by_type.items():
        support_count = len(items)
        avg_priority = _bounded(sum(float(item.priority_score or 0.0) for item in items) / float(max(1, support_count)))
        persistence_confidence = _bounded((avg_priority * 0.6) + (_bounded(float(support_count) / 5.0) * 0.4))
        is_persistent = support_count >= threshold_count and persistence_confidence >= threshold_confidence

        for item in items:
            prev_state = str(item.persistence_state or "session")
            next_state = "persistent" if is_persistent else "session"
            next_review = "needs_review" if is_persistent else "unreviewed"
            if str(item.review_status or "") in {"approved", "deferred", "archived"}:
                next_review = str(item.review_status)

            item.persistence_state = next_state
            item.review_status = next_review
            item.persistence_confidence = persistence_confidence
            item.surviving_sessions = support_count
            if prev_state == "persistent" and next_state == "persistent":
                item.carry_forward_count = int(item.carry_forward_count or 0) + 1

            item.reasoning_json = {
                **(item.reasoning_json if isinstance(item.reasoning_json, dict) else {}),
                "objective59_persistence": {
                    "support_count": support_count,
                    "threshold_count": threshold_count,
                    "avg_priority": avg_priority,
                    "persistence_confidence": persistence_confidence,
                    "threshold_confidence": threshold_confidence,
                    "persistent": is_persistent,
                    "evaluated_by": actor,
                    "source": source,
                },
            }
            item.metadata_json = {
                **(item.metadata_json if isinstance(item.metadata_json, dict) else {}),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective59_goal_persistence": True,
            }
            updated.append(item)

    return updated, {
        "evaluated": len(rows),
        "updated": len(updated),
        "types": len(by_type),
        "threshold_count": threshold_count,
        "threshold_confidence": threshold_confidence,
    }


async def review_strategy_goal(
    *,
    strategy_goal_id: int,
    actor: str,
    decision: str,
    reason: str,
    evidence_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceStrategyGoal | None, WorkspaceStrategyGoalReview | None]:
    goal = await get_strategy_goal(strategy_goal_id=strategy_goal_id, db=db)
    if not goal:
        return None, None

    normalized_decision = str(decision or "carry_forward").strip().lower()
    if normalized_decision not in STRATEGY_REVIEW_DECISIONS:
        normalized_decision = "carry_forward"

    if normalized_decision == "archive":
        goal.persistence_state = "archived"
        goal.review_status = "archived"
        goal.status = "superseded"
    elif normalized_decision == "defer":
        goal.persistence_state = "persistent"
        goal.review_status = "deferred"
        goal.status = "deferred"
    elif normalized_decision == "activate":
        goal.persistence_state = "persistent"
        goal.review_status = "approved"
        goal.status = "active"
    else:
        goal.persistence_state = "persistent"
        goal.review_status = "approved"

    goal.last_reviewed_at = datetime.now(timezone.utc)
    goal.review_notes = str(reason or "")
    if goal.persistence_state == "persistent":
        goal.carry_forward_count = int(goal.carry_forward_count or 0) + 1

    review = WorkspaceStrategyGoalReview(
        strategy_goal_id=int(goal.id),
        actor=actor,
        decision=normalized_decision,
        reason=str(reason or ""),
        resulting_persistence_state=goal.persistence_state,
        resulting_review_status=goal.review_status,
        evidence_json=evidence_json if isinstance(evidence_json, dict) else {},
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective59_goal_review": True,
        },
    )
    db.add(review)
    await db.flush()
    return goal, review


async def list_strategy_goal_reviews(
    *,
    strategy_goal_id: int,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceStrategyGoalReview]:
    rows = (
        await db.execute(
            select(WorkspaceStrategyGoalReview)
            .where(WorkspaceStrategyGoalReview.strategy_goal_id == strategy_goal_id)
            .order_by(WorkspaceStrategyGoalReview.id.desc())
            .limit(max(1, min(500, int(limit))))
        )
    ).scalars().all()
    return rows


def to_strategy_goal_review_out(row: WorkspaceStrategyGoalReview) -> dict:
    return {
        "review_id": int(row.id),
        "strategy_goal_id": int(row.strategy_goal_id),
        "actor": row.actor,
        "decision": row.decision,
        "reason": row.reason,
        "resulting_persistence_state": row.resulting_persistence_state,
        "resulting_review_status": row.resulting_review_status,
        "evidence_json": row.evidence_json if isinstance(row.evidence_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_strategy_goal_out(row: WorkspaceStrategyGoal) -> dict:
    return {
        "strategy_goal_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "strategy_type": row.strategy_type,
        "origin_context_id": int(row.origin_context_id) if row.origin_context_id is not None else None,
        "priority": row.priority,
        "priority_score": float(row.priority_score or 0.0),
        "success_criteria": row.success_criteria,
        "status": row.status,
        "evidence_summary": row.evidence_summary,
        "supporting_evidence": row.supporting_evidence_json if isinstance(row.supporting_evidence_json, dict) else {},
        "contributing_domains": row.contributing_domains_json if isinstance(row.contributing_domains_json, list) else [],
        "ranking_factors": row.ranking_factors_json if isinstance(row.ranking_factors_json, dict) else {},
        "reasoning_summary": row.reasoning_summary,
        "reasoning": row.reasoning_json if isinstance(row.reasoning_json, dict) else {},
        "operator_learned_preference_influence": (
            row.reasoning_json.get("operator_learned_preference", {})
            if isinstance(row.reasoning_json, dict)
            and isinstance(row.reasoning_json.get("operator_learned_preference", {}), dict)
            else {}
        ),
        "linked_horizon_plan_ids": row.linked_horizon_plan_ids_json if isinstance(row.linked_horizon_plan_ids_json, list) else [],
        "linked_improvement_proposal_ids": row.linked_improvement_proposal_ids_json if isinstance(row.linked_improvement_proposal_ids_json, list) else [],
        "linked_maintenance_run_ids": row.linked_maintenance_run_ids_json if isinstance(row.linked_maintenance_run_ids_json, list) else [],
        "operator_recommendations": row.operator_recommendations_json if isinstance(row.operator_recommendations_json, list) else [],
        "persistence_state": row.persistence_state,
        "review_status": row.review_status,
        "persistence_confidence": float(row.persistence_confidence or 0.0),
        "surviving_sessions": int(row.surviving_sessions or 0),
        "carry_forward_count": int(row.carry_forward_count or 0),
        "last_reviewed_at": row.last_reviewed_at,
        "review_notes": row.review_notes,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }