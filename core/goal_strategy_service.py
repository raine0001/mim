from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import build_cross_domain_reasoning_context, to_cross_domain_reasoning_out
from core.horizon_planning_service import create_horizon_plan
from core.improvement_service import generate_improvement_proposals as generate_improvement_proposals_for_strategy
from core.maintenance_service import run_environment_maintenance_cycle
from core.models import UserPreference, WorkspaceStrategyGoal


STRATEGY_GOAL_STATUSES = {
    "proposed",
    "active",
    "deferred",
    "superseded",
    "completed",
    "rejected",
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

    for candidate in candidates:
        strategy_type = str(candidate.get("strategy_type", "strategy_goal"))
        urgency = _bounded(float(candidate.get("urgency", 0.5) or 0.5))
        expected_impact = _bounded(float(candidate.get("expected_impact", 0.5) or 0.5))
        risk = _bounded(float(candidate.get("risk", _risk_baseline(strategy_type)) or _risk_baseline(strategy_type)))
        operator_pref = await _operator_preference_weight(strategy_type=strategy_type, db=db)

        score = _bounded(
            (urgency * 0.25)
            + (confidence * 0.2)
            + (expected_impact * 0.2)
            + ((1.0 - risk) * 0.1)
            + (operator_pref * 0.1)
            + (developmental_friction * 0.15)
        )

        ranked_candidates.append(
            {
                **candidate,
                "priority_score": score,
                "priority": _priority_label(score),
                "ranking_factors": {
                    "urgency": urgency,
                    "confidence": confidence,
                    "expected_impact": expected_impact,
                    "risk": risk,
                    "operator_preference_influence": operator_pref,
                    "developmental_friction_patterns": developmental_friction,
                },
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
            evidence_summary=str(candidate.get("why_formed", "")),
            supporting_evidence_json=candidate.get("supporting_evidence", {}) if isinstance(candidate.get("supporting_evidence", {}), dict) else {},
            contributing_domains_json=domains_present,
            ranking_factors_json=candidate.get("ranking_factors", {}) if isinstance(candidate.get("ranking_factors", {}), dict) else {},
            reasoning_summary=str(candidate.get("why_formed", "")),
            reasoning_json={
                "domains_contributed": domains_present,
                "cross_domain_links": links if isinstance(links, list) else [],
                "origin_context_confidence": confidence,
            },
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective57_strategy_goal": True,
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
        "linked_horizon_plan_ids": row.linked_horizon_plan_ids_json if isinstance(row.linked_horizon_plan_ids_json, list) else [],
        "linked_improvement_proposal_ids": row.linked_improvement_proposal_ids_json if isinstance(row.linked_improvement_proposal_ids_json, list) else [],
        "linked_maintenance_run_ids": row.linked_maintenance_run_ids_json if isinstance(row.linked_maintenance_run_ids_json, list) else [],
        "operator_recommendations": row.operator_recommendations_json if isinstance(row.operator_recommendations_json, list) else [],
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }