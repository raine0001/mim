from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import build_cross_domain_reasoning_context, to_cross_domain_reasoning_out
from core.horizon_planning_service import create_horizon_plan
from core.improvement_service import generate_improvement_proposals
from core.preferences import DEFAULT_USER_ID, get_user_preference_value, upsert_user_preference
from core.models import (
    Goal,
    InputEvent,
    UserPreference,
    WorkspaceCollaborationPattern,
    WorkspaceCollaborationNegotiation,
    WorkspaceCrossDomainReasoningContext,
    WorkspaceHorizonPlan,
    WorkspaceInquiryQuestion,
    WorkspaceInterruptionEvent,
    WorkspaceMonitoringState,
    WorkspaceTaskOrchestration,
)


ORCHESTRATION_POLICIES = {"ask", "defer", "replan", "escalate"}
COLLABORATION_MODES = {"autonomous", "assistive", "confirmation-first", "deferential"}
COLLABORATION_POLICY_VERSION = "human-aware-collaboration-v1"
NEGOTIATION_POLICY_VERSION = "human-aware-negotiation-v1"
NEGOTIATION_PATTERN_PREFERENCE_TYPE = "collaboration_negotiation_patterns"
NEGOTIATION_PATTERN_MIN_REUSE_COUNT = 2
NEGOTIATION_PATTERN_MIN_CONFIDENCE = 0.8
NEGOTIATION_MEMORY_PREFERENCE_TYPE = "collaboration_negotiation_memory"
NEGOTIATION_MEMORY_MIN_EVIDENCE = 4
NEGOTIATION_MEMORY_MIN_CONFIDENCE = 0.74
NEGOTIATION_MEMORY_REVISION_FLOOR = 0.55
NEGOTIATION_MEMORY_MAX_INTERACTIONS = 40
NEGOTIATION_MEMORY_DECAY_HALF_LIFE_DAYS = 14.0
NEGOTIATION_MEMORY_STALE_AFTER_DAYS = 45.0
NEGOTIATION_ABSTRACTION_POLICY_VERSION = "objective69-negotiation-pattern-abstraction-v1"
NEGOTIATION_ABSTRACTION_MIN_EVIDENCE = 4
NEGOTIATION_ABSTRACTION_MIN_CONFIDENCE = 0.74
NEGOTIATION_OPTION_IDS = {
    "continue_now",
    "defer_action",
    "rescan_first",
    "speak_summary_only",
    "request_confirmation_later",
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _parse_iso8601(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _negotiation_memory_decay(*, last_updated_at: object) -> tuple[float, float, bool]:
    parsed = _parse_iso8601(last_updated_at)
    if not parsed:
        return 1.0, 0.0, False
    now = datetime.now(timezone.utc)
    age_days = max((now - parsed).total_seconds() / 86400.0, 0.0)
    if NEGOTIATION_MEMORY_DECAY_HALF_LIFE_DAYS <= 0:
        decay_factor = 1.0
    else:
        decay_factor = _bounded(0.5 ** (age_days / NEGOTIATION_MEMORY_DECAY_HALF_LIFE_DAYS), lo=0.0, hi=1.0)
    is_stale = age_days >= NEGOTIATION_MEMORY_STALE_AFTER_DAYS
    return decay_factor, age_days, is_stale


def _priority_label(score: float) -> str:
    if score >= 0.8:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.45:
        return "normal"
    return "low"


def _run_id(metadata_json: dict) -> str:
    if not isinstance(metadata_json, dict):
        return ""
    return str(metadata_json.get("run_id", "")).strip()


def _default_human_aware_state() -> dict:
    return {
        "human_in_workspace": False,
        "human_near_target_zone": False,
        "human_near_motion_path": False,
        "shared_workspace_active": False,
        "operator_present": False,
        "occupied_zones": [],
        "high_proximity_zones": [],
    }


def _match_run_id(metadata_json: dict, run_id: str) -> bool:
    if not run_id:
        return True
    if not isinstance(metadata_json, dict):
        return False
    return str(metadata_json.get("run_id", "")).strip() == run_id


def _domain_signals(context: dict) -> dict[str, int]:
    workspace = context.get("workspace_state", {}) if isinstance(context.get("workspace_state", {}), dict) else {}
    communication = context.get("communication_state", {}) if isinstance(context.get("communication_state", {}), dict) else {}
    external = context.get("external_information", {}) if isinstance(context.get("external_information", {}), dict) else {}
    development = context.get("development_state", {}) if isinstance(context.get("development_state", {}), dict) else {}
    self_improvement = context.get("self_improvement_state", {}) if isinstance(context.get("self_improvement_state", {}), dict) else {}

    return {
        "workspace_state": _safe_int(workspace.get("observation_count", 0)),
        "communication_state": _safe_int(communication.get("input_event_count", 0)) + _safe_int(communication.get("output_event_count", 0)),
        "external_information": _safe_int(external.get("external_item_count", 0)),
        "development_state": _safe_int(development.get("pattern_count", 0)),
        "self_improvement_state": _safe_int(self_improvement.get("backlog_item_count", 0)),
    }


def _priority_score(*, context_confidence: float, signals: dict[str, int]) -> float:
    weighted_signal = (
        (signals.get("workspace_state", 0) * 0.25)
        + (signals.get("communication_state", 0) * 0.15)
        + (signals.get("external_information", 0) * 0.2)
        + (signals.get("development_state", 0) * 0.2)
        + (signals.get("self_improvement_state", 0) * 0.2)
    )
    normalized_signal = _bounded(weighted_signal / 20.0)
    return _bounded((context_confidence * 0.6) + (normalized_signal * 0.4))


def _dependency_gaps(
    *,
    context_confidence: float,
    contributing_domains: list[str],
    min_context_confidence: float,
    min_domains_required: int,
    context_reasoning: dict,
) -> list[dict]:
    gaps: list[dict] = []
    if context_confidence < min_context_confidence:
        gaps.append(
            {
                "dependency": "context_confidence",
                "required": min_context_confidence,
                "observed": context_confidence,
                "reason": "cross_domain_confidence_below_threshold",
            }
        )

    if len(contributing_domains) < min_domains_required:
        gaps.append(
            {
                "dependency": "domain_coverage",
                "required": min_domains_required,
                "observed": len(contributing_domains),
                "reason": "insufficient_contributing_domains",
            }
        )

    links = context_reasoning.get("cross_domain_links", []) if isinstance(context_reasoning.get("cross_domain_links", []), list) else []
    if len(contributing_domains) >= 2 and not links:
        gaps.append(
            {
                "dependency": "cross_domain_linkage",
                "required": 1,
                "observed": 0,
                "reason": "missing_explainable_links_between_domains",
            }
        )

    return gaps


async def _latest_reasoning_context(*, run_id: str, db: AsyncSession) -> WorkspaceCrossDomainReasoningContext | None:
    rows = (
        await db.execute(
            select(WorkspaceCrossDomainReasoningContext)
            .order_by(WorkspaceCrossDomainReasoningContext.id.desc())
            .limit(100)
        )
    ).scalars().all()
    if not run_id:
        return rows[0] if rows else None
    for row in rows:
        if _match_run_id(row.metadata_json if isinstance(row.metadata_json, dict) else {}, run_id):
            return row
    return None


async def _human_aware_state(*, db: AsyncSession) -> dict:
    row = (
        await db.execute(
            select(WorkspaceMonitoringState)
            .order_by(WorkspaceMonitoringState.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if not row:
        return _default_human_aware_state()
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    state = metadata.get("human_aware", {}) if isinstance(metadata.get("human_aware", {}), dict) else {}
    return {
        **_default_human_aware_state(),
        **state,
    }


async def _communication_urgency(
    *,
    lookback_hours: int,
    run_id: str,
    override: float | None,
    db: AsyncSession,
) -> tuple[float, dict]:
    if override is not None:
        return _bounded(float(override)), {"source": "override", "sample_count": 0}

    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    rows = (
        await db.execute(
            select(InputEvent)
            .where(InputEvent.created_at >= since)
            .order_by(InputEvent.id.desc())
            .limit(300)
        )
    ).scalars().all()

    if run_id:
        rows = [
            item
            for item in rows
            if _match_run_id(item.metadata_json if isinstance(item.metadata_json, dict) else {}, run_id)
        ]

    if not rows:
        return 0.0, {"source": "input_events", "sample_count": 0}

    urgent_hits = 0
    very_recent_hits = 0
    now = datetime.now(timezone.utc)
    for item in rows:
        metadata = item.metadata_json if isinstance(item.metadata_json, dict) else {}
        raw = str(item.raw_input or "").lower()
        intent = str(item.parsed_intent or "").lower()
        urgency_hint = str(metadata.get("urgency", "")).lower()
        if urgency_hint in {"high", "critical", "urgent"}:
            urgent_hits += 1
        elif any(token in raw for token in ["urgent", "asap", "immediately", "right away"]):
            urgent_hits += 1
        elif intent in {"operator_urgent_request", "urgent_request", "interruption"}:
            urgent_hits += 1
        age_seconds = max((now - item.created_at).total_seconds(), 0.0)
        if age_seconds <= 300:
            very_recent_hits += 1

    urgency = _bounded((urgent_hits / max(1, len(rows))) * 0.8 + (very_recent_hits / max(1, len(rows))) * 0.2)
    if urgent_hits > 0:
        urgency = max(urgency, 0.65)
    return urgency, {
        "source": "input_events",
        "sample_count": len(rows),
        "urgent_hits": urgent_hits,
        "very_recent_hits": very_recent_hits,
    }


async def _interruption_likelihood(*, human_aware: dict, db: AsyncSession) -> tuple[float, dict]:
    rows = (
        await db.execute(
            select(WorkspaceInterruptionEvent)
            .where(WorkspaceInterruptionEvent.status == "active")
            .order_by(WorkspaceInterruptionEvent.id.desc())
            .limit(100)
        )
    ).scalars().all()

    high_risk_outcomes = 0
    for item in rows:
        outcome = str(item.applied_outcome or "").strip().lower()
        if outcome in {"auto_pause", "auto_stop", "paused", "stopped", "require_operator_decision"}:
            high_risk_outcomes += 1

    human_weight = 0.0
    if bool(human_aware.get("shared_workspace_active", False)):
        human_weight += 0.35
    if bool(human_aware.get("human_near_motion_path", False)):
        human_weight += 0.35
    if bool(human_aware.get("human_near_target_zone", False)):
        human_weight += 0.2
    if bool(human_aware.get("human_in_workspace", False)):
        human_weight += 0.1

    interruption_ratio = high_risk_outcomes / max(1, len(rows)) if rows else 0.0
    score = _bounded((interruption_ratio * 0.7) + human_weight)
    return score, {
        "active_interruptions": len(rows),
        "high_risk_outcomes": high_risk_outcomes,
        "human_weight": round(human_weight, 6),
    }


async def _preferred_collaboration_mode(*, db: AsyncSession) -> str:
    row = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.in_(["collaboration_mode", "collaboration_mode:default"]))
            .order_by(UserPreference.last_updated.desc())
            .limit(20)
        )
    ).scalars().first()
    if not row:
        return "autonomous"
    value = row.value
    if isinstance(value, str) and value in COLLABORATION_MODES:
        return value
    if isinstance(value, dict):
        mode = str(value.get("mode", "")).strip().lower()
        if mode in COLLABORATION_MODES:
            return mode
    return "autonomous"


def _operator_presence_score(*, human_aware: dict) -> float:
    score = 0.0
    if bool(human_aware.get("operator_present", False)):
        score += 0.55
    if bool(human_aware.get("human_in_workspace", False)):
        score += 0.25
    if bool(human_aware.get("human_near_target_zone", False)):
        score += 0.1
    if bool(human_aware.get("shared_workspace_active", False)):
        score += 0.1
    return _bounded(score)


def _resolve_collaboration_mode(
    *,
    requested_mode: str,
    preferred_mode: str,
    human_aware: dict,
    communication_urgency: float,
    interruption_likelihood: float,
    memory_preference: dict | None = None,
    pattern_preference: dict | None = None,
) -> tuple[str, str]:
    requested = str(requested_mode or "auto").strip().lower()
    if requested in COLLABORATION_MODES:
        return requested, "requested_mode"

    if bool(human_aware.get("shared_workspace_active", False)) and bool(human_aware.get("human_in_workspace", False)):
        return "deferential", "shared_workspace_active"
    if interruption_likelihood >= 0.7:
        return "deferential", "high_interruption_likelihood"
    if bool(human_aware.get("human_near_motion_path", False)) or bool(human_aware.get("operator_present", False)):
        return "confirmation-first", "operator_or_motion_proximity"
    if communication_urgency >= 0.65:
        return "assistive", "urgent_communication_context"
    if isinstance(pattern_preference, dict):
        if bool(pattern_preference.get("influence_applied", False)):
            profile = pattern_preference.get("influence_profile", {}) if isinstance(pattern_preference.get("influence_profile", {}), dict) else {}
            suggested_mode = str(profile.get("collaboration_mode", "")).strip().lower()
            confidence = float(pattern_preference.get("effective_confidence", 0.0) or 0.0)
            if suggested_mode in COLLABORATION_MODES and confidence >= 0.78:
                return suggested_mode, "objective69_pattern_influence"
    if isinstance(memory_preference, dict):
        preferred_option = str(memory_preference.get("preferred_option_id", "")).strip()
        confidence = float(memory_preference.get("confidence", 0.0) or 0.0)
        if confidence >= 0.8 and preferred_option in {"defer_action", "rescan_first", "speak_summary_only"}:
            return "deferential", "negotiation_memory_preference"
    return preferred_mode, "operator_preference_default"


def _apply_collaboration_policy(
    *,
    mode: str,
    human_aware: dict,
    communication_urgency: float,
    interruption_likelihood: float,
    task_kind: str,
    action_risk_level: str,
    memory_preference: dict | None = None,
    pattern_preference: dict | None = None,
) -> dict:
    normalized_kind = str(task_kind or "mixed").strip().lower()
    normalized_risk = str(action_risk_level or "medium").strip().lower()
    physical = normalized_kind in {"physical", "mixed"}
    informational = normalized_kind == "informational"
    low_risk = normalized_risk == "low"

    modifiers: list[str] = []
    reprioritize_delta = 0.0
    defer_physical_action = False
    require_confirmation = False
    ask_question = False
    surface_concise_update = False

    if communication_urgency >= 0.65:
        reprioritize_delta += 0.08
        modifiers.append("urgent_communication_reprioritize")

    if mode == "assistive":
        surface_concise_update = communication_urgency >= 0.5
        if surface_concise_update:
            modifiers.append("assistive_concise_update")
        if physical and communication_urgency >= 0.75:
            defer_physical_action = True
            modifiers.append("assistive_defers_physical_for_urgent_comm")

    if mode == "confirmation-first" and physical and not low_risk:
        require_confirmation = True
        ask_question = True
        modifiers.append("confirmation_required_for_physical_action")

    if mode == "deferential" and physical and not low_risk:
        defer_physical_action = True
        modifiers.append("deferential_shared_workspace_suppression")

    if interruption_likelihood >= 0.7 and physical and not low_risk:
        defer_physical_action = True
        modifiers.append("elevated_interruption_likelihood_defer")

    if isinstance(memory_preference, dict):
        preferred_option = str(memory_preference.get("preferred_option_id", "")).strip()
        confidence = float(memory_preference.get("confidence", 0.0) or 0.0)
        if confidence >= 0.8 and preferred_option in {"defer_action", "rescan_first"} and physical:
            defer_physical_action = True
            ask_question = False
            modifiers.append("objective67_memory_prefers_deferral")
        if confidence >= 0.78 and preferred_option == "speak_summary_only":
            surface_concise_update = True
            defer_physical_action = True if physical else defer_physical_action
            ask_question = False
            modifiers.append("objective67_memory_prefers_concise_update")
        if confidence >= 0.9 and preferred_option == "continue_now" and informational and low_risk:
            modifiers.append("objective67_memory_prefers_continue")

    if isinstance(pattern_preference, dict):
        if bool(pattern_preference.get("influence_applied", False)):
            profile = pattern_preference.get("influence_profile", {}) if isinstance(pattern_preference.get("influence_profile", {}), dict) else {}
            if bool(profile.get("defer_physical_action", False)) and physical:
                defer_physical_action = True
                modifiers.append("objective69_pattern_prefers_defer_physical")
            if bool(profile.get("surface_concise_update", False)):
                surface_concise_update = True
                modifiers.append("objective69_pattern_prefers_concise_update")
            if bool(profile.get("require_confirmation", False)) and physical:
                require_confirmation = True
                ask_question = True
                modifiers.append("objective69_pattern_prefers_confirmation")

    continue_allowed = informational and low_risk
    if continue_allowed:
        modifiers.append("low_risk_informational_continue_allowed")

    return {
        "task_kind": normalized_kind,
        "action_risk_level": normalized_risk,
        "reprioritize_delta": _bounded(reprioritize_delta, lo=0.0, hi=0.2),
        "defer_physical_action": defer_physical_action,
        "require_confirmation": require_confirmation,
        "ask_question": ask_question,
        "surface_concise_update": surface_concise_update,
        "continue_allowed": continue_allowed,
        "active_modifiers": modifiers,
    }


async def _get_or_create_orchestration_question(
    *,
    actor: str,
    source: str,
    context_id: int | None,
    dependency_gaps: list[dict],
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceInquiryQuestion:
    run_id = _run_id(metadata_json)
    dedupe_key = f"orchestration_dependency_unmet:context:{int(context_id or 0)}:run:{run_id or 'na'}"

    existing = (
        await db.execute(
            select(WorkspaceInquiryQuestion)
            .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
            .where(WorkspaceInquiryQuestion.status == "open")
            .order_by(WorkspaceInquiryQuestion.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing:
        return existing

    question = WorkspaceInquiryQuestion(
        source=source,
        actor=actor,
        status="open",
        dedupe_key=dedupe_key,
        trigger_type="orchestration_dependency_unmet",
        uncertainty_type="cross_domain_dependency_gap",
        origin_strategy_goal_id=None,
        origin_strategy_id=None,
        origin_plan_id=None,
        why_answer_matters="Orchestration cannot safely continue until unresolved cross-domain dependencies are clarified.",
        waiting_decision="Choose whether to defer, replan, or escalate blocked orchestration dependencies.",
        no_answer_behavior="System remains blocked and avoids autonomous downstream execution.",
        candidate_answer_paths_json=[
            {
                "path_id": "defer",
                "label": "Defer orchestration until dependencies are satisfied",
                "effect_type": "defer",
                "params": {},
            },
            {
                "path_id": "replan",
                "label": "Replan with expanded context window",
                "effect_type": "replan",
                "params": {"lookback_hours_delta": 24},
            },
            {
                "path_id": "escalate",
                "label": "Escalate to operator with dependency report",
                "effect_type": "escalate",
                "params": {},
            },
        ],
        urgency="high",
        priority="high",
        safe_default_if_unanswered="defer",
        trigger_evidence_json={
            "dependency_gaps": dependency_gaps,
            "origin_context_id": context_id,
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective63_orchestration": True,
        },
    )
    db.add(question)
    await db.flush()
    return question


async def _get_or_create_collaboration_question(
    *,
    actor: str,
    source: str,
    context_id: int | None,
    collaboration_mode: str,
    policy: dict,
    question_priority: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceInquiryQuestion:
    run_id = _run_id(metadata_json)
    dedupe_key = f"collaboration_confirmation_required:context:{int(context_id or 0)}:mode:{collaboration_mode}:run:{run_id or 'na'}"
    existing = (
        await db.execute(
            select(WorkspaceInquiryQuestion)
            .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
            .where(WorkspaceInquiryQuestion.status == "open")
            .order_by(WorkspaceInquiryQuestion.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing:
        return existing

    question = WorkspaceInquiryQuestion(
        source=source,
        actor=actor,
        status="open",
        dedupe_key=dedupe_key,
        trigger_type="collaboration_confirmation_required",
        uncertainty_type="human_aware_collaboration",
        origin_strategy_goal_id=None,
        origin_strategy_id=None,
        origin_plan_id=None,
        why_answer_matters="Human-aware collaboration policy requires confirmation before proceeding with the current action shape.",
        waiting_decision="Choose whether to proceed now, defer action, or provide a concise status update.",
        no_answer_behavior="System defers sensitive action and surfaces concise status instead of silent progression.",
        candidate_answer_paths_json=[
            {
                "path_id": "proceed_with_confirmation",
                "label": "Proceed with explicit confirmation",
                "effect_type": "confirm_then_proceed",
                "params": {},
            },
            {
                "path_id": "defer_action",
                "label": "Defer action until human context clears",
                "effect_type": "defer",
                "params": {},
            },
            {
                "path_id": "status_update_only",
                "label": "Surface concise update and avoid physical action",
                "effect_type": "status_update",
                "params": {},
            },
        ],
        urgency="high" if str(question_priority or "normal").strip().lower() == "high" else "normal",
        priority="high" if str(question_priority or "normal").strip().lower() == "high" else "normal",
        safe_default_if_unanswered="defer_action",
        trigger_evidence_json={
            "collaboration_mode": collaboration_mode,
            "policy": policy,
            "origin_context_id": context_id,
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective64_human_aware_collaboration": True,
        },
    )
    db.add(question)
    await db.flush()
    return question


def _negotiation_triggers(
    *,
    collaboration_mode: str,
    preferred_mode: str,
    human_aware: dict,
    communication_urgency: float,
    task_kind: str,
    action_risk_level: str,
    collaboration_policy: dict,
) -> list[dict]:
    triggers: list[dict] = []
    physical = str(task_kind or "mixed").strip().lower() == "physical"
    risk = str(action_risk_level or "medium").strip().lower()
    operator_present = bool(human_aware.get("operator_present", False))
    shared_workspace_active = bool(human_aware.get("shared_workspace_active", False))

    if collaboration_mode == "deferential":
        triggers.append(
            {
                "trigger_type": "deferential_mode_requires_human_decision",
                "reason": "collaboration mode is deferential",
            }
        )

    if physical and preferred_mode == "autonomous" and communication_urgency >= 0.65:
        triggers.append(
            {
                "trigger_type": "urgent_communication_vs_autonomous_physical_conflict",
                "reason": "urgent communication conflicts with autonomous physical action",
            }
        )

    if physical and shared_workspace_active and bool(collaboration_policy.get("defer_physical_action", False)):
        triggers.append(
            {
                "trigger_type": "shared_workspace_suppressed_preferred_action",
                "reason": "shared workspace suppresses preferred action",
            }
        )

    if operator_present and risk in {"medium", "high"}:
        triggers.append(
            {
                "trigger_type": "operator_presence_raises_decision_significance",
                "reason": "operator presence raises decision significance",
            }
        )

    return triggers


def _negotiation_options(
    *,
    task_kind: str,
    action_risk_level: str,
    communication_urgency: float,
    human_aware: dict,
) -> list[dict]:
    physical = str(task_kind or "mixed").strip().lower() == "physical"
    risk = str(action_risk_level or "medium").strip().lower()
    shared_workspace_active = bool(human_aware.get("shared_workspace_active", False))
    operator_present = bool(human_aware.get("operator_present", False))

    options: list[dict] = []
    if not (physical and shared_workspace_active and risk in {"medium", "high"}):
        options.append(
            {
                "option_id": "continue_now",
                "label": "Continue now",
                "description": "Continue the current orchestration path now with current safeguards.",
                "effect": "set_orchestration_active",
                "safety_class": "guarded_continue",
            }
        )

    options.append(
        {
            "option_id": "defer_action",
            "label": "Defer action",
            "description": "Defer sensitive action until human-context constraints clear.",
            "effect": "defer_orchestration",
            "safety_class": "safe_default",
        }
    )

    if physical:
        options.append(
            {
                "option_id": "rescan_first",
                "label": "Rescan first",
                "description": "Rescan workspace context before deciding the next physical step.",
                "effect": "require_replan_rescan",
                "safety_class": "verification_first",
            }
        )

    if communication_urgency >= 0.5:
        options.append(
            {
                "option_id": "speak_summary_only",
                "label": "Speak summary only",
                "description": "Surface a concise update now and hold physical execution.",
                "effect": "status_update_only",
                "safety_class": "communication_first",
            }
        )

    if operator_present:
        options.append(
            {
                "option_id": "request_confirmation_later",
                "label": "Request confirmation later",
                "description": "Schedule explicit confirmation before final execution commitment.",
                "effect": "confirmation_follow_up",
                "safety_class": "human_confirmation",
            }
        )

    deduped: list[dict] = []
    seen: set[str] = set()
    for option in options:
        option_id = str(option.get("option_id", "")).strip()
        if not option_id or option_id in seen:
            continue
        if option_id not in NEGOTIATION_OPTION_IDS:
            continue
        seen.add(option_id)
        deduped.append(option)
    return deduped


def _default_safe_path(
    *,
    options: list[dict],
    task_kind: str,
    action_risk_level: str,
    human_aware: dict,
    communication_urgency: float,
    preferred_default_option: str = "",
) -> str:
    option_ids = {str(item.get("option_id", "")).strip() for item in options if isinstance(item, dict)}
    physical = str(task_kind or "mixed").strip().lower() == "physical"
    risk = str(action_risk_level or "medium").strip().lower()

    preferred = str(preferred_default_option or "").strip()
    if preferred and preferred in option_ids:
        return preferred

    if physical and bool(human_aware.get("shared_workspace_active", False)) and "defer_action" in option_ids:
        return "defer_action"
    if physical and communication_urgency >= 0.65 and "speak_summary_only" in option_ids:
        return "speak_summary_only"
    if risk == "high" and "rescan_first" in option_ids:
        return "rescan_first"
    if "defer_action" in option_ids:
        return "defer_action"
    return "continue_now" if "continue_now" in option_ids else next(iter(option_ids), "defer_action")


async def _create_collaboration_negotiation(
    *,
    actor: str,
    source: str,
    origin_context_id: int | None,
    origin_goal_id: int | None,
    origin_horizon_plan_id: int | None,
    trigger: dict,
    requested_decision: str,
    options_presented: list[dict],
    default_safe_path: str,
    human_context_state: dict,
    explainability: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceCollaborationNegotiation:
    row = WorkspaceCollaborationNegotiation(
        source=source,
        actor=actor,
        status="open",
        resolution_status="pending",
        origin_orchestration_id=None,
        origin_context_id=origin_context_id,
        origin_goal_id=origin_goal_id,
        origin_horizon_plan_id=origin_horizon_plan_id,
        trigger_type=str(trigger.get("trigger_type", "human_context_conflict")),
        trigger_reason=str(trigger.get("reason", "human-aware collaboration negotiation required")),
        requested_decision=requested_decision,
        options_presented_json=options_presented,
        default_safe_path=default_safe_path,
        human_context_state_json=human_context_state if isinstance(human_context_state, dict) else {},
        explainability_json=explainability if isinstance(explainability, dict) else {},
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective65_human_aware_collaboration_negotiation": True,
            "policy_version": NEGOTIATION_POLICY_VERSION,
        },
    )
    db.add(row)
    await db.flush()
    return row


def _negotiation_pattern_key(*, trigger_type: str, human_context_state: dict) -> str:
    state = human_context_state if isinstance(human_context_state, dict) else {}
    signals = state.get("signals", {}) if isinstance(state.get("signals", {}), dict) else {}
    urgency = float(state.get("communication_urgency", 0.0) or 0.0)
    urgency_bucket = "high" if urgency >= 0.65 else ("medium" if urgency >= 0.35 else "low")
    environment_profile = str(state.get("environment_profile", "default")).strip().lower() or "default"
    parts = [
        "collaboration_negotiation",
        str(state.get("task_kind", "mixed")).strip().lower(),
        str(state.get("action_risk_level", "medium")).strip().lower(),
        f"shared:{bool(signals.get('shared_workspace_active', False))}",
        f"operator:{bool(signals.get('operator_present', False))}",
        f"urgency:{urgency_bucket}",
        f"env:{environment_profile}",
    ]
    return "|".join(parts)


async def _load_negotiation_patterns(*, db: AsyncSession) -> dict:
    payload = await get_user_preference_value(
        db=db,
        preference_type=NEGOTIATION_PATTERN_PREFERENCE_TYPE,
        user_id=DEFAULT_USER_ID,
    )
    if isinstance(payload, dict):
        return payload
    return {"version": "objective66-v1", "patterns": {}}


async def _load_negotiation_memory(*, db: AsyncSession) -> dict:
    payload = await get_user_preference_value(
        db=db,
        preference_type=NEGOTIATION_MEMORY_PREFERENCE_TYPE,
        user_id=DEFAULT_USER_ID,
    )
    if isinstance(payload, dict):
        return payload
    return {
        "version": "objective68-v1",
        "patterns": {},
    }


def _memory_preference_for_pattern(*, memory_payload: dict, pattern_key: str, option_ids: set[str] | None = None) -> dict:
    patterns = memory_payload.get("patterns", {}) if isinstance(memory_payload.get("patterns", {}), dict) else {}
    item = patterns.get(pattern_key, {}) if isinstance(patterns.get(pattern_key, {}), dict) else {}
    state = str(item.get("state", "learning")).strip()
    evidence_count = int(item.get("evidence_count", 0) or 0)
    confidence = float(item.get("confidence", 0.0) or 0.0)
    preferred_option_id = str(item.get("dominant_option_id", "")).strip()
    last_updated_at = str(item.get("last_updated_at", "")).strip()
    decay_factor, age_days, is_stale = _negotiation_memory_decay(last_updated_at=last_updated_at)
    effective_confidence = round(_bounded(confidence * decay_factor), 6)
    effective_evidence = int(max(0, round(evidence_count * decay_factor)))
    effective_state = "learning" if is_stale else state
    if option_ids is not None and preferred_option_id and preferred_option_id not in option_ids:
        preferred_option_id = ""

    return {
        "pattern_key": pattern_key,
        "state": effective_state,
        "raw_state": state,
        "evidence_count": effective_evidence,
        "raw_evidence_count": evidence_count,
        "confidence": effective_confidence,
        "raw_confidence": confidence,
        "preferred_option_id": preferred_option_id,
        "option_counts": item.get("option_counts", {}) if isinstance(item.get("option_counts", {}), dict) else {},
        "source_interactions": item.get("source_interactions", []) if isinstance(item.get("source_interactions", []), list) else [],
        "freshness": "stale" if is_stale else "fresh",
        "is_stale": is_stale,
        "age_days": round(age_days, 6),
        "decay_factor": round(decay_factor, 6),
        "decay_applied": bool(decay_factor < 0.999999),
        "context_match_score": 1.0,
        "last_updated_at": last_updated_at,
    }


def _consolidated_option_from_memory(*, memory_preference: dict, option_ids: set[str]) -> tuple[str | None, float, int]:
    preferred_option_id = str(memory_preference.get("preferred_option_id", "")).strip()
    confidence = float(memory_preference.get("confidence", 0.0) or 0.0)
    evidence_count = int(memory_preference.get("evidence_count", 0) or 0)
    state = str(memory_preference.get("state", "learning")).strip()
    if state != "consolidated":
        return None, confidence, evidence_count
    if evidence_count < NEGOTIATION_MEMORY_MIN_EVIDENCE:
        return None, confidence, evidence_count
    if confidence < NEGOTIATION_MEMORY_MIN_CONFIDENCE:
        return None, confidence, evidence_count
    if preferred_option_id not in option_ids:
        return None, confidence, evidence_count
    return preferred_option_id, confidence, evidence_count


def _recommended_option_from_patterns(*, pattern_payload: dict, pattern_key: str, option_ids: set[str]) -> tuple[str | None, float, int]:
    patterns = pattern_payload.get("patterns", {}) if isinstance(pattern_payload.get("patterns", {}), dict) else {}
    item = patterns.get(pattern_key, {}) if isinstance(patterns.get(pattern_key, {}), dict) else {}
    total = int(item.get("total", 0) or 0)
    counts = item.get("counts", {}) if isinstance(item.get("counts", {}), dict) else {}
    if total < NEGOTIATION_PATTERN_MIN_REUSE_COUNT:
        return None, 0.0, total

    best_option = ""
    best_count = 0
    for option_id, raw_count in counts.items():
        candidate = str(option_id or "").strip()
        count = int(raw_count or 0)
        if candidate in option_ids and count > best_count:
            best_option = candidate
            best_count = count
    if not best_option:
        return None, 0.0, total

    confidence = float(best_count / max(1, total))
    if confidence < NEGOTIATION_PATTERN_MIN_CONFIDENCE:
        return None, confidence, total
    return best_option, confidence, total


def _collaboration_pattern_type(*, human_context_state: dict, dominant_option: str) -> str:
    state = human_context_state if isinstance(human_context_state, dict) else {}
    task_kind = str(state.get("task_kind", "mixed")).strip().lower()
    urgency = float(state.get("communication_urgency", 0.0) or 0.0)
    signals = state.get("signals", {}) if isinstance(state.get("signals", {}), dict) else {}
    shared_workspace_active = bool(signals.get("shared_workspace_active", False))
    occupied_zones = signals.get("occupied_zones", []) if isinstance(signals.get("occupied_zones", []), list) else []

    if shared_workspace_active and dominant_option in {"defer_action", "rescan_first"}:
        return "shared_workspace_deferential_preference"
    if urgency >= 0.65 and dominant_option == "speak_summary_only":
        return "urgent_communication_override"
    if task_kind == "physical" and bool(occupied_zones) and dominant_option in {"defer_action", "rescan_first", "request_confirmation_later"}:
        return "occupied_zone_physical_postponement"
    return "contextual_collaboration_preference"


def _collaboration_pattern_domains(*, human_context_state: dict, dominant_option: str) -> list[str]:
    state = human_context_state if isinstance(human_context_state, dict) else {}
    task_kind = str(state.get("task_kind", "mixed")).strip().lower()
    urgency = float(state.get("communication_urgency", 0.0) or 0.0)

    domains = {"collaboration", "orchestration"}
    if task_kind in {"physical", "mixed"}:
        domains.add("workspace")
    if urgency >= 0.65:
        domains.add("communication")
    if dominant_option in {"defer_action", "rescan_first"}:
        domains.add("autonomy")
    if dominant_option in {"request_confirmation_later", "rescan_first"}:
        domains.add("inquiry")
    return sorted(list(domains))


def _collaboration_pattern_influence_profile(*, dominant_outcome: str) -> dict:
    profile = {
        "preferred_default_option": dominant_outcome,
        "collaboration_mode": "autonomous",
        "defer_physical_action": False,
        "surface_concise_update": False,
        "require_confirmation": False,
        "autonomy_suppression": False,
        "question_priority": "normal",
    }
    if dominant_outcome == "defer_action":
        profile.update(
            {
                "collaboration_mode": "deferential",
                "defer_physical_action": True,
                "autonomy_suppression": True,
                "question_priority": "high",
            }
        )
    elif dominant_outcome == "rescan_first":
        profile.update(
            {
                "collaboration_mode": "deferential",
                "defer_physical_action": True,
                "require_confirmation": True,
                "autonomy_suppression": True,
                "question_priority": "high",
            }
        )
    elif dominant_outcome == "speak_summary_only":
        profile.update(
            {
                "collaboration_mode": "assistive",
                "defer_physical_action": True,
                "surface_concise_update": True,
                "autonomy_suppression": True,
                "question_priority": "high",
            }
        )
    elif dominant_outcome == "request_confirmation_later":
        profile.update(
            {
                "collaboration_mode": "confirmation-first",
                "require_confirmation": True,
                "question_priority": "high",
            }
        )
    return profile


def _collaboration_pattern_outcome_quality(*, source_interactions: list[dict]) -> float:
    if not source_interactions:
        return 0.0
    successful = 0
    for item in source_interactions:
        if not isinstance(item, dict):
            continue
        status = str(item.get("resolution_status", "")).strip()
        if status in {"operator_selected", "reused_prior_pattern"}:
            successful += 1
    return _bounded(successful / max(1, len(source_interactions)))


async def _upsert_collaboration_pattern_abstraction(
    *,
    context_signature: str,
    human_context_state: dict,
    memory_item: dict,
    actor: str,
    db: AsyncSession,
) -> WorkspaceCollaborationPattern | None:
    signature = str(context_signature or "").strip()
    if not signature:
        return None

    evidence_count = int(memory_item.get("evidence_count", 0) or 0)
    confidence = float(memory_item.get("confidence", 0.0) or 0.0)
    dominant_outcome = str(memory_item.get("dominant_option_id", "")).strip()
    if not dominant_outcome:
        return None

    pattern_type = _collaboration_pattern_type(
        human_context_state=human_context_state,
        dominant_option=dominant_outcome,
    )
    affected_domains = _collaboration_pattern_domains(
        human_context_state=human_context_state,
        dominant_option=dominant_outcome,
    )
    source_interactions = memory_item.get("source_interactions", []) if isinstance(memory_item.get("source_interactions", []), list) else []
    outcome_quality = _collaboration_pattern_outcome_quality(source_interactions=source_interactions)
    last_updated_at = str(memory_item.get("last_updated_at", "")).strip()
    decay_factor, age_days, is_stale = _negotiation_memory_decay(last_updated_at=last_updated_at)

    if is_stale:
        status = "stale"
    elif evidence_count >= NEGOTIATION_ABSTRACTION_MIN_EVIDENCE and confidence >= NEGOTIATION_ABSTRACTION_MIN_CONFIDENCE:
        status = "consolidated"
    else:
        status = "learning"

    evidence_summary = (
        f"Observed {evidence_count} similar negotiations; dominant outcome '{dominant_outcome}' "
        f"with confidence={confidence:.2f} and outcome_quality={outcome_quality:.2f}."
    )
    influence_profile = _collaboration_pattern_influence_profile(dominant_outcome=dominant_outcome)

    row = (
        await db.execute(
            select(WorkspaceCollaborationPattern)
            .where(WorkspaceCollaborationPattern.context_signature == signature)
            .order_by(WorkspaceCollaborationPattern.id.desc())
            .limit(1)
        )
    ).scalars().first()

    now = datetime.now(timezone.utc)
    if not row:
        row = WorkspaceCollaborationPattern(
            source="objective69",
            actor=actor,
            pattern_type=pattern_type,
            context_signature=signature,
            evidence_count=evidence_count,
            confidence=round(confidence, 6),
            dominant_outcome=dominant_outcome,
            affected_domains_json=affected_domains,
            status=status,
            evidence_summary=evidence_summary,
            explainability_json={},
            influence_profile_json=influence_profile,
            last_observed_at=now,
            metadata_json={},
        )
        db.add(row)
        await db.flush()

    prior_status = str(row.status or "learning").strip()
    row.pattern_type = pattern_type
    row.evidence_count = evidence_count
    row.confidence = round(confidence, 6)
    row.dominant_outcome = dominant_outcome
    row.affected_domains_json = affected_domains
    row.evidence_summary = evidence_summary
    row.influence_profile_json = influence_profile
    row.last_observed_at = now
    row.status = "acknowledged" if prior_status == "acknowledged" and status in {"consolidated", "learning"} else status
    row.explainability_json = {
        "policy_version": NEGOTIATION_ABSTRACTION_POLICY_VERSION,
        "context_signature": signature,
        "decay_factor": round(decay_factor, 6),
        "age_days": round(age_days, 6),
        "is_stale": is_stale,
        "outcome_quality": round(outcome_quality, 6),
        "evidence_threshold": NEGOTIATION_ABSTRACTION_MIN_EVIDENCE,
        "confidence_threshold": NEGOTIATION_ABSTRACTION_MIN_CONFIDENCE,
        "source_interaction_count": len([item for item in source_interactions if isinstance(item, dict)]),
    }
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "last_updated_at": now.isoformat(),
        "abstraction_source": "objective69",
    }
    return row


def _collaboration_pattern_freshness(*, row: WorkspaceCollaborationPattern) -> tuple[str, float, float]:
    decay_factor, age_days, is_stale = _negotiation_memory_decay(last_updated_at=row.last_observed_at)
    freshness = "stale" if is_stale else "fresh"
    return freshness, round(decay_factor, 6), round(age_days, 6)


async def _pattern_influence_for_signature(
    *,
    context_signature: str,
    option_ids: set[str],
    db: AsyncSession,
) -> dict:
    signature = str(context_signature or "").strip()
    if not signature:
        return {}
    row = (
        await db.execute(
            select(WorkspaceCollaborationPattern)
            .where(WorkspaceCollaborationPattern.context_signature == signature)
            .order_by(WorkspaceCollaborationPattern.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if not row:
        return {}

    freshness, decay_factor, age_days = _collaboration_pattern_freshness(row=row)
    effective_confidence = _bounded(float(row.confidence or 0.0) * decay_factor)
    preferred_option = str(row.dominant_outcome or "").strip()
    status = str(row.status or "learning").strip()

    if freshness == "stale":
        return {
            "pattern_id": int(row.id),
            "context_signature": signature,
            "influence_applied": False,
            "reason": "stale_pattern",
            "freshness": freshness,
            "decay_factor": decay_factor,
            "age_days": age_days,
        }
    if status not in {"consolidated", "acknowledged"}:
        return {
            "pattern_id": int(row.id),
            "context_signature": signature,
            "influence_applied": False,
            "reason": "pattern_not_consolidated",
            "freshness": freshness,
            "decay_factor": decay_factor,
            "age_days": age_days,
        }
    if effective_confidence < NEGOTIATION_ABSTRACTION_MIN_CONFIDENCE:
        return {
            "pattern_id": int(row.id),
            "context_signature": signature,
            "influence_applied": False,
            "reason": "pattern_confidence_below_threshold",
            "freshness": freshness,
            "decay_factor": decay_factor,
            "age_days": age_days,
        }

    profile = row.influence_profile_json if isinstance(row.influence_profile_json, dict) else {}
    preferred_default_option = str(profile.get("preferred_default_option", preferred_option)).strip()
    if option_ids and preferred_default_option and preferred_default_option not in option_ids:
        preferred_default_option = ""

    return {
        "pattern_id": int(row.id),
        "context_signature": signature,
        "pattern_type": str(row.pattern_type or ""),
        "preferred_default_option": preferred_default_option,
        "effective_confidence": round(effective_confidence, 6),
        "evidence_count": int(row.evidence_count or 0),
        "dominant_outcome": preferred_option,
        "affected_domains": row.affected_domains_json if isinstance(row.affected_domains_json, list) else [],
        "influence_profile": profile,
        "influence_applied": bool(preferred_default_option),
        "reason": "matched_context_signature" if preferred_default_option else "dominant_option_not_available",
        "freshness": freshness,
        "decay_factor": decay_factor,
        "age_days": age_days,
    }


async def _record_negotiation_pattern_signal(
    *,
    trigger_type: str,
    human_context_state: dict,
    selected_option_id: str,
    actor: str,
    resolution_status: str = "",
    negotiation_id: int | None = None,
    db: AsyncSession,
) -> None:
    pattern_key = _negotiation_pattern_key(trigger_type=trigger_type, human_context_state=human_context_state)
    payload = await _load_negotiation_patterns(db=db)
    patterns = payload.get("patterns", {}) if isinstance(payload.get("patterns", {}), dict) else {}
    item = patterns.get(pattern_key, {}) if isinstance(patterns.get(pattern_key, {}), dict) else {}
    counts = item.get("counts", {}) if isinstance(item.get("counts", {}), dict) else {}
    option_id = str(selected_option_id or "").strip()
    if not option_id:
        return

    counts[option_id] = int(counts.get(option_id, 0) or 0) + 1
    total = int(item.get("total", 0) or 0) + 1
    patterns[pattern_key] = {
        "pattern_key": pattern_key,
        "counts": counts,
        "total": total,
        "last_option": option_id,
        "last_actor": actor,
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "version": "objective66-v1",
        "patterns": patterns,
    }
    confidence = min(1.0, 0.25 + (total / 20.0))
    await upsert_user_preference(
        db=db,
        preference_type=NEGOTIATION_PATTERN_PREFERENCE_TYPE,
        value=payload,
        confidence=confidence,
        source="learning",
        user_id=DEFAULT_USER_ID,
    )

    memory_payload = await _load_negotiation_memory(db=db)
    memory_patterns = memory_payload.get("patterns", {}) if isinstance(memory_payload.get("patterns", {}), dict) else {}
    memory_item = memory_patterns.get(pattern_key, {}) if isinstance(memory_patterns.get(pattern_key, {}), dict) else {}
    memory_counts = memory_item.get("option_counts", {}) if isinstance(memory_item.get("option_counts", {}), dict) else {}
    memory_counts[option_id] = int(memory_counts.get(option_id, 0) or 0) + 1
    evidence_count = int(memory_item.get("evidence_count", 0) or 0) + 1
    dominant_option = ""
    dominant_count = 0
    for candidate, candidate_count in memory_counts.items():
        normalized = str(candidate or "").strip()
        count = int(candidate_count or 0)
        if count > dominant_count:
            dominant_option = normalized
            dominant_count = count
    ratio = float(dominant_count / max(1, evidence_count))
    scaled_confidence = _bounded(ratio)

    prior_state = str(memory_item.get("state", "learning")).strip()
    new_state = "learning"
    if evidence_count >= NEGOTIATION_MEMORY_MIN_EVIDENCE and ratio >= NEGOTIATION_MEMORY_MIN_CONFIDENCE:
        new_state = "consolidated"
    elif prior_state == "consolidated" and ratio < NEGOTIATION_MEMORY_REVISION_FLOOR:
        new_state = "learning"

    source_interactions = memory_item.get("source_interactions", []) if isinstance(memory_item.get("source_interactions", []), list) else []
    source_interactions = [
        item
        for item in source_interactions
        if isinstance(item, dict)
    ]
    source_interactions.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "selected_option_id": option_id,
            "actor": actor,
            "resolution_status": str(resolution_status or ""),
            "negotiation_id": int(negotiation_id) if negotiation_id is not None else None,
        }
    )
    source_interactions = source_interactions[-NEGOTIATION_MEMORY_MAX_INTERACTIONS:]

    memory_patterns[pattern_key] = {
        "pattern_key": pattern_key,
        "state": new_state,
        "evidence_count": evidence_count,
        "option_counts": memory_counts,
        "dominant_option_id": dominant_option,
        "confidence": round(scaled_confidence, 6),
        "source_interactions": source_interactions,
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    }

    memory_payload = {
        "version": "objective68-v1",
        "patterns": memory_patterns,
    }
    await upsert_user_preference(
        db=db,
        preference_type=NEGOTIATION_MEMORY_PREFERENCE_TYPE,
        value=memory_payload,
        confidence=min(1.0, 0.2 + (evidence_count / 20.0)),
        source="learning",
        user_id=DEFAULT_USER_ID,
    )

    await _upsert_collaboration_pattern_abstraction(
        context_signature=pattern_key,
        human_context_state=human_context_state,
        memory_item=memory_patterns.get(pattern_key, {}) if isinstance(memory_patterns.get(pattern_key, {}), dict) else {},
        actor=actor,
        db=db,
    )


async def inspect_negotiation_preferences(
    *,
    pattern_key: str,
    limit: int,
    db: AsyncSession,
) -> dict:
    memory_payload = await _load_negotiation_memory(db=db)
    patterns = memory_payload.get("patterns", {}) if isinstance(memory_payload.get("patterns", {}), dict) else {}
    selected: dict[str, dict]
    if pattern_key.strip():
        key = pattern_key.strip()
        selected = {key: patterns.get(key, {}) if isinstance(patterns.get(key, {}), dict) else {}}
    else:
        selected = {
            key: value
            for key, value in patterns.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    enriched: list[dict] = []
    for key, item in selected.items():
        if not isinstance(item, dict):
            continue
        memory_item = _memory_preference_for_pattern(
            memory_payload=memory_payload,
            pattern_key=key,
            option_ids=None,
        )
        memory_item["context_match_score"] = 1.0 if pattern_key.strip() and key == pattern_key.strip() else 0.0
        enriched.append(memory_item)

    ordered = sorted(
        enriched,
        key=lambda item: (
            float(item.get("confidence", 0.0) or 0.0),
            int(item.get("evidence_count", 0) or 0),
            str(item.get("last_updated_at", "")),
        ),
        reverse=True,
    )
    trimmed = ordered[: max(1, min(500, int(limit)))]
    consolidated = [
        {
            "pattern_key": str(item.get("pattern_key", "")),
            "state": str(item.get("state", "learning")),
            "raw_state": str(item.get("raw_state", "learning")),
            "preferred_option_id": str(item.get("preferred_option_id", "")),
            "confidence": float(item.get("confidence", 0.0) or 0.0),
            "raw_confidence": float(item.get("raw_confidence", 0.0) or 0.0),
            "evidence_count": int(item.get("evidence_count", 0) or 0),
            "raw_evidence_count": int(item.get("raw_evidence_count", 0) or 0),
            "option_counts": item.get("option_counts", {}) if isinstance(item.get("option_counts", {}), dict) else {},
            "source_interactions": item.get("source_interactions", []) if isinstance(item.get("source_interactions", []), list) else [],
            "last_updated_at": item.get("last_updated_at", ""),
            "freshness": str(item.get("freshness", "fresh")),
            "decay_applied": bool(item.get("decay_applied", False)),
            "decay_factor": float(item.get("decay_factor", 1.0) or 1.0),
            "age_days": float(item.get("age_days", 0.0) or 0.0),
            "context_match_score": float(item.get("context_match_score", 0.0) or 0.0),
        }
        for item in trimmed
    ]
    return {
        "policy_version": "objective68-negotiation-memory-v1",
        "thresholds": {
            "min_evidence_for_consolidation": NEGOTIATION_MEMORY_MIN_EVIDENCE,
            "min_confidence_for_consolidation": NEGOTIATION_MEMORY_MIN_CONFIDENCE,
            "revision_floor": NEGOTIATION_MEMORY_REVISION_FLOOR,
            "decay_half_life_days": NEGOTIATION_MEMORY_DECAY_HALF_LIFE_DAYS,
            "stale_after_days": NEGOTIATION_MEMORY_STALE_AFTER_DAYS,
        },
        "preferences": consolidated,
    }


def to_collaboration_pattern_out(row: WorkspaceCollaborationPattern, *, context_signature: str = "") -> dict:
    freshness, decay_factor, age_days = _collaboration_pattern_freshness(row=row)
    effective_confidence = _bounded(float(row.confidence or 0.0) * decay_factor)
    expected_signature = str(context_signature or "").strip()
    row_signature = str(row.context_signature or "").strip()
    return {
        "pattern_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "pattern_type": row.pattern_type,
        "context_signature": row_signature,
        "evidence_count": int(row.evidence_count or 0),
        "confidence": round(effective_confidence, 6),
        "raw_confidence": float(row.confidence or 0.0),
        "dominant_outcome": row.dominant_outcome,
        "affected_domains": row.affected_domains_json if isinstance(row.affected_domains_json, list) else [],
        "status": row.status,
        "evidence_summary": row.evidence_summary,
        "freshness": freshness,
        "decay_factor": decay_factor,
        "age_days": age_days,
        "context_match_score": 1.0 if expected_signature and expected_signature == row_signature else 0.0,
        "explainability": row.explainability_json if isinstance(row.explainability_json, dict) else {},
        "influence_profile": row.influence_profile_json if isinstance(row.influence_profile_json, dict) else {},
        "acknowledged_by": row.acknowledged_by,
        "acknowledged_at": row.acknowledged_at,
        "last_observed_at": row.last_observed_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


async def list_collaboration_patterns(
    *,
    status: str,
    pattern_type: str,
    context_signature: str,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceCollaborationPattern]:
    rows = (
        await db.execute(
            select(WorkspaceCollaborationPattern)
            .order_by(WorkspaceCollaborationPattern.id.desc())
        )
    ).scalars().all()

    filtered = rows
    if status.strip():
        requested = status.strip().lower()
        filtered = [item for item in filtered if str(item.status or "").strip().lower() == requested]
    if pattern_type.strip():
        requested = pattern_type.strip().lower()
        filtered = [item for item in filtered if str(item.pattern_type or "").strip().lower() == requested]
    if context_signature.strip():
        requested = context_signature.strip()
        filtered = [item for item in filtered if str(item.context_signature or "").strip() == requested]

    return filtered[: max(1, min(500, int(limit)))]


async def get_collaboration_pattern(*, pattern_id: int, db: AsyncSession) -> WorkspaceCollaborationPattern | None:
    return (
        await db.execute(
            select(WorkspaceCollaborationPattern).where(WorkspaceCollaborationPattern.id == pattern_id)
        )
    ).scalars().first()


async def acknowledge_collaboration_pattern(
    *,
    pattern_id: int,
    actor: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceCollaborationPattern:
    row = await get_collaboration_pattern(pattern_id=pattern_id, db=db)
    if not row:
        raise ValueError("collaboration_pattern_not_found")

    row.status = "acknowledged"
    row.acknowledged_by = actor
    row.acknowledged_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "acknowledge": {
            "actor": actor,
            "reason": reason,
            "metadata_json": metadata_json if isinstance(metadata_json, dict) else {},
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    await db.flush()
    return row


async def inspect_collaboration_patterns(
    *,
    status: str,
    pattern_type: str,
    context_signature: str,
    limit: int,
    db: AsyncSession,
) -> dict:
    rows = await list_collaboration_patterns(
        status=status,
        pattern_type=pattern_type,
        context_signature=context_signature,
        limit=limit,
        db=db,
    )
    return {
        "policy_version": NEGOTIATION_ABSTRACTION_POLICY_VERSION,
        "thresholds": {
            "min_evidence_for_abstraction": NEGOTIATION_ABSTRACTION_MIN_EVIDENCE,
            "min_confidence_for_abstraction": NEGOTIATION_ABSTRACTION_MIN_CONFIDENCE,
            "decay_half_life_days": NEGOTIATION_MEMORY_DECAY_HALF_LIFE_DAYS,
            "stale_after_days": NEGOTIATION_MEMORY_STALE_AFTER_DAYS,
        },
        "patterns": [
            to_collaboration_pattern_out(item, context_signature=context_signature)
            for item in rows
        ],
    }


async def _apply_negotiation_follow_through(
    *,
    negotiation: WorkspaceCollaborationNegotiation,
    selected_option_id: str,
    db: AsyncSession,
) -> dict:
    follow_through = {
        "selected_option_id": selected_option_id,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "updated_horizon_plan": False,
    }
    if negotiation.origin_horizon_plan_id is None:
        return follow_through

    plan = await db.get(WorkspaceHorizonPlan, int(negotiation.origin_horizon_plan_id))
    if not plan:
        return follow_through

    plan.metadata_json = {
        **(plan.metadata_json if isinstance(plan.metadata_json, dict) else {}),
        "negotiation_follow_through": {
            "negotiation_id": int(negotiation.id),
            "selected_option_id": selected_option_id,
            "resolution_status": str(negotiation.resolution_status or ""),
            "trigger_type": str(negotiation.trigger_type or ""),
            "applied_at": follow_through["applied_at"],
        },
    }
    follow_through["updated_horizon_plan"] = True
    follow_through["horizon_plan_id"] = int(plan.id)
    return follow_through


async def build_cross_domain_task_orchestration(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_items_per_domain: int,
    min_context_confidence: float,
    min_domains_required: int,
    dependency_resolution_policy: str,
    collaboration_mode_preference: str,
    task_kind: str,
    action_risk_level: str,
    communication_urgency_override: float | None,
    use_human_aware_signals: bool,
    generate_goal: bool,
    generate_horizon_plan: bool,
    generate_improvement_proposals: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceTaskOrchestration, WorkspaceCrossDomainReasoningContext]:
    policy = str(dependency_resolution_policy or "ask").strip().lower()
    if policy not in ORCHESTRATION_POLICIES:
        policy = "ask"

    run_id = _run_id(metadata_json)
    context = await _latest_reasoning_context(run_id=run_id, db=db)
    if not context:
        context = await build_cross_domain_reasoning_context(
            actor=actor,
            source=source,
            lookback_hours=max(1, int(lookback_hours)),
            max_items_per_domain=max(1, min(200, int(max_items_per_domain))),
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective63_orchestration_context_build": True,
            },
            db=db,
        )

    context_out = to_cross_domain_reasoning_out(context)
    context_confidence = _bounded(float(context_out.get("confidence", 0.0)))
    signals = _domain_signals(context_out)
    contributing_domains = [key for key, value in signals.items() if int(value) > 0]
    reasoning = context_out.get("reasoning", {}) if isinstance(context_out.get("reasoning", {}), dict) else {}

    dependency_gaps = _dependency_gaps(
        context_confidence=context_confidence,
        contributing_domains=contributing_domains,
        min_context_confidence=_bounded(min_context_confidence),
        min_domains_required=max(1, int(min_domains_required)),
        context_reasoning=reasoning,
    )

    priority_score = _priority_score(context_confidence=context_confidence, signals=signals)
    human_aware = await _human_aware_state(db=db)
    if not bool(use_human_aware_signals):
        human_aware = _default_human_aware_state()
    communication_urgency, communication_details = await _communication_urgency(
        lookback_hours=lookback_hours,
        run_id=run_id,
        override=communication_urgency_override,
        db=db,
    )
    interruption_likelihood, interruption_details = await _interruption_likelihood(human_aware=human_aware, db=db)
    operator_presence_score = _operator_presence_score(human_aware=human_aware)
    preferred_mode = await _preferred_collaboration_mode(db=db)
    environment_profile = str((metadata_json if isinstance(metadata_json, dict) else {}).get("environment_profile", "default")).strip().lower() or "default"
    memory_pattern_key = _negotiation_pattern_key(
        trigger_type="",
        human_context_state={
            "task_kind": task_kind,
            "action_risk_level": action_risk_level,
            "environment_profile": environment_profile,
            "communication_urgency": communication_urgency,
            "signals": human_aware,
        },
    )
    pattern_influence = await _pattern_influence_for_signature(
        context_signature=memory_pattern_key,
        option_ids=set(),
        db=db,
    )
    memory_payload = await _load_negotiation_memory(db=db)
    memory_preference = _memory_preference_for_pattern(
        memory_payload=memory_payload,
        pattern_key=memory_pattern_key,
        option_ids=None,
    )
    if str(memory_preference.get("freshness", "fresh")).strip() == "stale" or str(memory_preference.get("state", "learning")).strip() != "consolidated":
        pattern_influence = {
            **(pattern_influence if isinstance(pattern_influence, dict) else {}),
            "influence_applied": False,
            "reason": "objective68_memory_gate",
        }
    collaboration_mode, collaboration_mode_reason = _resolve_collaboration_mode(
        requested_mode=collaboration_mode_preference,
        preferred_mode=preferred_mode,
        human_aware=human_aware,
        communication_urgency=communication_urgency,
        interruption_likelihood=interruption_likelihood,
        memory_preference=memory_preference,
        pattern_preference=pattern_influence,
    )
    collaboration_policy = _apply_collaboration_policy(
        mode=collaboration_mode,
        human_aware=human_aware,
        communication_urgency=communication_urgency,
        interruption_likelihood=interruption_likelihood,
        task_kind=task_kind,
        action_risk_level=action_risk_level,
        memory_preference=memory_preference,
        pattern_preference=pattern_influence,
    )
    priority_score = _bounded(priority_score + float(collaboration_policy.get("reprioritize_delta", 0.0)))
    priority_label = _priority_label(priority_score)

    linked_goal_ids: list[int] = []
    linked_horizon_plan_ids: list[int] = []
    linked_improvement_proposal_ids: list[int] = []
    linked_inquiry_question_ids: list[int] = []
    linked_negotiation_ids: list[int] = []
    downstream_artifacts: list[dict] = []

    status = "active"
    dependency_resolution = {
        "has_unmet_dependencies": bool(dependency_gaps),
        "path": "proceed",
        "unmet_dependencies": dependency_gaps,
    }

    if dependency_gaps:
        dependency_resolution["path"] = policy
        if policy == "ask":
            question = await _get_or_create_orchestration_question(
                actor=actor,
                source=source,
                context_id=int(context.id) if context else None,
                dependency_gaps=dependency_gaps,
                metadata_json=metadata_json,
                db=db,
            )
            linked_inquiry_question_ids.append(int(question.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "inquiry_question",
                    "artifact_id": int(question.id),
                    "status": "open",
                }
            )
            status = "blocked_needs_input"
        elif policy == "defer":
            status = "deferred"
        elif policy == "replan":
            status = "replan_required"
        else:
            status = "escalated"

    if bool(collaboration_policy.get("defer_physical_action", False)) and not bool(collaboration_policy.get("continue_allowed", False)):
        status = "deferred"
        downstream_artifacts.append(
            {
                "artifact_type": "collaboration_update",
                "artifact_id": 0,
                "status": "deferred_for_human_context",
            }
        )
    if bool(collaboration_policy.get("surface_concise_update", False)):
        downstream_artifacts.append(
            {
                "artifact_type": "collaboration_update",
                "artifact_id": 0,
                "status": "concise_update_surfaced",
            }
        )
    if bool(collaboration_policy.get("ask_question", False)) and not bool(dependency_gaps):
        profile = pattern_influence.get("influence_profile", {}) if isinstance(pattern_influence.get("influence_profile", {}), dict) else {}
        question_priority = str(profile.get("question_priority", "high")).strip().lower()
        if question_priority not in {"normal", "high"}:
            question_priority = "high"
        question = await _get_or_create_collaboration_question(
            actor=actor,
            source=source,
            context_id=int(context.id) if context else None,
            collaboration_mode=collaboration_mode,
            policy=collaboration_policy,
            question_priority=question_priority,
            metadata_json=metadata_json,
            db=db,
        )
        linked_inquiry_question_ids.append(int(question.id))
        downstream_artifacts.append(
            {
                "artifact_type": "inquiry_question",
                "artifact_id": int(question.id),
                "status": "open",
            }
        )
        if not bool(collaboration_policy.get("continue_allowed", False)):
            status = "blocked_needs_input"

    negotiation_triggers = _negotiation_triggers(
        collaboration_mode=collaboration_mode,
        preferred_mode=preferred_mode,
        human_aware=human_aware,
        communication_urgency=communication_urgency,
        task_kind=task_kind,
        action_risk_level=action_risk_level,
        collaboration_policy=collaboration_policy,
    )
    options_presented = _negotiation_options(
        task_kind=task_kind,
        action_risk_level=action_risk_level,
        communication_urgency=communication_urgency,
        human_aware=human_aware,
    )
    if len(options_presented) >= 2 and bool(human_aware.get("operator_present", False)):
        negotiation_triggers.append(
            {
                "trigger_type": "multiple_safe_paths_human_preference_sensitive",
                "reason": "multiple safe paths exist and human preference matters",
            }
        )

    negotiation_payload: dict | None = None
    if negotiation_triggers:
        memory_option_ids = {
            str(item.get("option_id", "")).strip()
            for item in options_presented
            if isinstance(item, dict)
        }
        memory_default_option, memory_default_confidence, memory_default_evidence = _consolidated_option_from_memory(
            memory_preference=_memory_preference_for_pattern(
                memory_payload=memory_payload,
                pattern_key=memory_pattern_key,
                option_ids=memory_option_ids,
            ),
            option_ids=memory_option_ids,
        )
        memory_preference_with_options = _memory_preference_for_pattern(
            memory_payload=memory_payload,
            pattern_key=memory_pattern_key,
            option_ids=memory_option_ids,
        )
        pattern_influence = await _pattern_influence_for_signature(
            context_signature=memory_pattern_key,
            option_ids=memory_option_ids,
            db=db,
        )
        if str(memory_preference_with_options.get("freshness", "fresh")).strip() == "stale" or str(memory_preference_with_options.get("state", "learning")).strip() != "consolidated":
            pattern_influence = {
                **(pattern_influence if isinstance(pattern_influence, dict) else {}),
                "influence_applied": False,
                "reason": "objective68_memory_gate",
                "preferred_default_option": "",
            }
        pattern_default_option = str(pattern_influence.get("preferred_default_option", "")).strip() if isinstance(pattern_influence, dict) else ""
        default_path = _default_safe_path(
            options=options_presented,
            task_kind=task_kind,
            action_risk_level=action_risk_level,
            human_aware=human_aware,
            communication_urgency=communication_urgency,
            preferred_default_option=(pattern_default_option or memory_default_option or ""),
        )
        negotiation_payload = {
            "trigger": negotiation_triggers[0],
            "requested_decision": "Select preferred safe collaboration path for current orchestration conflict.",
            "options_presented": options_presented,
            "default_safe_path": default_path,
            "human_context_state": {
                "collaboration_mode": collaboration_mode,
                "preferred_mode": preferred_mode,
                "task_kind": task_kind,
                "action_risk_level": action_risk_level,
                "environment_profile": environment_profile,
                "communication_urgency": communication_urgency,
                "interruption_likelihood": interruption_likelihood,
                "operator_presence_score": operator_presence_score,
                "signals": human_aware,
            },
            "explainability": {
                "policy_version": NEGOTIATION_POLICY_VERSION,
                "trigger_summary": [item.get("reason", "") for item in negotiation_triggers],
                "why_human_input_needed": "Human-aware policy detected meaningful trade-offs among safe options requiring cooperative decision-making.",
                "safe_fallback_if_unanswered": default_path,
            },
        }
        if memory_default_option:
            negotiation_payload["explainability"] = {
                **(negotiation_payload.get("explainability", {}) if isinstance(negotiation_payload.get("explainability", {}), dict) else {}),
                "objective67_memory_influence": {
                    "preferred_option_id": memory_default_option,
                    "confidence": memory_default_confidence,
                    "evidence_count": memory_default_evidence,
                    "freshness": str(memory_preference.get("freshness", "fresh")),
                    "decay_factor": float(memory_preference.get("decay_factor", 1.0) or 1.0),
                    "context_match_score": float(memory_preference.get("context_match_score", 1.0) or 0.0),
                },
            }
        if isinstance(pattern_influence, dict) and bool(pattern_influence):
            negotiation_payload["explainability"] = {
                **(negotiation_payload.get("explainability", {}) if isinstance(negotiation_payload.get("explainability", {}), dict) else {}),
                "objective69_pattern_influence": {
                    "pattern_id": int(pattern_influence.get("pattern_id", 0) or 0),
                    "pattern_type": str(pattern_influence.get("pattern_type", "")),
                    "context_signature": str(pattern_influence.get("context_signature", "")),
                    "influence_applied": bool(pattern_influence.get("influence_applied", False)),
                    "preferred_default_option": str(pattern_influence.get("preferred_default_option", "")),
                    "effective_confidence": float(pattern_influence.get("effective_confidence", 0.0) or 0.0),
                    "evidence_count": int(pattern_influence.get("evidence_count", 0) or 0),
                    "freshness": str(pattern_influence.get("freshness", "fresh")),
                    "decay_factor": float(pattern_influence.get("decay_factor", 1.0) or 1.0),
                    "reason": str(pattern_influence.get("reason", "")),
                },
            }
        if not bool(collaboration_policy.get("continue_allowed", False)):
            if bool(collaboration_policy.get("defer_physical_action", False)):
                status = "deferred"
            else:
                status = "blocked_needs_input"

    if not dependency_gaps and status == "active":
        if generate_goal:
            goal = Goal(
                objective_id=None,
                task_id=None,
                goal_type="cross_domain_orchestration",
                goal_description=(
                    "Coordinate cross-domain execution using synchronized workspace, communication, "
                    "external information, and developmental context."
                ),
                requested_by=actor,
                priority=priority_label,
                status="new",
            )
            db.add(goal)
            await db.flush()
            linked_goal_ids.append(int(goal.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "goal",
                    "artifact_id": int(goal.id),
                    "status": "new",
                }
            )

        if generate_horizon_plan:
            horizon_goal_candidates = [
                {
                    "goal_key": f"orchestration:{int(context.id)}:primary",
                    "title": "Cross-domain orchestration execution",
                    "priority": priority_label,
                    "goal_type": "cross_domain_orchestration",
                    "dependencies": [],
                    "estimated_steps": 3,
                    "expected_value": _bounded(priority_score + 0.05),
                    "urgency": _bounded(priority_score),
                    "requires_fresh_map": False,
                    "requires_high_confidence": context_confidence >= 0.7,
                    "is_physical": False,
                    "metadata_json": {
                        "origin_context_id": int(context.id),
                        "linked_goal_id": linked_goal_ids[0] if linked_goal_ids else None,
                    },
                }
            ]
            plan, _ = await create_horizon_plan(
                actor=actor,
                source=source,
                planning_horizon_minutes=120,
                goal_candidates=horizon_goal_candidates,
                expected_future_constraints=[],
                priority_policy={
                    "min_target_confidence": max(0.4, _bounded(min_context_confidence)),
                    "map_freshness_limit_seconds": 900,
                },
                map_freshness_seconds=0,
                object_confidence=max(0.4, context_confidence),
                human_aware_state={},
                operator_preferences={},
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective63_orchestration": True,
                },
                db=db,
            )
            linked_horizon_plan_ids.append(int(plan.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "horizon_plan",
                    "artifact_id": int(plan.id),
                    "status": str(plan.status or "planned"),
                }
            )

        if generate_improvement_proposals and ("development_state" in contributing_domains or "self_improvement_state" in contributing_domains):
            proposals = await generate_improvement_proposals(
                actor=actor,
                source=source,
                lookback_hours=max(1, int(lookback_hours)),
                min_occurrence_count=2,
                max_proposals=1,
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective63_orchestration": True,
                },
                db=db,
            )
            for item in proposals[:1]:
                linked_improvement_proposal_ids.append(int(item.id))
                downstream_artifacts.append(
                    {
                        "artifact_type": "improvement_proposal",
                        "artifact_id": int(item.id),
                        "status": str(item.status or "proposed"),
                    }
                )

    orchestration_reason = (
        f"Prioritized {len(contributing_domains)}/5 contributing domains with context confidence={context_confidence:.2f}; "
        f"dependency_policy={policy}"
    )
    reasoning_out = {
        "priority_reason": "Blends context confidence with weighted cross-domain signal volume.",
        "contributing_domains": contributing_domains,
        "domain_signal_counts": signals,
        "context_summary": context_out.get("reasoning_summary", ""),
        "cross_domain_links": reasoning.get("cross_domain_links", []),
    }
    human_context_modifiers = {
        "collaboration_mode": collaboration_mode,
        "mode_reason": collaboration_mode_reason,
        "task_kind": collaboration_policy.get("task_kind", "mixed"),
        "action_risk_level": collaboration_policy.get("action_risk_level", "medium"),
        "active_modifiers": collaboration_policy.get("active_modifiers", []),
        "defer_physical_action": bool(collaboration_policy.get("defer_physical_action", False)),
        "require_confirmation": bool(collaboration_policy.get("require_confirmation", False)),
        "ask_question": bool(collaboration_policy.get("ask_question", False)),
        "surface_concise_update": bool(collaboration_policy.get("surface_concise_update", False)),
        "continue_allowed": bool(collaboration_policy.get("continue_allowed", False)),
        "reprioritize_delta": float(collaboration_policy.get("reprioritize_delta", 0.0)),
        "negotiation_required": bool(negotiation_payload),
    }
    collaboration_reasoning = {
        "policy_version": COLLABORATION_POLICY_VERSION,
        "preferred_mode": preferred_mode,
        "communication_urgency": communication_urgency,
        "communication_details": communication_details,
        "interruption_likelihood": interruption_likelihood,
        "interruption_details": interruption_details,
        "operator_presence_score": operator_presence_score,
        "human_aware_signals": human_aware,
    }

    row = WorkspaceTaskOrchestration(
        source=source,
        actor=actor,
        status=status,
        orchestration_type="cross_domain_task_orchestration",
        origin_context_id=int(context.id) if context else None,
        lookback_hours=max(1, int(lookback_hours)),
        priority_score=priority_score,
        priority_label=priority_label,
        collaboration_mode=collaboration_mode,
        human_context_modifiers_json=human_context_modifiers,
        collaboration_reasoning_json=collaboration_reasoning,
        contributing_domains_json=contributing_domains,
        dependency_resolution_json=dependency_resolution,
        orchestration_reason=orchestration_reason,
        reasoning_json=reasoning_out,
        linked_goal_ids_json=linked_goal_ids,
        linked_horizon_plan_ids_json=linked_horizon_plan_ids,
        linked_improvement_proposal_ids_json=linked_improvement_proposal_ids,
        linked_inquiry_question_ids_json=linked_inquiry_question_ids,
        downstream_artifacts_json=downstream_artifacts,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective63_cross_domain_task_orchestration": True,
            "objective64_human_aware_cross_domain_collaboration": True,
            "objective65_human_aware_collaboration_negotiation": bool(negotiation_payload),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(row)
    await db.flush()

    if negotiation_payload:
        trigger = negotiation_payload.get("trigger", {}) if isinstance(negotiation_payload.get("trigger", {}), dict) else {}
        trigger_type = str(trigger.get("trigger_type", "")).strip()
        pattern_key = _negotiation_pattern_key(
            trigger_type=trigger_type,
            human_context_state=negotiation_payload.get("human_context_state", {}),
        )
        pattern_payload = await _load_negotiation_patterns(db=db)
        option_ids = {
            str(item.get("option_id", "")).strip()
            for item in (negotiation_payload.get("options_presented", []) if isinstance(negotiation_payload.get("options_presented", []), list) else [])
            if isinstance(item, dict)
        }
        recommended_option_id, recommended_confidence, recommended_total = _recommended_option_from_patterns(
            pattern_payload=pattern_payload,
            pattern_key=pattern_key,
            option_ids=option_ids,
        )
        consolidated_memory_option, consolidated_memory_confidence, consolidated_memory_total = _consolidated_option_from_memory(
            memory_preference=_memory_preference_for_pattern(
                memory_payload=memory_payload,
                pattern_key=pattern_key,
                option_ids=option_ids,
            ),
            option_ids=option_ids,
        )
        if not recommended_option_id:
            if consolidated_memory_option:
                recommended_option_id = consolidated_memory_option
                recommended_confidence = consolidated_memory_confidence
                recommended_total = consolidated_memory_total

        negotiation = await _create_collaboration_negotiation(
            actor=actor,
            source=source,
            origin_context_id=int(context.id) if context else None,
            origin_goal_id=(int(linked_goal_ids[0]) if linked_goal_ids else None),
            origin_horizon_plan_id=(int(linked_horizon_plan_ids[0]) if linked_horizon_plan_ids else None),
            trigger=negotiation_payload.get("trigger", {}),
            requested_decision=str(negotiation_payload.get("requested_decision", "")),
            options_presented=negotiation_payload.get("options_presented", []),
            default_safe_path=str(negotiation_payload.get("default_safe_path", "defer_action")),
            human_context_state=negotiation_payload.get("human_context_state", {}),
            explainability=negotiation_payload.get("explainability", {}),
            metadata_json=metadata_json,
            db=db,
        )
        negotiation.origin_orchestration_id = int(row.id)
        linked_negotiation_ids.append(int(negotiation.id))
        row.downstream_artifacts_json = [
            *(row.downstream_artifacts_json if isinstance(row.downstream_artifacts_json, list) else []),
            {
                "artifact_type": "collaboration_negotiation",
                "artifact_id": int(negotiation.id),
                "status": str(negotiation.status or "open"),
                "default_safe_path": str(negotiation.default_safe_path or "defer_action"),
            },
        ]
        row.metadata_json = {
            **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
            "linked_collaboration_negotiation_ids": linked_negotiation_ids,
        }

        if recommended_option_id and not consolidated_memory_option:
            selected_option = _option_by_id(
                options=negotiation.options_presented_json if isinstance(negotiation.options_presented_json, list) else [],
                option_id=recommended_option_id,
            )
            if selected_option:
                applied_effect = _apply_negotiation_effect(negotiation=negotiation, orchestration=row, option=selected_option)
                follow_through = await _apply_negotiation_follow_through(
                    negotiation=negotiation,
                    selected_option_id=recommended_option_id,
                    db=db,
                )
                now = datetime.now(timezone.utc)
                negotiation.status = "resolved"
                negotiation.resolution_status = "reused_prior_pattern"
                negotiation.selected_option_id = str(selected_option.get("option_id", "")).strip()
                negotiation.selected_option_label = str(selected_option.get("label", "")).strip()
                negotiation.resolved_by = "system_pattern"
                negotiation.resolved_at = now
                negotiation.applied_effect_json = {
                    **(applied_effect if isinstance(applied_effect, dict) else {}),
                    "pattern_reuse": {
                        "pattern_key": pattern_key,
                        "confidence": recommended_confidence,
                        "observed_total": recommended_total,
                    },
                    "follow_through": follow_through,
                }
                negotiation.metadata_json = {
                    **(negotiation.metadata_json if isinstance(negotiation.metadata_json, dict) else {}),
                    "pattern_reuse": {
                        "auto_resolved": True,
                        "pattern_key": pattern_key,
                        "confidence": recommended_confidence,
                        "observed_total": recommended_total,
                    },
                }
                row.downstream_artifacts_json = [
                    {
                        **item,
                        "status": "resolved",
                    }
                    if isinstance(item, dict)
                    and str(item.get("artifact_type", "")).strip() == "collaboration_negotiation"
                    and int(item.get("artifact_id", 0) or 0) == int(negotiation.id)
                    else item
                    for item in (row.downstream_artifacts_json if isinstance(row.downstream_artifacts_json, list) else [])
                ]
                row.metadata_json = {
                    **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
                    "negotiation_pattern_reuse": {
                        "pattern_key": pattern_key,
                        "confidence": recommended_confidence,
                        "observed_total": recommended_total,
                        "selected_option_id": recommended_option_id,
                    },
                }
                await _record_negotiation_pattern_signal(
                    trigger_type=trigger_type,
                    human_context_state=negotiation.human_context_state_json if isinstance(negotiation.human_context_state_json, dict) else {},
                    selected_option_id=recommended_option_id,
                    actor="system_pattern",
                    resolution_status="reused_prior_pattern",
                    negotiation_id=int(negotiation.id),
                    db=db,
                )
    return row, context


async def list_task_orchestrations(
    *,
    db: AsyncSession,
    status: str = "",
    source: str = "",
    limit: int = 50,
) -> list[WorkspaceTaskOrchestration]:
    rows = (
        await db.execute(
            select(WorkspaceTaskOrchestration)
            .order_by(WorkspaceTaskOrchestration.id.desc())
        )
    ).scalars().all()

    filtered = rows
    if status.strip():
        requested_status = status.strip().lower()
        filtered = [item for item in filtered if str(item.status or "").strip().lower() == requested_status]
    if source.strip():
        requested_source = source.strip().lower()
        filtered = [item for item in filtered if str(item.source or "").strip().lower() == requested_source]

    return filtered[: max(1, min(500, int(limit)))]


async def get_task_orchestration(*, orchestration_id: int, db: AsyncSession) -> WorkspaceTaskOrchestration | None:
    return (
        await db.execute(
            select(WorkspaceTaskOrchestration).where(WorkspaceTaskOrchestration.id == orchestration_id)
        )
    ).scalars().first()


def to_task_orchestration_out(row: WorkspaceTaskOrchestration) -> dict:
    return {
        "orchestration_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "orchestration_type": row.orchestration_type,
        "origin_context_id": int(row.origin_context_id) if row.origin_context_id is not None else None,
        "lookback_hours": int(row.lookback_hours or 0),
        "priority_score": float(row.priority_score or 0.0),
        "priority_label": row.priority_label,
        "collaboration_mode": row.collaboration_mode,
        "human_context_modifiers": row.human_context_modifiers_json if isinstance(row.human_context_modifiers_json, dict) else {},
        "collaboration_reasoning": row.collaboration_reasoning_json if isinstance(row.collaboration_reasoning_json, dict) else {},
        "contributing_domains": row.contributing_domains_json if isinstance(row.contributing_domains_json, list) else [],
        "dependency_resolution": row.dependency_resolution_json if isinstance(row.dependency_resolution_json, dict) else {},
        "orchestration_reason": row.orchestration_reason,
        "reasoning": row.reasoning_json if isinstance(row.reasoning_json, dict) else {},
        "linked_goal_ids": row.linked_goal_ids_json if isinstance(row.linked_goal_ids_json, list) else [],
        "linked_horizon_plan_ids": row.linked_horizon_plan_ids_json if isinstance(row.linked_horizon_plan_ids_json, list) else [],
        "linked_improvement_proposal_ids": row.linked_improvement_proposal_ids_json if isinstance(row.linked_improvement_proposal_ids_json, list) else [],
        "linked_inquiry_question_ids": row.linked_inquiry_question_ids_json if isinstance(row.linked_inquiry_question_ids_json, list) else [],
        "downstream_artifacts": row.downstream_artifacts_json if isinstance(row.downstream_artifacts_json, list) else [],
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_collaboration_negotiation_out(row: WorkspaceCollaborationNegotiation) -> dict:
    return {
        "negotiation_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "resolution_status": row.resolution_status,
        "origin_orchestration_id": int(row.origin_orchestration_id) if row.origin_orchestration_id is not None else None,
        "origin_context_id": int(row.origin_context_id) if row.origin_context_id is not None else None,
        "origin_goal_id": int(row.origin_goal_id) if row.origin_goal_id is not None else None,
        "origin_horizon_plan_id": int(row.origin_horizon_plan_id) if row.origin_horizon_plan_id is not None else None,
        "trigger_type": row.trigger_type,
        "trigger_reason": row.trigger_reason,
        "requested_decision": row.requested_decision,
        "options_presented": row.options_presented_json if isinstance(row.options_presented_json, list) else [],
        "default_safe_path": row.default_safe_path,
        "selected_option_id": row.selected_option_id,
        "selected_option_label": row.selected_option_label,
        "human_context_state": row.human_context_state_json if isinstance(row.human_context_state_json, dict) else {},
        "explainability": row.explainability_json if isinstance(row.explainability_json, dict) else {},
        "applied_effect": row.applied_effect_json if isinstance(row.applied_effect_json, dict) else {},
        "resolved_by": row.resolved_by,
        "resolved_at": row.resolved_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


async def list_collaboration_negotiations(
    *,
    db: AsyncSession,
    status: str = "",
    source: str = "",
    limit: int = 50,
) -> list[WorkspaceCollaborationNegotiation]:
    rows = (
        await db.execute(
            select(WorkspaceCollaborationNegotiation)
            .order_by(WorkspaceCollaborationNegotiation.id.desc())
        )
    ).scalars().all()

    filtered = rows
    if status.strip():
        requested_status = status.strip().lower()
        filtered = [item for item in filtered if str(item.status or "").strip().lower() == requested_status]
    if source.strip():
        requested_source = source.strip().lower()
        filtered = [item for item in filtered if str(item.source or "").strip().lower() == requested_source]

    return filtered[: max(1, min(500, int(limit)))]


async def get_collaboration_negotiation(*, negotiation_id: int, db: AsyncSession) -> WorkspaceCollaborationNegotiation | None:
    return (
        await db.execute(
            select(WorkspaceCollaborationNegotiation).where(WorkspaceCollaborationNegotiation.id == negotiation_id)
        )
    ).scalars().first()


def _option_by_id(*, options: list[dict], option_id: str) -> dict | None:
    normalized = str(option_id or "").strip()
    for item in options:
        if not isinstance(item, dict):
            continue
        if str(item.get("option_id", "")).strip() == normalized:
            return item
    return None


def _apply_negotiation_effect(*, negotiation: WorkspaceCollaborationNegotiation, orchestration: WorkspaceTaskOrchestration | None, option: dict) -> dict:
    option_id = str(option.get("option_id", "")).strip()
    effect = str(option.get("effect", "")).strip()
    result: dict = {
        "option_id": option_id,
        "effect": effect,
        "updated_orchestration": bool(orchestration),
    }
    if not orchestration:
        return result

    modifiers = orchestration.human_context_modifiers_json if isinstance(orchestration.human_context_modifiers_json, dict) else {}
    active_modifiers = modifiers.get("active_modifiers", []) if isinstance(modifiers.get("active_modifiers", []), list) else []

    if option_id == "continue_now":
        orchestration.status = "active"
    elif option_id == "defer_action":
        orchestration.status = "deferred"
    elif option_id == "rescan_first":
        orchestration.status = "replan_required"
        active_modifiers.append("negotiated_rescan_first")
    elif option_id == "speak_summary_only":
        orchestration.status = "active"
        active_modifiers.append("negotiated_summary_only")
        modifiers["surface_concise_update"] = True
        modifiers["defer_physical_action"] = True
    elif option_id == "request_confirmation_later":
        orchestration.status = "blocked_needs_input"
        active_modifiers.append("negotiated_confirmation_follow_up")

    modifiers["active_modifiers"] = sorted(list({str(item) for item in active_modifiers if str(item).strip()}))
    modifiers["negotiation_required"] = False
    modifiers["negotiation_selected_option"] = option_id
    orchestration.human_context_modifiers_json = modifiers
    result["orchestration_status"] = orchestration.status
    return result


async def respond_collaboration_negotiation(
    *,
    negotiation_id: int,
    actor: str,
    option_id: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceCollaborationNegotiation, WorkspaceTaskOrchestration | None]:
    negotiation = await get_collaboration_negotiation(negotiation_id=negotiation_id, db=db)
    if not negotiation:
        raise ValueError("negotiation_not_found")
    if str(negotiation.status or "").strip().lower() != "open":
        raise ValueError("negotiation_not_open")

    option = _option_by_id(
        options=negotiation.options_presented_json if isinstance(negotiation.options_presented_json, list) else [],
        option_id=option_id,
    )
    if not option:
        raise ValueError("invalid_negotiation_option")

    orchestration = None
    if negotiation.origin_orchestration_id is not None:
        orchestration = await get_task_orchestration(orchestration_id=int(negotiation.origin_orchestration_id), db=db)

    selected_option_id = str(option.get("option_id", "")).strip()
    applied_effect = _apply_negotiation_effect(negotiation=negotiation, orchestration=orchestration, option=option)
    follow_through = await _apply_negotiation_follow_through(
        negotiation=negotiation,
        selected_option_id=selected_option_id,
        db=db,
    )
    negotiation.status = "resolved"
    negotiation.resolution_status = "operator_selected"
    negotiation.selected_option_id = selected_option_id
    negotiation.selected_option_label = str(option.get("label", "")).strip()
    negotiation.resolved_by = actor
    negotiation.resolved_at = datetime.now(timezone.utc)
    negotiation.applied_effect_json = {
        **(applied_effect if isinstance(applied_effect, dict) else {}),
        "reason": reason,
        "follow_through": follow_through,
    }
    negotiation.metadata_json = {
        **(negotiation.metadata_json if isinstance(negotiation.metadata_json, dict) else {}),
        "response": {
            "actor": actor,
            "reason": reason,
            "metadata_json": metadata_json if isinstance(metadata_json, dict) else {},
            "responded_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    await _record_negotiation_pattern_signal(
        trigger_type=str(negotiation.trigger_type or ""),
        human_context_state=negotiation.human_context_state_json if isinstance(negotiation.human_context_state_json, dict) else {},
        selected_option_id=selected_option_id,
        actor=actor,
        resolution_status="operator_selected",
        negotiation_id=int(negotiation.id),
        db=db,
    )
    await db.flush()
    return negotiation, orchestration


async def apply_due_collaboration_negotiation_fallbacks(
    *,
    fallback_after_seconds: int,
    db: AsyncSession,
) -> list[WorkspaceCollaborationNegotiation]:
    threshold = max(0, int(fallback_after_seconds))
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(WorkspaceCollaborationNegotiation)
            .where(WorkspaceCollaborationNegotiation.status == "open")
            .order_by(WorkspaceCollaborationNegotiation.id.asc())
            .limit(200)
        )
    ).scalars().all()

    applied: list[WorkspaceCollaborationNegotiation] = []
    for row in rows:
        age_seconds = max((now - row.created_at).total_seconds(), 0.0)
        if age_seconds < threshold:
            continue
        option = _option_by_id(
            options=row.options_presented_json if isinstance(row.options_presented_json, list) else [],
            option_id=str(row.default_safe_path or "").strip(),
        )
        if not option:
            continue

        orchestration = None
        if row.origin_orchestration_id is not None:
            orchestration = await get_task_orchestration(orchestration_id=int(row.origin_orchestration_id), db=db)
        selected_option_id = str(option.get("option_id", "")).strip()
        applied_effect = _apply_negotiation_effect(negotiation=row, orchestration=orchestration, option=option)
        follow_through = await _apply_negotiation_follow_through(
            negotiation=row,
            selected_option_id=selected_option_id,
            db=db,
        )
        row.status = "fallback_applied"
        row.resolution_status = "fallback_safe_default"
        row.selected_option_id = selected_option_id
        row.selected_option_label = str(option.get("label", "")).strip()
        row.resolved_by = "system"
        row.resolved_at = now
        row.applied_effect_json = {
            **(applied_effect if isinstance(applied_effect, dict) else {}),
            "fallback": True,
            "follow_through": follow_through,
        }
        await _record_negotiation_pattern_signal(
            trigger_type=str(row.trigger_type or ""),
            human_context_state=row.human_context_state_json if isinstance(row.human_context_state_json, dict) else {},
            selected_option_id=selected_option_id,
            actor="system",
            resolution_status="fallback_safe_default",
            negotiation_id=int(row.id),
            db=db,
        )
        applied.append(row)

    if applied:
        await db.flush()
    return applied


async def inspect_collaboration_state(
    *,
    lookback_hours: int,
    communication_urgency_override: float | None,
    db: AsyncSession,
) -> dict:
    human_aware = await _human_aware_state(db=db)
    communication_urgency, communication_details = await _communication_urgency(
        lookback_hours=lookback_hours,
        run_id="",
        override=communication_urgency_override,
        db=db,
    )
    interruption_likelihood, interruption_details = await _interruption_likelihood(human_aware=human_aware, db=db)
    operator_presence_score = _operator_presence_score(human_aware=human_aware)
    preferred_mode = await _preferred_collaboration_mode(db=db)
    mode, mode_reason = _resolve_collaboration_mode(
        requested_mode="auto",
        preferred_mode=preferred_mode,
        human_aware=human_aware,
        communication_urgency=communication_urgency,
        interruption_likelihood=interruption_likelihood,
    )
    policy = _apply_collaboration_policy(
        mode=mode,
        human_aware=human_aware,
        communication_urgency=communication_urgency,
        interruption_likelihood=interruption_likelihood,
        task_kind="mixed",
        action_risk_level="medium",
    )
    return {
        "policy_version": COLLABORATION_POLICY_VERSION,
        "collaboration_mode": mode,
        "communication_urgency": communication_urgency,
        "interruption_likelihood": interruption_likelihood,
        "operator_presence_score": operator_presence_score,
        "human_aware_signals": human_aware,
        "active_modifiers": policy.get("active_modifiers", []),
        "reasoning": {
            "mode_reason": mode_reason,
            "preferred_mode": preferred_mode,
            "communication_details": communication_details,
            "interruption_details": interruption_details,
        },
    }


async def set_collaboration_mode_preference(
    *,
    actor: str,
    mode: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> UserPreference:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in COLLABORATION_MODES:
        raise ValueError("invalid_collaboration_mode")

    row = UserPreference(
        user_id="operator",
        preference_type="collaboration_mode:default",
        value={
            "mode": normalized_mode,
            "reason": reason,
            "updated_by": actor,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "metadata_json": metadata_json if isinstance(metadata_json, dict) else {},
        },
        confidence=0.9,
        source="operator",
        last_updated=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row
