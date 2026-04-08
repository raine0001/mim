from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ExecutionIntent, ExecutionStrategyPlan, ExecutionTaskOrchestration


SUPPORTED_DOMAINS = {"robot", "web", "data", "decision"}
TERMINAL_STEP_STATES = {"completed", "failed", "blocked", "skipped"}


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
    explainability = _build_explainability(
        goal_summary=goal_summary,
        canonical_intent=canonical_intent,
        continuation_state=continuation_state,
        confidence=float(blueprint["confidence"]),
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
    row.confidence = float(blueprint["confidence"])
    row.metadata_json = {
        **intent_context,
        "intent_understanding": intent_understanding,
        "strategy_generated_at": datetime.now(timezone.utc).isoformat(),
    }
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
    confidence = _bounded(
        observed_confidence if observed_confidence is not None else plan.confidence,
        lo=0.0,
        hi=1.0,
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
        confidence=confidence,
    )
    plan.confidence = confidence
    plan.status = (
        "completed"
        if str(continuation_state.get("stop_reason") or "").strip() == "plan_completed"
        else "blocked"
        if bool(continuation_state.get("should_stop", False))
        else "active"
    )
    plan.metadata_json = {
        **_json_dict(plan.metadata_json),
        "last_continuation_metadata": _json_dict(metadata_json),
    }
    await db.flush()
    return plan


def to_execution_strategy_plan_out(row: ExecutionStrategyPlan) -> dict:
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
        "metadata_json": _json_dict(row.metadata_json),
        "created_at": row.created_at,
    }