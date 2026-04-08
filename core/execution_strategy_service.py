from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_boundary_service import (
    build_boundary_action_controls,
    build_boundary_profile_snapshot,
    get_latest_autonomy_boundary_for_scope,
)
from core.execution_readiness_service import (
    execution_readiness_summary,
    load_latest_execution_readiness,
)
from core.execution_truth_governance_service import (
    latest_execution_truth_governance_snapshot,
)
from core.models import ExecutionIntent, ExecutionStrategyPlan, ExecutionTaskOrchestration
from core.stewardship_service import list_stewardship_states, to_stewardship_out


SUPPORTED_DOMAINS = {"robot", "web", "data", "decision"}
TERMINAL_STEP_STATES = {"completed", "failed", "blocked", "skipped"}
REMOTE_EXECUTOR_BY_DOMAIN = {
    "robot": "tod",
    "web": "web",
    "data": "data",
    "decision": "mim",
}


def _bounded(value: object, *, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value or 0.0)
    except Exception:
        numeric = 0.0
    return max(lo, min(hi, numeric))


def _json_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _json_list(raw: object) -> list:
    return raw if isinstance(raw, list) else []


def _normalized_scope(raw: object) -> str:
    value = str(raw or "").strip()
    return value or "global"


def _unique_domains(domains: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in domains:
        normalized = str(item or "").strip().lower()
        if not normalized or normalized not in SUPPORTED_DOMAINS or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered or ["decision"]


def understand_intent(
    *,
    raw_text: str,
    internal_intent: str,
    requested_goal: str = "",
    capability_name: str = "",
    metadata_json: dict | None = None,
) -> dict:
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    normalized = " ".join(str(raw_text or "").strip().lower().split())
    goal = str(requested_goal or "").strip()
    capability = str(capability_name or metadata.get("capability") or "").strip()
    tokens = set(normalized.replace(",", " ").replace("/", " ").split())

    canonical_intent = ""
    semantic_goal = goal or normalized
    rationale = "literal_intent"
    domains: list[str] = []
    suggested_steps: list[dict] = []
    confidence = 0.62

    if ("scan" in tokens or "inspect" in tokens) and (
        "capture" in tokens or "photo" in tokens or "frame" in tokens
    ):
        canonical_intent = "inspect_object"
        semantic_goal = "inspect object"
        rationale = "compound_scan_capture_collapsed_to_inspection_goal"
        domains = ["robot", "decision"]
        confidence = 0.88
        suggested_steps = [
            {
                "step": 1,
                "step_key": "observe_target",
                "action_type": "execute_capability",
                "capability": capability or "workspace_check",
                "domain": "robot",
                "details": "Scan the target area to locate the object and gather fresh state.",
            },
            {
                "step": 2,
                "step_key": "capture_evidence",
                "action_type": "capture_frame",
                "capability": "capture_frame",
                "domain": "robot",
                "details": "Capture a frame or artifact for the identified object.",
            },
            {
                "step": 3,
                "step_key": "assess_findings",
                "action_type": "decision_review",
                "capability": "decision",
                "domain": "decision",
                "details": "Summarize the inspection result and decide the next bounded step.",
            },
        ]
    elif internal_intent in {"execute_capability", "create_goal"} and goal:
        canonical_intent = capability or internal_intent
        semantic_goal = goal
        rationale = "goal_text_promoted_to_semantic_goal"
        confidence = 0.74
        domains = ["decision"]

    return {
        "canonical_intent": canonical_intent,
        "semantic_goal": semantic_goal,
        "rationale": rationale,
        "confidence": round(confidence, 6),
        "suggested_domains": _unique_domains(domains),
        "suggested_steps": suggested_steps,
        "literal_input": str(raw_text or "").strip(),
    }


def _default_steps_for_domains(*, goal_summary: str, domains: list[str]) -> list[dict]:
    steps: list[dict] = []
    sequence_index = 1
    if "robot" in domains:
        steps.append(
            {
                "step_key": "robot_observe",
                "title": "Observe target state",
                "domain": "robot",
                "action_type": "workspace_check",
                "depends_on": [],
                "status": "pending",
                "success_signal": "fresh workspace evidence collected",
                "details": f"Use robot/perception execution to advance goal: {goal_summary}",
                "sequence_index": sequence_index,
            }
        )
        sequence_index += 1
    if "web" in domains:
        steps.append(
            {
                "step_key": "web_reference_lookup",
                "title": "Collect web reference context",
                "domain": "web",
                "action_type": "web_lookup",
                "depends_on": [],
                "status": "pending",
                "success_signal": "relevant external reference found",
                "details": f"Gather web context relevant to goal: {goal_summary}",
                "sequence_index": sequence_index,
            }
        )
        sequence_index += 1
    if "data" in domains:
        steps.append(
            {
                "step_key": "data_context_fetch",
                "title": "Retrieve local data context",
                "domain": "data",
                "action_type": "data_fetch",
                "depends_on": [],
                "status": "pending",
                "success_signal": "local evidence gathered",
                "details": f"Retrieve memory or state relevant to goal: {goal_summary}",
                "sequence_index": sequence_index,
            }
        )
        sequence_index += 1
    steps.append(
        {
            "step_key": "decision_synthesis",
            "title": "Synthesize decision",
            "domain": "decision",
            "action_type": "decision_review",
            "depends_on": [step["step_key"] for step in steps],
            "status": "pending",
            "success_signal": "bounded next step selected",
            "details": f"Choose the best next action for goal: {goal_summary}",
            "sequence_index": sequence_index,
        }
    )
    return steps


def _strategy_blueprint(
    *,
    goal_summary: str,
    canonical_intent: str,
    requested_domains: list[str],
    intent_understanding: dict,
) -> dict:
    domains = _unique_domains(
        [
            *requested_domains,
            *[
                str(item)
                for item in _json_list(intent_understanding.get("suggested_domains", []))
            ],
        ]
    )
    suggested_steps = _json_list(intent_understanding.get("suggested_steps", []))
    primary_steps: list[dict]
    if suggested_steps:
        primary_steps = []
        for index, item in enumerate(suggested_steps, start=1):
            step = {
                "step_key": str(item.get("step_key") or f"step_{index}").strip(),
                "title": str(item.get("details") or item.get("step_key") or f"Step {index}").strip(),
                "domain": str(item.get("domain") or "decision").strip(),
                "action_type": str(item.get("action_type") or "decision_review").strip(),
                "depends_on": [],
                "status": "pending",
                "success_signal": "step completed",
                "details": str(item.get("details") or "").strip(),
                "sequence_index": index,
                "capability": str(item.get("capability") or "").strip(),
            }
            if primary_steps:
                step["depends_on"] = [primary_steps[-1]["step_key"]]
            primary_steps.append(step)
    else:
        primary_steps = _default_steps_for_domains(goal_summary=goal_summary, domains=domains)

    alternative_plans = [
        {
            "plan_key": "operator_review_fallback",
            "title": "Escalate to operator review",
            "reason": "Use when bounded autonomous planning loses confidence or evidence is contradictory.",
            "domains": ["decision"],
            "steps": [
                {
                    "step_key": "operator_review",
                    "title": "Request operator review",
                    "domain": "decision",
                    "action_type": "operator_review",
                }
            ],
        }
    ]
    if "web" not in domains:
        alternative_plans.append(
            {
                "plan_key": "web_reference_alternative",
                "title": "Gather web reference before deciding",
                "reason": "Use when local observation is weak and an external reference may disambiguate the next step.",
                "domains": ["web", "decision"],
                "steps": [
                    {
                        "step_key": "web_reference_lookup",
                        "title": "Collect web reference context",
                        "domain": "web",
                        "action_type": "web_lookup",
                    },
                    {
                        "step_key": "decision_synthesis",
                        "title": "Synthesize decision",
                        "domain": "decision",
                        "action_type": "decision_review",
                    },
                ],
            }
        )

    contingencies = []
    for step in primary_steps:
        step_key = str(step.get("step_key") or "").strip()
        if not step_key:
            continue
        fallback_key = "operator_review"
        if str(step.get("domain") or "").strip() == "robot":
            fallback_key = "web_reference_lookup"
        contingencies.append(
            {
                "on_step": step_key,
                "on_outcome": "failed",
                "fallback_step_key": fallback_key,
                "action": "activate_contingency",
                "reason": f"If {step_key} fails, switch to {fallback_key} or stop for review.",
            }
        )

    confidence = 0.7 + (0.05 * min(len(domains), 3))
    if canonical_intent == "inspect_object":
        confidence += 0.08
    return {
        "domains": domains,
        "primary_steps": primary_steps,
        "alternative_plans": alternative_plans,
        "contingencies": contingencies,
        "confidence": round(_bounded(confidence), 6),
    }


def _next_pending_step(primary_steps: list[dict]) -> dict:
    completed = {
        str(item.get("step_key") or "").strip()
        for item in primary_steps
        if str(item.get("status") or "").strip() == "completed"
    }
    for item in primary_steps:
        status = str(item.get("status") or "pending").strip()
        if status in TERMINAL_STEP_STATES and status != "pending":
            continue
        depends_on = [
            str(dep).strip()
            for dep in _json_list(item.get("depends_on", []))
            if str(dep).strip()
        ]
        if all(dep in completed for dep in depends_on):
            return item
    return {}


def _build_continuation_state(*, primary_steps: list[dict], outcome_reason: str = "") -> dict:
    next_step = _next_pending_step(primary_steps)
    completed_steps = [
        str(item.get("step_key") or "").strip()
        for item in primary_steps
        if str(item.get("status") or "").strip() == "completed"
    ]
    failed_steps = [
        str(item.get("step_key") or "").strip()
        for item in primary_steps
        if str(item.get("status") or "").strip() == "failed"
    ]
    blocked_steps = [
        str(item.get("step_key") or "").strip()
        for item in primary_steps
        if str(item.get("status") or "").strip() == "blocked"
    ]
    all_terminal = bool(primary_steps) and all(
        str(item.get("status") or "pending").strip() in TERMINAL_STEP_STATES
        for item in primary_steps
    )
    should_stop = bool(blocked_steps)
    if all_terminal and not next_step:
        should_stop = True

    stop_reason = outcome_reason.strip()
    if not stop_reason:
        if blocked_steps:
            stop_reason = "strategy_blocked_pending_review"
        elif all_terminal and not failed_steps:
            stop_reason = "plan_completed"
        elif failed_steps and not next_step:
            stop_reason = "no_safe_contingency_available"

    return {
        "current_step_key": str(next_step.get("step_key") or "").strip(),
        "recommended_next_step": next_step,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "blocked_steps": blocked_steps,
        "can_continue": bool(next_step) and not should_stop,
        "should_stop": should_stop,
        "stop_reason": stop_reason,
        "completed_step_count": len(completed_steps),
        "total_step_count": len(primary_steps),
    }


def _build_explainability(
    *,
    goal_summary: str,
    canonical_intent: str,
    continuation_state: dict,
    confidence: float,
) -> dict:
    next_step = _json_dict(continuation_state.get("recommended_next_step", {}))
    completed_count = int(continuation_state.get("completed_step_count", 0) or 0)
    total_count = int(continuation_state.get("total_step_count", 0) or 0)
    what_it_did = (
        f"Built a {total_count}-step strategy plan"
        + (f" for canonical intent {canonical_intent}" if canonical_intent else "")
        + (f" on goal '{goal_summary}'" if goal_summary else "")
    )
    why_it_did_it = (
        "Convert reactive execution into a bounded goal-driven sequence with alternatives, contingencies, and explainable continuation."
    )
    if next_step:
        what_next = str(
            next_step.get("title")
            or next_step.get("details")
            or next_step.get("step_key")
            or ""
        ).strip()
    elif bool(continuation_state.get("should_stop", False)):
        what_next = f"Stop and surface reason: {str(continuation_state.get('stop_reason') or 'strategy_complete').strip()}"
    else:
        what_next = "No additional bounded step selected."
    return {
        "what_it_did": what_it_did,
        "why_it_did_it": why_it_did_it,
        "what_it_will_do_next": what_next,
        "confidence": round(confidence, 6),
        "confidence_reasoning": (
            f"confidence={confidence:.2f}; completed_steps={completed_count}/{total_count}; "
            f"next_step={'present' if next_step else 'absent'}"
        ),
        "summary": f"{what_it_did}. Next: {what_next}",
    }


def _confidence_tier(score: float) -> str:
    bounded_score = _bounded(score)
    if bounded_score >= 0.85:
        return "high"
    if bounded_score >= 0.65:
        return "medium"
    return "low"


async def _load_strategy_support_context(
    *,
    db: AsyncSession,
    managed_scope: str,
    capability_name: str,
    action_label: str,
    requested_executor: str,
    metadata_json: dict | None,
) -> dict:
    scope = _normalized_scope(managed_scope)
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    readiness = load_latest_execution_readiness(
        action=action_label,
        capability_name=capability_name,
        managed_scope=scope,
        requested_executor=requested_executor,
        metadata_json=metadata,
    )
    governance = await latest_execution_truth_governance_snapshot(
        managed_scope=scope,
        db=db,
    )
    boundary_row = await get_latest_autonomy_boundary_for_scope(scope=scope, db=db)
    boundary_profile = build_boundary_profile_snapshot(boundary_row)
    stewardship_rows = await list_stewardship_states(
        managed_scope=scope,
        limit=1,
        db=db,
    )
    stewardship = to_stewardship_out(stewardship_rows[0]) if stewardship_rows else {}
    return {
        "readiness": readiness,
        "readiness_summary": execution_readiness_summary(readiness),
        "governance": governance,
        "boundary_profile": boundary_profile,
        "boundary_controls": build_boundary_action_controls(boundary_profile),
        "stewardship": stewardship,
    }


def _environment_awareness_snapshot(
    *,
    managed_scope: str,
    support_context: dict,
) -> dict:
    readiness_summary = _json_dict(support_context.get("readiness_summary", {}))
    governance = _json_dict(support_context.get("governance", {}))
    stewardship = _json_dict(support_context.get("stewardship", {}))
    signals: list[str] = []

    policy_outcome = str(readiness_summary.get("policy_outcome") or "allow").strip().lower()
    governance_decision = str(governance.get("governance_decision") or "monitor_only").strip()
    stewardship_health = _bounded(float(stewardship.get("current_health", 1.0) or 1.0))

    if policy_outcome in {"block", "degrade"}:
        signals.append(f"execution_readiness:{policy_outcome}")
    if governance_decision not in {"", "monitor_only", "increase_visibility"}:
        signals.append(f"execution_truth_governance:{governance_decision}")
    if stewardship and stewardship_health < 0.6:
        signals.append("stewardship_health_low")

    if policy_outcome == "block" or governance_decision == "escalate_to_operator":
        status = "degraded"
    elif signals:
        status = "watch"
    else:
        status = "stable"

    return {
        "managed_scope": _normalized_scope(managed_scope),
        "status": status,
        "signals": signals,
        "execution_readiness": readiness_summary,
        "execution_truth_governance": {
            "decision": governance_decision or "monitor_only",
            "confidence": round(float(governance.get("confidence", 0.0) or 0.0), 6),
            "signal_count": int(governance.get("signal_count", 0) or 0),
        },
        "stewardship": {
            "status": str(stewardship.get("status") or "inactive").strip() or "inactive",
            "current_health": round(float(stewardship.get("current_health", 0.0) or 0.0), 6),
            "maintenance_priority": str(stewardship.get("maintenance_priority") or "").strip(),
        },
    }


def _context_persistence_snapshot(
    *,
    trace_id: str,
    intent_id: int | None,
    orchestration_id: int | None,
    execution_id: int | None,
    managed_scope: str,
    resumption_count: int,
    continuation_state: dict,
    checkpoint_json: dict | None,
) -> dict:
    checkpoint = checkpoint_json if isinstance(checkpoint_json, dict) else {}
    persistence_keys = [
        key
        for key, value in (
            ("trace_id", trace_id),
            ("intent_id", intent_id),
            ("orchestration_id", orchestration_id),
            ("execution_id", execution_id),
            ("managed_scope", managed_scope),
        )
        if str(value or "").strip()
    ]
    return {
        "trace_id": str(trace_id or "").strip(),
        "intent_id": intent_id,
        "orchestration_id": orchestration_id,
        "execution_id": execution_id,
        "managed_scope": _normalized_scope(managed_scope),
        "resumption_count": max(0, int(resumption_count or 0)),
        "current_step_key": str(continuation_state.get("current_step_key") or "").strip(),
        "checkpoint_keys": sorted(str(key).strip() for key in checkpoint.keys() if str(key).strip()),
        "persistence_keys": persistence_keys,
        "context_status": (
            "resumable"
            if bool(continuation_state.get("can_continue", False))
            else "terminal"
            if bool(continuation_state.get("should_stop", False))
            else "retained"
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _coordination_state_snapshot(
    *,
    domains: list[str],
    primary_steps: list[dict],
    readiness_summary: dict,
) -> dict:
    normalized_domains = _unique_domains(domains)
    agents = [
        {
            "agent_key": f"{domain}_executor",
            "domain": domain,
            "executor": REMOTE_EXECUTOR_BY_DOMAIN.get(domain, "mim"),
            "role": "specialist" if domain != "decision" else "coordinator",
        }
        for domain in normalized_domains
    ]
    handoffs: list[dict] = []
    previous_domain = ""
    previous_step_key = ""
    for item in primary_steps:
        current_domain = str(item.get("domain") or "decision").strip() or "decision"
        current_step_key = str(item.get("step_key") or "").strip()
        if previous_domain and current_domain != previous_domain:
            handoffs.append(
                {
                    "from_domain": previous_domain,
                    "to_domain": current_domain,
                    "after_step": previous_step_key,
                    "next_step": current_step_key,
                }
            )
        previous_domain = current_domain
        previous_step_key = current_step_key

    tod_required = any(agent.get("executor") == "tod" for agent in agents)
    policy_outcome = str(readiness_summary.get("policy_outcome") or "allow").strip().lower()
    if tod_required and policy_outcome in {"block", "degrade"}:
        coordination_status = "awaiting_remote_clearance"
        coordination_confidence = 0.42
    elif len(normalized_domains) > 1:
        coordination_status = "handoff_ready"
        coordination_confidence = 0.78
    else:
        coordination_status = "local_only"
        coordination_confidence = 0.88

    return {
        "mode": "multi_agent" if len(normalized_domains) > 1 else "single_agent",
        "domains": normalized_domains,
        "agents": agents,
        "handoffs": handoffs,
        "tod_coordination_required": tod_required,
        "coordination_status": coordination_status,
        "coordination_confidence": round(_bounded(coordination_confidence), 6),
    }


def _confidence_assessment_snapshot(
    *,
    blueprint_confidence: float,
    observed_confidence: float | None,
    intent_understanding: dict,
    domains: list[str],
    continuation_state: dict,
    support_context: dict,
) -> dict:
    readiness_summary = _json_dict(support_context.get("readiness_summary", {}))
    governance = _json_dict(support_context.get("governance", {}))
    boundary_profile = _json_dict(support_context.get("boundary_profile", {}))

    base_signal = _bounded(observed_confidence if observed_confidence is not None else blueprint_confidence)
    intent_signal = _bounded(float(intent_understanding.get("confidence", 0.5) or 0.5))
    policy_outcome = str(readiness_summary.get("policy_outcome") or "allow").strip().lower()
    environment_signal = 0.92
    if policy_outcome == "degrade":
        environment_signal = 0.58
    elif policy_outcome == "block":
        environment_signal = 0.24
    governance_decision = str(governance.get("governance_decision") or "monitor_only").strip()
    if governance_decision in {"lower_autonomy_boundary", "require_sandbox_experiment"}:
        environment_signal = min(environment_signal, 0.52)
    elif governance_decision == "escalate_to_operator":
        environment_signal = min(environment_signal, 0.36)

    boundary_level = str(boundary_profile.get("current_level") or "operator_required").strip()
    if boundary_level == "strategy_auto":
        boundary_signal = 0.92
    elif boundary_level == "bounded_auto":
        boundary_signal = 0.78
    else:
        boundary_signal = 0.48

    continuation_signal = 0.88 if bool(continuation_state.get("can_continue", False)) else 0.5
    if bool(continuation_state.get("should_stop", False)):
        continuation_signal = 0.25
    complexity_penalty = min(0.18, max(0, len(_unique_domains(domains)) - 1) * 0.06)

    score = _bounded(
        (base_signal * 0.4)
        + (intent_signal * 0.2)
        + (environment_signal * 0.15)
        + (boundary_signal * 0.15)
        + (continuation_signal * 0.1)
        - complexity_penalty
    )
    tier = _confidence_tier(score)
    return {
        "score": round(score, 6),
        "tier": tier,
        "source": "observed" if observed_confidence is not None else "planned",
        "recommended_action": (
            "continue"
            if score >= 0.65 and bool(continuation_state.get("can_continue", False))
            else "refine_or_review"
        ),
        "factors": {
            "base_signal": round(base_signal, 6),
            "intent_signal": round(intent_signal, 6),
            "environment_signal": round(environment_signal, 6),
            "boundary_signal": round(boundary_signal, 6),
            "continuation_signal": round(continuation_signal, 6),
            "domain_complexity_penalty": round(complexity_penalty, 6),
        },
    }


def _refinement_state_snapshot(
    *,
    previous_state: dict,
    alternative_plans: list[dict],
    completed_step_key: str,
    outcome: str,
    confidence_assessment: dict,
    continuation_state: dict,
) -> dict:
    previous = previous_state if isinstance(previous_state, dict) else {}
    needs_refinement = (
        str(outcome or "").strip() in {"failed", "blocked"}
        or float(confidence_assessment.get("score", 0.0) or 0.0) < 0.55
    )
    adaptation_count = int(previous.get("adaptation_count", 0) or 0)
    if needs_refinement and str(outcome or "").strip() in {"failed", "blocked"}:
        adaptation_count += 1
    candidate_plan = alternative_plans[0] if alternative_plans else {}
    reason = "stable_plan"
    if str(outcome or "").strip() in {"failed", "blocked"}:
        reason = f"step_{str(outcome or '').strip()}:{completed_step_key}"
    elif needs_refinement:
        reason = "confidence_below_refinement_threshold"
    elif not bool(continuation_state.get("can_continue", False)):
        reason = "awaiting_next_safe_step"
    return {
        "needs_refinement": needs_refinement,
        "reason": reason,
        "adaptation_count": adaptation_count,
        "candidate_plan_key": str(candidate_plan.get("plan_key") or "").strip(),
        "candidate_plan_title": str(candidate_plan.get("title") or "").strip(),
        "superseded_step_key": str(completed_step_key or "").strip(),
    }


def _safety_envelope_snapshot(
    *,
    managed_scope: str,
    support_context: dict,
    confidence_assessment: dict,
    continuation_state: dict,
) -> dict:
    readiness_summary = _json_dict(support_context.get("readiness_summary", {}))
    governance = _json_dict(support_context.get("governance", {}))
    boundary_profile = _json_dict(support_context.get("boundary_profile", {}))
    boundary_controls = _json_dict(support_context.get("boundary_controls", {}))
    confidence_score = _bounded(float(confidence_assessment.get("score", 0.0) or 0.0))

    policy_outcome = str(readiness_summary.get("policy_outcome") or "allow").strip().lower()
    governance_decision = str(governance.get("governance_decision") or "monitor_only").strip()
    boundary_level = str(boundary_profile.get("current_level") or "operator_required").strip()
    operator_review_required = (
        policy_outcome in {"block", "degrade"}
        or governance_decision in {"lower_autonomy_boundary", "require_sandbox_experiment", "escalate_to_operator"}
        or boundary_level in {"manual_only", "operator_required"}
        or confidence_score < 0.55
    )
    safe_to_continue = bool(continuation_state.get("can_continue", False)) and not bool(
        continuation_state.get("should_stop", False)
    ) and not operator_review_required
    stop_reason = str(continuation_state.get("stop_reason") or "").strip()
    if not stop_reason and operator_review_required:
        stop_reason = "operator_review_required"

    return {
        "managed_scope": _normalized_scope(managed_scope),
        "operator_review_required": operator_review_required,
        "safe_to_continue": safe_to_continue,
        "status": (
            "continue"
            if safe_to_continue
            else "review_required"
            if operator_review_required
            else "stop"
        ),
        "stop_reason": stop_reason,
        "minimum_confidence_to_continue": 0.55,
        "autonomy_boundary": boundary_profile,
        "action_controls": boundary_controls,
        "execution_readiness": readiness_summary,
        "governance_decision": governance_decision or "monitor_only",
    }


async def ensure_execution_strategy_plan(
    *,
    db: AsyncSession,
    trace_id: str,
    intent: ExecutionIntent,
    orchestration: ExecutionTaskOrchestration,
    execution_id: int | None,
    actor: str,
    source: str,
) -> ExecutionStrategyPlan:
    normalized_trace = str(trace_id or "").strip()
    existing = None
    if normalized_trace:
        existing = (
            (
                await db.execute(
                    select(ExecutionStrategyPlan)
                    .where(ExecutionStrategyPlan.trace_id == normalized_trace)
                    .order_by(ExecutionStrategyPlan.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    intent_context = _json_dict(intent.context_json)
    resolution_metadata = _json_dict(intent_context.get("resolution_metadata", {}))
    intent_understanding = _json_dict(resolution_metadata.get("intent_understanding", {}))
    canonical_intent = str(
        intent_understanding.get("canonical_intent") or intent.intent_type or ""
    ).strip()
    goal_summary = str(
        intent_understanding.get("semantic_goal") or intent.requested_goal or ""
    ).strip()
    requested_domains = [
        str(item) for item in _json_list(intent_context.get("requested_domains", []))
    ]
    blueprint = _strategy_blueprint(
        goal_summary=goal_summary,
        canonical_intent=canonical_intent,
        requested_domains=requested_domains,
        intent_understanding=intent_understanding,
    )
    continuation_state = _build_continuation_state(primary_steps=blueprint["primary_steps"])
    requested_executor = REMOTE_EXECUTOR_BY_DOMAIN.get(
        blueprint["domains"][0] if blueprint["domains"] else "decision",
        "mim",
    )
    support_context = await _load_strategy_support_context(
        db=db,
        managed_scope=intent.managed_scope,
        capability_name=str(intent.capability_name or "").strip(),
        action_label=goal_summary or canonical_intent or str(intent.capability_name or "execution").strip(),
        requested_executor=requested_executor,
        metadata_json=intent_context,
    )
    context_persistence = _context_persistence_snapshot(
        trace_id=normalized_trace,
        intent_id=int(intent.id),
        orchestration_id=int(orchestration.id),
        execution_id=execution_id,
        managed_scope=intent.managed_scope,
        resumption_count=int(intent.resumption_count or 0),
        continuation_state=continuation_state,
        checkpoint_json=orchestration.checkpoint_json if isinstance(orchestration.checkpoint_json, dict) else {},
    )
    coordination_state = _coordination_state_snapshot(
        domains=blueprint["domains"],
        primary_steps=blueprint["primary_steps"],
        readiness_summary=_json_dict(support_context.get("readiness_summary", {})),
    )
    confidence_assessment = _confidence_assessment_snapshot(
        blueprint_confidence=float(blueprint["confidence"]),
        observed_confidence=None,
        intent_understanding=intent_understanding,
        domains=blueprint["domains"],
        continuation_state=continuation_state,
        support_context=support_context,
    )
    refinement_state = _refinement_state_snapshot(
        previous_state=_json_dict(_json_dict(existing.metadata_json if existing is not None else {}).get("refinement_state", {})),
        alternative_plans=blueprint["alternative_plans"],
        completed_step_key="",
        outcome="planned",
        confidence_assessment=confidence_assessment,
        continuation_state=continuation_state,
    )
    environment_awareness = _environment_awareness_snapshot(
        managed_scope=intent.managed_scope,
        support_context=support_context,
    )
    safety_envelope = _safety_envelope_snapshot(
        managed_scope=intent.managed_scope,
        support_context=support_context,
        confidence_assessment=confidence_assessment,
        continuation_state=continuation_state,
    )
    explainability = _build_explainability(
        goal_summary=goal_summary,
        canonical_intent=canonical_intent,
        continuation_state=continuation_state,
        confidence=float(confidence_assessment["score"]),
    )
    row = existing or ExecutionStrategyPlan(
        trace_id=normalized_trace,
        actor=actor,
        source=source,
        managed_scope=_normalized_scope(intent.managed_scope),
    )
    row.intent_id = int(intent.id)
    row.orchestration_id = int(orchestration.id)
    row.execution_id = execution_id
    row.actor = actor
    row.source = source
    row.managed_scope = _normalized_scope(intent.managed_scope)
    row.status = "active" if continuation_state.get("can_continue", False) else "planned"
    row.plan_family = "goal_driven_sequence"
    row.canonical_intent = canonical_intent
    row.goal_summary = goal_summary
    row.primary_plan_json = blueprint["primary_steps"]
    row.alternative_plans_json = blueprint["alternative_plans"]
    row.contingency_rules_json = blueprint["contingencies"]
    row.coordination_domains_json = blueprint["domains"]
    row.continuation_state_json = continuation_state
    row.explainability_json = explainability
    row.confidence = float(confidence_assessment["score"])
    row.metadata_json = {
        **(_json_dict(existing.metadata_json) if existing is not None else {}),
        **intent_context,
        "intent_understanding": intent_understanding,
        "confidence_assessment": confidence_assessment,
        "refinement_state": refinement_state,
        "environment_awareness": environment_awareness,
        "context_persistence": context_persistence,
        "coordination_state": coordination_state,
        "safety_envelope": safety_envelope,
        "strategy_generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if bool(continuation_state.get("should_stop", False)):
        row.status = "blocked"
    elif bool(safety_envelope.get("operator_review_required", False)):
        row.status = "pending_review"
    elif bool(continuation_state.get("can_continue", False)):
        row.status = "active"
    else:
        row.status = "planned"
    if existing is None:
        db.add(row)
    await db.flush()
    return row


async def get_execution_strategy_plan(*, plan_id: int, db: AsyncSession) -> ExecutionStrategyPlan | None:
    return await db.get(ExecutionStrategyPlan, int(plan_id))


async def list_execution_strategy_plans(
    *,
    db: AsyncSession,
    managed_scope: str = "",
    trace_id: str = "",
    limit: int = 20,
) -> list[ExecutionStrategyPlan]:
    stmt = select(ExecutionStrategyPlan).order_by(ExecutionStrategyPlan.id.desc()).limit(max(1, min(200, int(limit))))
    if str(managed_scope or "").strip():
        stmt = stmt.where(ExecutionStrategyPlan.managed_scope == _normalized_scope(managed_scope))
    if str(trace_id or "").strip():
        stmt = stmt.where(ExecutionStrategyPlan.trace_id == str(trace_id).strip())
    return list((await db.execute(stmt)).scalars().all())


async def latest_execution_strategy_plan(
    *,
    db: AsyncSession,
    managed_scope: str = "",
    trace_id: str = "",
) -> ExecutionStrategyPlan | None:
    rows = await list_execution_strategy_plans(
        db=db,
        managed_scope=managed_scope,
        trace_id=trace_id,
        limit=1,
    )
    return rows[0] if rows else None


def _update_step_status(*, primary_steps: list[dict], completed_step_key: str, outcome: str) -> list[dict]:
    updated: list[dict] = []
    for item in primary_steps:
        copied = dict(item)
        if str(copied.get("step_key") or "").strip() == completed_step_key:
            copied["status"] = outcome
        updated.append(copied)
    return updated


def _activate_contingency(*, primary_steps: list[dict], contingency_rules: list[dict], completed_step_key: str, outcome: str) -> list[dict]:
    if outcome != "failed":
        return primary_steps
    fallback_key = ""
    for rule in contingency_rules:
        if str(rule.get("on_step") or "").strip() == completed_step_key and str(rule.get("on_outcome") or "").strip() == outcome:
            fallback_key = str(rule.get("fallback_step_key") or "").strip()
            break
    if not fallback_key:
        return primary_steps
    updated: list[dict] = []
    found = False
    for item in primary_steps:
        copied = dict(item)
        if str(copied.get("step_key") or "").strip() == fallback_key:
            copied["status"] = "pending"
            found = True
        updated.append(copied)
    if found:
        return updated
    updated.append(
        {
            "step_key": fallback_key,
            "title": fallback_key.replace("_", " "),
            "domain": "decision",
            "action_type": fallback_key,
            "depends_on": [],
            "status": "pending",
            "success_signal": "fallback handled",
            "details": f"Fallback activated after {completed_step_key} failed.",
            "sequence_index": len(updated) + 1,
        }
    )
    return updated


async def advance_execution_strategy_plan(
    *,
    plan: ExecutionStrategyPlan,
    actor: str,
    source: str,
    completed_step_key: str,
    outcome: str,
    observed_confidence: float | None,
    metadata_json: dict | None,
    db: AsyncSession,
) -> ExecutionStrategyPlan:
    existing_metadata = _json_dict(plan.metadata_json)
    normalized_step = str(completed_step_key or "").strip()
    normalized_outcome = str(outcome or "completed").strip() or "completed"
    primary_steps = _update_step_status(
        primary_steps=_json_list(plan.primary_plan_json),
        completed_step_key=normalized_step,
        outcome=normalized_outcome,
    )
    primary_steps = _activate_contingency(
        primary_steps=primary_steps,
        contingency_rules=_json_list(plan.contingency_rules_json),
        completed_step_key=normalized_step,
        outcome=normalized_outcome,
    )
    outcome_reason = ""
    if normalized_outcome in {"blocked", "failed"}:
        outcome_reason = f"step_{normalized_outcome}:{normalized_step}"
    continuation_state = _build_continuation_state(
        primary_steps=primary_steps,
        outcome_reason=outcome_reason,
    )
    support_context = await _load_strategy_support_context(
        db=db,
        managed_scope=plan.managed_scope,
        capability_name=str(existing_metadata.get("capability_name") or plan.canonical_intent or "").strip(),
        action_label=str(plan.goal_summary or plan.canonical_intent or "execution").strip(),
        requested_executor=str(
            _json_dict(existing_metadata.get("coordination_state", {})).get("agents", [{}])[0].get("executor")
            if isinstance(_json_dict(existing_metadata.get("coordination_state", {})).get("agents", []), list)
            and _json_dict(existing_metadata.get("coordination_state", {})).get("agents", [])
            else REMOTE_EXECUTOR_BY_DOMAIN.get(
                str((_json_list(plan.coordination_domains_json) or ["decision"])[0]),
                "mim",
            )
        ).strip() or "mim",
        metadata_json=existing_metadata,
    )
    coordination_state = _coordination_state_snapshot(
        domains=[str(item) for item in _json_list(plan.coordination_domains_json)],
        primary_steps=primary_steps,
        readiness_summary=_json_dict(support_context.get("readiness_summary", {})),
    )
    confidence_assessment = _confidence_assessment_snapshot(
        blueprint_confidence=float(plan.confidence or 0.0),
        observed_confidence=observed_confidence,
        intent_understanding=_json_dict(existing_metadata.get("intent_understanding", {})),
        domains=[str(item) for item in _json_list(plan.coordination_domains_json)],
        continuation_state=continuation_state,
        support_context=support_context,
    )
    refinement_state = _refinement_state_snapshot(
        previous_state=_json_dict(existing_metadata.get("refinement_state", {})),
        alternative_plans=_json_list(plan.alternative_plans_json),
        completed_step_key=normalized_step,
        outcome=normalized_outcome,
        confidence_assessment=confidence_assessment,
        continuation_state=continuation_state,
    )
    environment_awareness = _environment_awareness_snapshot(
        managed_scope=plan.managed_scope,
        support_context=support_context,
    )
    context_persistence = _context_persistence_snapshot(
        trace_id=plan.trace_id,
        intent_id=plan.intent_id,
        orchestration_id=plan.orchestration_id,
        execution_id=plan.execution_id,
        managed_scope=plan.managed_scope,
        resumption_count=int(_json_dict(existing_metadata.get("context_persistence", {})).get("resumption_count", 0) or 0),
        continuation_state=continuation_state,
        checkpoint_json=_json_dict(existing_metadata.get("checkpoint_json", {})),
    )
    safety_envelope = _safety_envelope_snapshot(
        managed_scope=plan.managed_scope,
        support_context=support_context,
        confidence_assessment=confidence_assessment,
        continuation_state=continuation_state,
    )
    plan.primary_plan_json = primary_steps
    plan.continuation_state_json = {
        **continuation_state,
        "last_action": {
            "actor": actor,
            "source": source,
            "completed_step_key": normalized_step,
            "outcome": normalized_outcome,
        },
    }
    plan.explainability_json = _build_explainability(
        goal_summary=str(plan.goal_summary or "").strip(),
        canonical_intent=str(plan.canonical_intent or "").strip(),
        continuation_state=continuation_state,
        confidence=float(confidence_assessment["score"]),
    )
    plan.confidence = float(confidence_assessment["score"])
    plan.status = (
        "completed"
        if str(continuation_state.get("stop_reason") or "").strip() == "plan_completed"
        else "blocked"
        if bool(continuation_state.get("should_stop", False))
        else "pending_review"
        if bool(safety_envelope.get("operator_review_required", False))
        else "active"
    )
    plan.metadata_json = {
        **existing_metadata,
        "last_continuation_metadata": _json_dict(metadata_json),
        "confidence_assessment": confidence_assessment,
        "refinement_state": refinement_state,
        "environment_awareness": environment_awareness,
        "context_persistence": context_persistence,
        "coordination_state": coordination_state,
        "safety_envelope": safety_envelope,
    }
    await db.flush()
    return plan


def to_execution_strategy_plan_out(row: ExecutionStrategyPlan) -> dict:
    metadata = _json_dict(row.metadata_json)
    return {
        "strategy_plan_id": int(row.id),
        "trace_id": str(row.trace_id or "").strip(),
        "intent_id": row.intent_id,
        "orchestration_id": row.orchestration_id,
        "execution_id": row.execution_id,
        "source": str(row.source or "").strip(),
        "actor": str(row.actor or "").strip(),
        "managed_scope": _normalized_scope(row.managed_scope),
        "status": str(row.status or "").strip(),
        "plan_family": str(row.plan_family or "").strip(),
        "canonical_intent": str(row.canonical_intent or "").strip(),
        "goal_summary": str(row.goal_summary or "").strip(),
        "primary_plan": _json_list(row.primary_plan_json),
        "alternative_plans": _json_list(row.alternative_plans_json),
        "contingency_rules": _json_list(row.contingency_rules_json),
        "coordination_domains": [
            str(item).strip() for item in _json_list(row.coordination_domains_json) if str(item).strip()
        ],
        "continuation_state": _json_dict(row.continuation_state_json),
        "explainability": _json_dict(row.explainability_json),
        "confidence": round(float(row.confidence or 0.0), 6),
        "confidence_assessment": _json_dict(metadata.get("confidence_assessment", {})),
        "refinement_state": _json_dict(metadata.get("refinement_state", {})),
        "environment_awareness": _json_dict(metadata.get("environment_awareness", {})),
        "context_persistence": _json_dict(metadata.get("context_persistence", {})),
        "coordination_state": _json_dict(metadata.get("coordination_state", {})),
        "safety_envelope": _json_dict(metadata.get("safety_envelope", {})),
        "metadata_json": metadata,
        "created_at": row.created_at,
    }