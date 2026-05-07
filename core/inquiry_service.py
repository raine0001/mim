from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_service import derive_execution_truth_signals, execution_truth_scope_refs
from core.models import (
    CapabilityExecution,
    ConstraintEvaluation,
    InputEvent,
    MemoryEntry,
    WorkspaceAutonomyBoundaryProfile,
    WorkspaceEnvironmentStrategy,
    WorkspaceHorizonPlan,
    WorkspaceImprovementProposal,
    WorkspaceInquiryQuestion,
    WorkspaceOperatorResolutionCommitment,
    WorkspaceOperatorResolutionCommitmentMonitoringProfile,
    WorkspaceOperatorResolutionCommitmentOutcomeProfile,
    WorkspacePerceptionSource,
    WorkspaceProposal,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
    WorkspaceStrategyGoal,
)
from core.policy_conflict_resolution_service import (
    resolve_inquiry_decision_policy_conflict,
    resolve_inquiry_answer_path_policy_conflict,
)
from core.proposal_arbitration_learning_service import workspace_proposal_arbitration_family_influence
from core.operator_resolution_service import latest_active_operator_resolution_commitment, scope_value

QUESTION_STATUSES = {"open", "answered", "dismissed", "expired"}
INQUIRY_REQUIRED_THRESHOLD = 0.7
INQUIRY_OPTIONAL_THRESHOLD = 0.45
INQUIRY_DEFAULT_COOLDOWN_SECONDS = 3600
HIGH_CONFIDENCE_AUTONOMY_LEVELS = {"bounded_auto", "full_auto"}
HIGH_CONFIDENCE_AUTONOMY_EXEMPT_TRIGGERS = {
    "operator_commitment_drift_detected",
    "operator_commitment_learning_review",
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _run_id(metadata_json: dict) -> str:
    if not isinstance(metadata_json, dict):
        return ""
    return str(metadata_json.get("run_id", "")).strip()


def _match_run_id(data: dict, run_id: str) -> bool:
    if not run_id:
        return True
    if not isinstance(data, dict):
        return False
    return str(data.get("run_id", "")).strip() == run_id


def _extract_warning_keys(row: ConstraintEvaluation) -> list[str]:
    warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
    keys: list[str] = []
    for item in warnings:
        if not isinstance(item, dict):
            continue
        key = str(item.get("constraint", "")).strip()
        if key:
            keys.append(key)
    return keys


def _constraint_run_metadata(row: ConstraintEvaluation) -> dict:
    explanation = row.explanation_json if isinstance(row.explanation_json, dict) else {}
    metadata = explanation.get("metadata_json", {})
    return metadata if isinstance(metadata, dict) else {}


def _metadata_json(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _allowed_answer_effects(candidate_paths: list[dict]) -> list[str]:
    allowed: list[str] = []
    seen: set[str] = set()
    effect_map = {
        "trigger_rescan": "rescan",
        "update_stewardship_target": "tighten_tracking",
        "create_proposal": "propose_improvement",
        "change_autonomy": "lower_autonomy",
        "update_commitment_status": "change_commitment",
        "record_commitment_learning_bias": "change_commitment",
        "shift_strategy": "no_action",
        "shift_strategy_and_unblock": "no_action",
        "unblock_plan": "no_action",
        "no_action": "no_action",
    }
    for item in candidate_paths:
        if not isinstance(item, dict):
            continue
        if bool(item.get("policy_conflict_masked", False)):
            continue
        mapped = effect_map.get(str(item.get("effect_type", "")).strip(), "no_action")
        if mapped in seen:
            continue
        seen.add(mapped)
        allowed.append(mapped)
    return allowed


def _apply_inquiry_policy_conflict(
    *, candidate_paths: list[dict], policy_conflict_resolution: dict
) -> list[dict]:
    if not isinstance(candidate_paths, list) or not candidate_paths:
        return []
    if not isinstance(policy_conflict_resolution, dict) or not policy_conflict_resolution:
        return list(candidate_paths)

    effects = (
        policy_conflict_resolution.get("policy_effects_json", {})
        if isinstance(policy_conflict_resolution.get("policy_effects_json", {}), dict)
        else {}
    )
    preferred_path_ids = {
        str(item).strip()
        for item in effects.get("preferred_path_ids", [])
        if str(item).strip()
    }
    preferred_effect_types = {
        str(item).strip()
        for item in effects.get("preferred_effect_types", [])
        if str(item).strip()
    }
    masked_path_ids = {
        str(item).strip()
        for item in effects.get("masked_path_ids", [])
        if str(item).strip()
    }
    disallowed_effect_types = {
        str(item).strip()
        for item in effects.get("disallowed_effect_types", [])
        if str(item).strip()
    }
    winning_policy_source = str(
        policy_conflict_resolution.get("winning_policy_source", "") or ""
    ).strip()

    annotated: list[tuple[int, int, dict]] = []
    for index, item in enumerate(candidate_paths):
        if not isinstance(item, dict):
            continue
        path_id = str(item.get("path_id", "") or "").strip()
        effect_type = str(item.get("effect_type", "") or "").strip()
        preferred = path_id in preferred_path_ids or effect_type in preferred_effect_types
        masked = path_id in masked_path_ids or effect_type in disallowed_effect_types
        updated = {
            **item,
            "policy_conflict_preferred": preferred,
            "policy_conflict_masked": masked,
        }
        if winning_policy_source:
            updated["policy_conflict_winning_source"] = winning_policy_source
        priority_bucket = 0 if preferred else (2 if masked else 1)
        annotated.append((priority_bucket, index, updated))

    annotated.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in annotated]


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _inquiry_related_zone(*, trigger_evidence: dict, metadata_json: dict) -> str:
    for bucket in (trigger_evidence, metadata_json):
        if not isinstance(bucket, dict):
            continue
        for key in ("managed_scope", "target_scope", "related_zone", "scope", "scan_area", "zone"):
            value = str(bucket.get(key, "") or "").strip()
            if value:
                return value
    return "global"


def _inquiry_managed_scope_from_context(
    *,
    origin_goal: WorkspaceStrategyGoal | None = None,
    plan: WorkspaceHorizonPlan | None = None,
    metadata_json: dict | None = None,
) -> str:
    for bucket in [metadata_json if isinstance(metadata_json, dict) else {}]:
        value = scope_value(bucket.get("managed_scope"))
        if value:
            return value
        value = scope_value(bucket.get("scope"))
        if value:
            return value

    if origin_goal is not None and isinstance(origin_goal.metadata_json, dict):
        value = scope_value(origin_goal.metadata_json.get("managed_scope"))
        if value:
            return value
        value = scope_value(origin_goal.metadata_json.get("scope"))
        if value:
            return value

    if plan is not None:
        ranked = plan.ranked_goals_json if isinstance(plan.ranked_goals_json, list) else []
        for item in ranked:
            if not isinstance(item, dict):
                continue
            item_metadata = item.get("metadata_json", {})
            if not isinstance(item_metadata, dict):
                continue
            value = scope_value(item_metadata.get("managed_scope"))
            if value:
                return value
            value = scope_value(item_metadata.get("scope"))
            if value:
                return value
    return ""


def _answer_path_related_proposal_types(*, trigger_type: str, path: dict) -> list[str]:
    if not isinstance(path, dict):
        return []
    effect_type = str(path.get("effect_type", "") or "").strip()
    params = path.get("params", {}) if isinstance(path.get("params", {}), dict) else {}
    if effect_type == "trigger_rescan":
        proposal_type = str(params.get("proposal_type", "rescan_zone") or "rescan_zone").strip()
        return [proposal_type] if proposal_type else []
    if effect_type == "update_stewardship_target":
        return ["rescan_zone", "verify_moved_object", "confirm_target_ready"]
    if effect_type in {"shift_strategy", "shift_strategy_and_unblock"}:
        return ["confirm_target_ready", "rescan_zone"]
    if effect_type == "update_commitment_status":
        return ["rescan_zone", "confirm_target_ready"]
    if effect_type == "create_proposal":
        proposal_type = str(params.get("proposal_type", "") or "").strip()
        return [proposal_type] if proposal_type else []
    if str(trigger_type or "").strip() == "stewardship_persistent_degradation" and effect_type == "no_action":
        return ["monitor_recheck_workspace"]
    return []


async def _rank_candidate_answer_paths(
    *,
    trigger_type: str,
    candidate_answer_paths: list[dict],
    trigger_evidence_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> list[dict]:
    if not isinstance(candidate_answer_paths, list):
        return []
    related_zone = _inquiry_related_zone(
        trigger_evidence=trigger_evidence_json if isinstance(trigger_evidence_json, dict) else {},
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    ranked: list[tuple[float, int, dict]] = []
    for index, item in enumerate(candidate_answer_paths):
        if not isinstance(item, dict):
            continue
        proposal_types = _answer_path_related_proposal_types(
            trigger_type=trigger_type,
            path=item,
        )
        influence = await workspace_proposal_arbitration_family_influence(
            proposal_types=proposal_types,
            related_zone=related_zone,
            db=db,
            max_abs_bias=0.08,
        )
        arbitration_weight = float(influence.get("aggregate_priority_bias", 0.0) or 0.0)
        score = round(0.5 + arbitration_weight, 6)
        ranked_item = {
            **item,
            "score": score,
            "proposal_arbitration_weight": round(arbitration_weight, 6),
            "proposal_arbitration_learning": {
                "related_zone": str(influence.get("related_zone", related_zone) or related_zone),
                "proposal_types": influence.get("proposal_types", proposal_types),
                "sample_count": int(influence.get("sample_count", 0) or 0),
                "learning": influence.get("learning", []),
                "applied": bool(influence.get("applied", False)),
            },
        }
        ranked.append((-score, index, ranked_item))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


async def _latest_active_inquiry_suppression_commitment(
    *,
    managed_scope: str,
    db: AsyncSession,
) -> object | None:
    scope = scope_value(managed_scope)
    if not scope:
        return None
    return await latest_active_operator_resolution_commitment(
        scope=scope,
        db=db,
        decision_types=["require_additional_evidence"],
        require_downstream_effects=["suppress_duplicate_inquiry"],
    )


def _policy_metadata(row: WorkspaceInquiryQuestion) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    policy = metadata.get("inquiry_policy", {})
    return policy if isinstance(policy, dict) else {}


def _infer_evidence_score(*, trigger_type: str, trigger_evidence: dict) -> float:
    if not isinstance(trigger_evidence, dict):
        return 0.0

    if trigger_type == "stewardship_persistent_degradation":
        degraded_signal_count = float(trigger_evidence.get("degraded_signal_count", 0) or 0)
        inquiry_candidates = trigger_evidence.get("inquiry_candidates", [])
        missing_key_objects = trigger_evidence.get("missing_key_objects", [])
        signal_count = float(trigger_evidence.get("execution_truth_signal_count", 0) or 0)
        score = 0.35
        score += min(0.25, degraded_signal_count * 0.12)
        if isinstance(inquiry_candidates, list) and inquiry_candidates:
            score += min(0.2, len(inquiry_candidates) * 0.05)
        if isinstance(missing_key_objects, list) and missing_key_objects:
            score += min(0.15, len(missing_key_objects) * 0.05)
        if signal_count > 0:
            score += min(0.15, signal_count * 0.03)
        if degraded_signal_count > 0 and (
            (isinstance(missing_key_objects, list) and missing_key_objects)
            or (isinstance(inquiry_candidates, list) and inquiry_candidates)
            or signal_count > 0
        ):
            score = max(score, INQUIRY_REQUIRED_THRESHOLD)
        return _bounded(score)

    if trigger_type == "execution_truth_runtime_mismatch":
        signal_count = float(trigger_evidence.get("signal_count", 0) or 0)
        return _bounded(0.45 + min(0.4, signal_count * 0.08))

    if trigger_type == "target_confidence_too_low":
        warning_count = float(trigger_evidence.get("warning_count", 0) or 0)
        return _bounded(0.4 + min(0.35, warning_count * 0.06))

    if trigger_type == "repeated_soft_constraint_friction":
        soft_warning_count = float(trigger_evidence.get("soft_warning_count", 0) or 0)
        return _bounded(0.25 + min(0.3, soft_warning_count * 0.04))

    if trigger_type == "low_confidence_perception_blocking_strategic_goal":
        low_confidence_count = float(trigger_evidence.get("low_confidence_count", 0) or 0)
        return _bounded(0.5 + min(0.35, low_confidence_count * 0.08))

    if trigger_type == "ambiguous_next_action_under_multiple_valid_paths":
        top_1 = float(trigger_evidence.get("score_top_1", 0.0) or 0.0)
        top_2 = float(trigger_evidence.get("score_top_2", 0.0) or 0.0)
        closeness = 1.0 - min(1.0, abs(top_1 - top_2) / 0.05) if top_1 or top_2 else 0.0
        return _bounded(0.3 + (closeness * 0.35))

    if trigger_type == "operator_commitment_drift_detected":
        drift_score = float(trigger_evidence.get("drift_score", 0.0) or 0.0)
        health_score = float(trigger_evidence.get("health_score", 0.0) or 0.0)
        potential_violations = float(
            trigger_evidence.get("potential_violation_count", 0) or 0
        )
        return _bounded(
            0.45
            + min(0.3, drift_score * 0.35)
            + min(0.2, (1.0 - _bounded(health_score)) * 0.3)
            + min(0.2, potential_violations * 0.08)
        )

    return _bounded(0.55 if trigger_evidence else 0.0)


def _state_delta_summary(applied_effect: dict) -> list[str]:
    summary: list[str] = []
    if bool(applied_effect.get("workspace_proposal_created", False)):
        summary.append("workspace_proposal_created")
    if bool(applied_effect.get("improvement_proposal_created", False)):
        summary.append("improvement_proposal_created")
    if bool(applied_effect.get("stewardship_target_updated", False)):
        summary.append("stewardship_target_updated")
    if bool(applied_effect.get("autonomy_changed", False)):
        summary.append("autonomy_changed")
    if bool(applied_effect.get("commitment_status_updated", False)):
        summary.append("commitment_status_updated")
    if bool(applied_effect.get("strategy_shifted", False)):
        summary.append("strategy_shifted")
    if bool(applied_effect.get("plan_unblocked", False)):
        summary.append("plan_unblocked")
    if not summary and not bool(applied_effect.get("applied", False)):
        summary.append("no_material_state_change")
    return summary


async def _latest_autonomy_profile(
    *, managed_scope: str, db: AsyncSession
) -> WorkspaceAutonomyBoundaryProfile | None:
    scope = scope_value(managed_scope)
    if scope:
        scoped = (
            (
                await db.execute(
                    select(WorkspaceAutonomyBoundaryProfile)
                    .where(WorkspaceAutonomyBoundaryProfile.scope == scope)
                    .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if scoped is not None:
            return scoped
    return None


async def _latest_answered_question(
    *, dedupe_key: str, db: AsyncSession
) -> WorkspaceInquiryQuestion | None:
    return (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
                .where(WorkspaceInquiryQuestion.status == "answered")
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _govern_inquiry_decision(
    *,
    dedupe_key: str,
    trigger_type: str,
    candidate_paths: list[dict],
    trigger_evidence: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> dict:
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    policy_inputs = metadata.get("inquiry_policy_inputs", {})
    if not isinstance(policy_inputs, dict):
        policy_inputs = {}

    evidence_score = _infer_evidence_score(
        trigger_type=trigger_type,
        trigger_evidence=trigger_evidence if isinstance(trigger_evidence, dict) else {},
    )
    cooldown_seconds = int(
        policy_inputs.get(
            "cooldown_seconds",
            (
                trigger_evidence.get("intervention_policy", {})
                if isinstance(trigger_evidence.get("intervention_policy", {}), dict)
                else {}
            ).get("scope_cooldown_seconds", INQUIRY_DEFAULT_COOLDOWN_SECONDS),
        )
        or INQUIRY_DEFAULT_COOLDOWN_SECONDS
    )
    allowed_answer_effects = _allowed_answer_effects(candidate_paths)

    decision = {
        "decision_state": "optional_for_refinement",
        "reason": "moderate_evidence_uncertainty",
        "evidence_score": round(evidence_score, 6),
        "cooldown_active": False,
        "cooldown_remaining_seconds": 0,
        "duplicate_suppressed": False,
        "recent_answer_reused": False,
        "allowed_answer_effects": allowed_answer_effects,
        "suppression_reason": "",
        "reused_question_id": None,
        "reused_answered_at": None,
        "question_id": None,
        "dedupe_key": dedupe_key,
        "trigger_type": trigger_type,
        "cooldown_seconds": max(0, cooldown_seconds),
    }

    managed_scope = scope_value(
        trigger_evidence.get("managed_scope") if isinstance(trigger_evidence, dict) else ""
    )
    if not managed_scope and isinstance(metadata, dict):
        metadata_scope = scope_value(metadata.get("managed_scope"))
        if metadata_scope:
            managed_scope = metadata_scope
    decision_scope = managed_scope or dedupe_key
    suppression_commitment = await _latest_active_inquiry_suppression_commitment(
        managed_scope=managed_scope,
        db=db,
    )

    latest_answered = await _latest_answered_question(dedupe_key=dedupe_key, db=db)
    latest_answered_policy = _policy_metadata(latest_answered) if latest_answered is not None else {}
    cooldown_remaining_seconds = 0
    if latest_answered and latest_answered.answered_at is not None and cooldown_seconds > 0:
        now = datetime.now(timezone.utc)
        elapsed = max(0.0, (now - latest_answered.answered_at).total_seconds())
        if elapsed < cooldown_seconds:
            cooldown_remaining_seconds = max(0, int(round(cooldown_seconds - elapsed)))

    autonomy_profile = await _latest_autonomy_profile(
        managed_scope=managed_scope,
        db=db,
    )
    autonomy_level = (
        str(autonomy_profile.current_level or "").strip() if autonomy_profile else ""
    )
    decision["autonomy_level"] = autonomy_level

    decision_policy_conflict_resolution = await resolve_inquiry_decision_policy_conflict(
        managed_scope=decision_scope,
        trigger_type=trigger_type,
        evidence_score=evidence_score,
        cooldown_seconds=max(0, cooldown_seconds),
        cooldown_remaining_seconds=cooldown_remaining_seconds,
        latest_answered=latest_answered if cooldown_remaining_seconds > 0 else None,
        latest_answered_policy=latest_answered_policy,
        trigger_evidence=trigger_evidence if isinstance(trigger_evidence, dict) else {},
        suppression_commitment=(
            suppression_commitment
            if suppression_commitment is not None
            and trigger_type != "operator_commitment_drift_detected"
            else None
        ),
        autonomy_profile=autonomy_profile,
        db=db,
    )
    if decision_policy_conflict_resolution:
        effects = (
            decision_policy_conflict_resolution.get("policy_effects_json", {})
            if isinstance(
                decision_policy_conflict_resolution.get("policy_effects_json", {}),
                dict,
            )
            else {}
        )
        decision.update(effects)
        decision["decision_policy_conflict_resolution"] = decision_policy_conflict_resolution
        return {
            "decision": decision,
            "question": None,
            "create_new": str(decision.get("decision_state", ""))
            in {"optional_for_refinement", "required_for_progress"},
        }

    existing_open = await _existing_open_question(dedupe_key=dedupe_key, db=db)
    if existing_open:
        decision.update(
            {
                "decision_state": "deferred_due_to_cooldown",
                "reason": "duplicate_open_inquiry_exists",
                "cooldown_active": True,
                "duplicate_suppressed": True,
                "question_id": int(existing_open.id),
                "suppression_reason": "open_question_exists",
            }
        )
        return {"decision": decision, "question": existing_open, "create_new": False}

    if evidence_score < INQUIRY_OPTIONAL_THRESHOLD:
        decision.update(
            {
                "decision_state": "suppressed_low_evidence",
                "reason": "evidence_below_minimum_threshold",
                "suppression_reason": "low_evidence",
            }
        )
        return {"decision": decision, "question": None, "create_new": False}

    if (
        autonomy_level in HIGH_CONFIDENCE_AUTONOMY_LEVELS
        and trigger_type not in HIGH_CONFIDENCE_AUTONOMY_EXEMPT_TRIGGERS
        and evidence_score < INQUIRY_REQUIRED_THRESHOLD
    ):
        decision.update(
            {
                "decision_state": "suppressed_high_confidence_autonomy",
                "reason": "autonomy_confident_enough_without_operator_inquiry",
                "suppression_reason": "high_confidence_autonomy",
            }
        )
        return {"decision": decision, "question": None, "create_new": False}

    if evidence_score >= INQUIRY_REQUIRED_THRESHOLD:
        decision.update(
            {
                "decision_state": "required_for_progress",
                "reason": "persistent_degradation_with_actionable_uncertainty",
            }
        )

    return {"decision": decision, "question": None, "create_new": True}


def _merge_unique_strings(existing: object, additions: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for collection in [existing, additions]:
        if not isinstance(collection, list):
            continue
        for item in collection:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _execution_run_id(
    *, row: CapabilityExecution, input_event: InputEvent | None
) -> str:
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
    feedback_run_id = str(feedback.get("run_id", "")).strip()
    if feedback_run_id:
        return feedback_run_id
    if input_event and isinstance(input_event.metadata_json, dict):
        return str(input_event.metadata_json.get("run_id", "")).strip()
    return ""


async def _recent_stewardship_cycles(
    *,
    since: datetime,
    run_id: str,
    db: AsyncSession,
) -> list[WorkspaceStewardshipCycle]:
    rows = (
        (
            await db.execute(
                select(WorkspaceStewardshipCycle)
                .where(WorkspaceStewardshipCycle.created_at >= since)
                .order_by(WorkspaceStewardshipCycle.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    if not run_id:
        return rows
    return [
        item
        for item in rows
        if _match_run_id(_metadata_json(item.metadata_json), run_id)
    ]


async def _get_stewardship_state(
    *, stewardship_id: int, db: AsyncSession
) -> WorkspaceStewardshipState | None:
    if stewardship_id <= 0:
        return None
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


async def _existing_open_question(
    *, dedupe_key: str, db: AsyncSession
) -> WorkspaceInquiryQuestion | None:
    return (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
                .where(WorkspaceInquiryQuestion.status == "open")
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_horizon_plan(
    *, run_id: str, db: AsyncSession
) -> WorkspaceHorizonPlan | None:
    rows = (
        (
            await db.execute(
                select(WorkspaceHorizonPlan)
                .order_by(WorkspaceHorizonPlan.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    if not run_id:
        return rows[0] if rows else None
    for row in rows:
        if _match_run_id(
            row.metadata_json if isinstance(row.metadata_json, dict) else {}, run_id
        ):
            return row
    return None


def _question_payload(row: WorkspaceInquiryQuestion) -> dict:
    policy = _policy_metadata(row)
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return {
        "question_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "trigger_type": row.trigger_type,
        "uncertainty_type": row.uncertainty_type,
        "originating_goal_id": int(row.origin_strategy_goal_id)
        if row.origin_strategy_goal_id is not None
        else None,
        "originating_strategy_id": int(row.origin_strategy_id)
        if row.origin_strategy_id is not None
        else None,
        "originating_plan_id": int(row.origin_plan_id)
        if row.origin_plan_id is not None
        else None,
        "why_answer_matters": row.why_answer_matters,
        "waiting_decision": row.waiting_decision,
        "no_answer_behavior": row.no_answer_behavior,
        "candidate_answer_paths": row.candidate_answer_paths_json
        if isinstance(row.candidate_answer_paths_json, list)
        else [],
        "urgency": row.urgency,
        "priority": row.priority,
        "safe_default_if_unanswered": row.safe_default_if_unanswered,
        "trigger_evidence": row.trigger_evidence_json
        if isinstance(row.trigger_evidence_json, dict)
        else {},
        "selected_path_id": row.selected_path_id,
        "answer_json": row.answer_json if isinstance(row.answer_json, dict) else {},
        "applied_effect_json": row.applied_effect_json
        if isinstance(row.applied_effect_json, dict)
        else {},
        "answered_by": row.answered_by,
        "answered_at": row.answered_at,
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "decision_state": str(policy.get("decision_state", "")).strip(),
        "decision_reason": str(policy.get("reason", "")).strip(),
        "policy_evidence_score": float(policy.get("evidence_score", 0.0) or 0.0),
        "cooldown_active": bool(policy.get("cooldown_active", False)),
        "cooldown_remaining_seconds": int(
            policy.get("cooldown_remaining_seconds", 0) or 0
        ),
        "duplicate_suppressed": bool(policy.get("duplicate_suppressed", False)),
        "recent_answer_reused": bool(policy.get("recent_answer_reused", False)),
        "allowed_answer_effects": policy.get("allowed_answer_effects", [])
        if isinstance(policy.get("allowed_answer_effects", []), list)
        else [],
        "policy_conflict_resolution": (
            policy.get("policy_conflict_resolution", {})
            if isinstance(policy.get("policy_conflict_resolution", {}), dict)
            else (
                metadata.get("policy_conflict_resolution", {})
                if isinstance(metadata.get("policy_conflict_resolution", {}), dict)
                else {}
            )
        ),
        "decision_policy_conflict_resolution": (
            policy.get("decision_policy_conflict_resolution", {})
            if isinstance(policy.get("decision_policy_conflict_resolution", {}), dict)
            else (
                metadata.get("decision_policy_conflict_resolution", {})
                if isinstance(metadata.get("decision_policy_conflict_resolution", {}), dict)
                else {}
            )
        ),
        "created_at": row.created_at,
    }


async def _create_governed_inquiry_question(
    *,
    source: str,
    actor: str,
    dedupe_key: str,
    trigger_type: str,
    uncertainty_type: str,
    origin_strategy_goal_id: int | None,
    origin_strategy_id: int | None,
    origin_plan_id: int | None,
    why_answer_matters: str,
    waiting_decision: str,
    no_answer_behavior: str,
    candidate_answer_paths: list[dict],
    urgency: str,
    priority: str,
    safe_default_if_unanswered: str,
    trigger_evidence_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceInquiryQuestion | None, dict]:
    ranked_candidate_paths = await _rank_candidate_answer_paths(
        trigger_type=trigger_type,
        candidate_answer_paths=candidate_answer_paths,
        trigger_evidence_json=trigger_evidence_json,
        metadata_json=metadata_json,
        db=db,
    )
    policy_conflict_resolution = await resolve_inquiry_answer_path_policy_conflict(
        managed_scope=_inquiry_related_zone(
            trigger_evidence=(
                trigger_evidence_json if isinstance(trigger_evidence_json, dict) else {}
            ),
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
        ),
        trigger_type=trigger_type,
        candidate_paths=ranked_candidate_paths,
        db=db,
    )
    ranked_candidate_paths = _apply_inquiry_policy_conflict(
        candidate_paths=ranked_candidate_paths,
        policy_conflict_resolution=policy_conflict_resolution,
    )
    governed = await _govern_inquiry_decision(
        dedupe_key=dedupe_key,
        trigger_type=trigger_type,
        candidate_paths=ranked_candidate_paths,
        trigger_evidence=trigger_evidence_json,
        metadata_json=metadata_json,
        db=db,
    )
    decision = governed.get("decision", {})
    if policy_conflict_resolution:
        decision = {
            **decision,
            "policy_conflict_resolution": policy_conflict_resolution,
        }
    existing = governed.get("question")
    if isinstance(existing, WorkspaceInquiryQuestion):
        current_metadata = (
            existing.metadata_json if isinstance(existing.metadata_json, dict) else {}
        )
        existing.metadata_json = {
            **current_metadata,
            "policy_conflict_resolution": policy_conflict_resolution,
            "decision_policy_conflict_resolution": decision.get(
                "decision_policy_conflict_resolution",
                {},
            ),
            "inquiry_policy": {
                **(
                    current_metadata.get("inquiry_policy", {})
                    if isinstance(current_metadata.get("inquiry_policy", {}), dict)
                    else {}
                ),
                **decision,
            },
        }
        return existing, decision
    if not bool(governed.get("create_new", False)):
        return None, decision

    row = WorkspaceInquiryQuestion(
        source=source,
        actor=actor,
        status="open",
        dedupe_key=dedupe_key,
        trigger_type=trigger_type,
        uncertainty_type=uncertainty_type,
        origin_strategy_goal_id=origin_strategy_goal_id,
        origin_strategy_id=origin_strategy_id,
        origin_plan_id=origin_plan_id,
        why_answer_matters=why_answer_matters,
        waiting_decision=waiting_decision,
        no_answer_behavior=no_answer_behavior,
        candidate_answer_paths_json=ranked_candidate_paths,
        urgency=urgency,
        priority=priority,
        safe_default_if_unanswered=safe_default_if_unanswered,
        trigger_evidence_json=trigger_evidence_json,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "policy_conflict_resolution": policy_conflict_resolution,
            "decision_policy_conflict_resolution": decision.get(
                "decision_policy_conflict_resolution",
                {},
            ),
            "inquiry_policy": decision,
        },
    )
    db.add(row)
    return row, decision


async def generate_inquiry_questions(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_questions: int,
    min_soft_friction_count: int,
    metadata_json: dict,
    db: AsyncSession,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    run_id = _run_id(metadata_json)

    strategy_goals = (
        (
            await db.execute(
                select(WorkspaceStrategyGoal)
                .where(WorkspaceStrategyGoal.created_at >= since)
                .order_by(WorkspaceStrategyGoal.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    strategies = (
        (
            await db.execute(
                select(WorkspaceEnvironmentStrategy)
                .where(WorkspaceEnvironmentStrategy.current_status == "active")
                .order_by(
                    WorkspaceEnvironmentStrategy.influence_weight.desc(),
                    WorkspaceEnvironmentStrategy.id.desc(),
                )
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    plan = await _latest_horizon_plan(run_id=run_id, db=db)

    if run_id:
        strategy_goals = [
            item
            for item in strategy_goals
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
        strategies = [
            item
            for item in strategies
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]

    constraint_rows = (
        (
            await db.execute(
                select(ConstraintEvaluation)
                .where(ConstraintEvaluation.created_at >= since)
                .order_by(ConstraintEvaluation.id.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        constraint_rows = [
            item
            for item in constraint_rows
            if _match_run_id(_constraint_run_metadata(item), run_id)
        ]

    input_rows = (
        (
            await db.execute(
                select(InputEvent)
                .where(InputEvent.created_at >= since)
                .order_by(InputEvent.id.desc())
                .limit(1500)
            )
        )
        .scalars()
        .all()
    )
    external_memory = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.created_at >= since)
                .order_by(MemoryEntry.id.desc())
                .limit(1500)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        input_rows = [
            item
            for item in input_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
        external_memory = [
            item
            for item in external_memory
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
    external_memory = [
        item
        for item in external_memory
        if str(item.memory_class or "").lower().startswith("external")
    ]

    source_rows = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    stewardship_cycles = await _recent_stewardship_cycles(
        since=since,
        run_id=run_id,
        db=db,
    )
    commitment_monitoring_rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentMonitoringProfile)
                .where(WorkspaceOperatorResolutionCommitmentMonitoringProfile.created_at >= since)
                .order_by(WorkspaceOperatorResolutionCommitmentMonitoringProfile.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        commitment_monitoring_rows = [
            item
            for item in commitment_monitoring_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
    commitment_outcome_rows = (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentOutcomeProfile)
                .where(WorkspaceOperatorResolutionCommitmentOutcomeProfile.created_at >= since)
                .order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        commitment_outcome_rows = [
            item
            for item in commitment_outcome_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
    input_rows_by_id = {int(item.id): item for item in input_rows}

    execution_rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.created_at >= since)
                .order_by(CapabilityExecution.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    execution_rows = [
        item
        for item in execution_rows
        if isinstance(item.execution_truth_json, dict)
        and str(item.execution_truth_json.get("contract", "")).strip()
        == "execution_truth_v1"
    ]
    if run_id:
        execution_rows = [
            item
            for item in execution_rows
            if _execution_run_id(
                row=item,
                input_event=input_rows_by_id.get(int(item.input_event_id or 0)),
            )
            == run_id
        ]
    if run_id:
        source_rows = [
            item
            for item in source_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]

    max_count = max(1, min(100, int(max_questions)))
    min_friction = max(2, int(min_soft_friction_count))

    created: list[WorkspaceInquiryQuestion] = []
    decisions: list[dict] = []

    low_confidence_warnings = [
        item
        for item in constraint_rows
        if "target_confidence_threshold" in _extract_warning_keys(item)
    ]
    if (
        low_confidence_warnings
        and len(low_confidence_warnings) >= min_friction
        and (strategies or plan)
    ):
        origin_strategy = strategies[0] if strategies else None
        origin_goal = strategy_goals[0] if strategy_goals else None
        managed_scope = _inquiry_managed_scope_from_context(
            origin_goal=origin_goal,
            plan=plan,
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
        )
        candidate_paths = [
            {
                "path_id": "shift_strategy_and_unblock",
                "label": "Bias strategy toward reobserve and continue with updated confidence guardrails",
                "effect_type": "shift_strategy_and_unblock",
                "params": {
                    "influence_delta": 0.15,
                },
            },
            {
                "path_id": "trigger_rescan",
                "label": "Trigger immediate adjacent-zone reobserve before next action",
                "effect_type": "trigger_rescan",
                "params": {"proposal_type": "rescan_zone"},
            },
            {
                "path_id": "hold_manual_confirmation",
                "label": "Hold and require manual confirmation until confidence improves",
                "effect_type": "no_action",
                "params": {},
            },
        ]
        dedupe_key = (
            f"target_confidence_too_low:goal:{int(origin_goal.id) if origin_goal else 0}:"
            f"strategy:{int(origin_strategy.id) if origin_strategy else 0}:plan:{int(plan.id) if plan else 0}"
        )
        row, decision = await _create_governed_inquiry_question(
            source=source,
            actor=actor,
            dedupe_key=dedupe_key,
            trigger_type="target_confidence_too_low",
            uncertainty_type="perception_confidence",
            origin_strategy_goal_id=int(origin_goal.id) if origin_goal else None,
            origin_strategy_id=int(origin_strategy.id) if origin_strategy else None,
            origin_plan_id=int(plan.id) if plan else None,
            why_answer_matters="Low-confidence target evidence can invalidate the current strategy ordering and action sequence.",
            waiting_decision="Whether to continue current plan sequencing or reobserve before execution.",
            no_answer_behavior="System keeps manual confirmation gating and avoids autonomous progression.",
            candidate_answer_paths=candidate_paths,
            urgency="high",
            priority="high",
            safe_default_if_unanswered="hold_manual_confirmation",
            trigger_evidence_json={
                "managed_scope": managed_scope,
                "warning_count": len(low_confidence_warnings),
                "sample_evaluation_ids": [int(item.id) for item in low_confidence_warnings[:10]],
            },
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                **({"managed_scope": managed_scope} if managed_scope else {}),
                "objective62": True,
            },
            db=db,
        )
        decisions.append(decision)
        if row:
            created.append(row)

    if len(created) < max_count:
        communication_count = len(input_rows)
        external_count = len(external_memory)
        if communication_count >= 2 and external_count >= 1:
            origin_goal = strategy_goals[0] if strategy_goals else None
            origin_strategy = strategies[0] if strategies else None
            candidate_paths = [
                {
                    "path_id": "prioritize_workspace_stability",
                    "label": "Prioritize workspace-state stability checks before external-context actions",
                    "effect_type": "shift_strategy",
                    "params": {"influence_delta": 0.1},
                },
                {
                    "path_id": "trigger_context_rescan",
                    "label": "Gather additional perception evidence to resolve domain disagreement",
                    "effect_type": "trigger_rescan",
                    "params": {"proposal_type": "monitor_search_adjacent_zone"},
                },
                {
                    "path_id": "defer_external_context",
                    "label": "Defer external-context branch and keep current safe prioritization",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            dedupe_key = (
                f"conflicting_domain_evidence:goal:{int(origin_goal.id) if origin_goal else 0}:"
                f"strategy:{int(origin_strategy.id) if origin_strategy else 0}:run:{run_id or 'global'}"
            )
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="conflicting_domain_evidence",
                uncertainty_type="cross_domain_conflict",
                origin_strategy_goal_id=int(origin_goal.id) if origin_goal else None,
                origin_strategy_id=int(origin_strategy.id) if origin_strategy else None,
                origin_plan_id=int(plan.id) if plan else None,
                why_answer_matters="Communication and external context signals suggest different priorities for the same planning window.",
                waiting_decision="Which domain should dominate near-term strategy ranking and action sequencing.",
                no_answer_behavior="Default to workspace stability and operator confirmation over aggressive reprioritization.",
                candidate_answer_paths=candidate_paths,
                urgency="medium",
                priority="high",
                safe_default_if_unanswered="defer_external_context",
                trigger_evidence_json={
                    "communication_event_count": communication_count,
                    "external_memory_count": external_count,
                    "sample_input_event_ids": [int(item.id) for item in input_rows[:10]],
                    "sample_external_memory_ids": [int(item.id) for item in external_memory[:10]],
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) < max_count and strategies:
        blocked = [
            item
            for item in strategies
            if not (
                item.influenced_plan_ids_json
                if isinstance(item.influenced_plan_ids_json, list)
                else []
            )
        ]
        if blocked:
            strategy = blocked[0]
            candidate_paths = [
                {
                    "path_id": "request_scope_rescan",
                    "label": "Request targeted rescan for blocked strategy scope",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": strategy.target_scope,
                    },
                },
                {
                    "path_id": "lower_strategy_weight",
                    "label": "Temporarily lower blocked strategy influence",
                    "effect_type": "shift_strategy",
                    "params": {"influence_delta": -0.12},
                },
            ]
            dedupe_key = (
                f"strategy_blocked_by_missing_information:strategy:{int(strategy.id)}"
            )
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="strategy_blocked_by_missing_information",
                uncertainty_type="strategy_blocked",
                origin_strategy_goal_id=None,
                origin_strategy_id=int(strategy.id),
                origin_plan_id=int(plan.id) if plan else None,
                why_answer_matters="Active strategy has not influenced planning outcomes and appears blocked by missing information.",
                waiting_decision="Whether to gather missing evidence or down-rank the blocked strategy.",
                no_answer_behavior="Strategy remains active but no autonomous ranking boost is applied.",
                candidate_answer_paths=candidate_paths,
                urgency="medium",
                priority="normal",
                safe_default_if_unanswered="lower_strategy_weight",
                trigger_evidence_json={
                    "strategy_id": int(strategy.id),
                    "strategy_type": strategy.strategy_type,
                    "target_scope": strategy.target_scope,
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) < max_count and stewardship_cycles:
        for cycle in stewardship_cycles:
            cycle_meta = _metadata_json(cycle.metadata_json)
            assessment = (
                cycle_meta.get("assessment", {})
                if isinstance(cycle_meta.get("assessment", {}), dict)
                else {}
            )
            post = (
                assessment.get("post", {})
                if isinstance(assessment.get("post", {}), dict)
                else {}
            )
            system_metrics = (
                post.get("system_metrics", {})
                if isinstance(post.get("system_metrics", {}), dict)
                else {}
            )
            execution_truth_summary = (
                post.get("execution_truth_summary", {})
                if isinstance(post.get("execution_truth_summary", {}), dict)
                else {}
            )
            inquiry_candidates = (
                post.get("inquiry_candidates", [])
                if isinstance(post.get("inquiry_candidates", []), list)
                else []
            )
            degraded_signals = (
                post.get("deviation_signals", [])
                if isinstance(post.get("deviation_signals", []), list)
                else []
            )
            key_objects = (
                post.get("scope_metrics", {}).get("key_objects", [])
                if isinstance(post.get("scope_metrics", {}), dict)
                else []
            )
            if not inquiry_candidates and not degraded_signals:
                continue

            managed_scope = (
                str(cycle_meta.get("managed_scope", "global")).strip() or "global"
            )
            stewardship_id = int(cycle.stewardship_id or 0)
            missing_key_objects = [
                str(item.get("object_name", "")).strip()
                for item in key_objects
                if isinstance(item, dict)
                and not bool(item.get("is_known", False))
                and str(item.get("object_name", "")).strip()
            ]
            candidate_paths = [
                {
                    "path_id": "stabilize_scope_now",
                    "label": "Trigger targeted rescan for the degraded stewardship scope",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": managed_scope,
                    },
                },
                {
                    "path_id": "tighten_scope_tracking",
                    "label": "Tighten stewardship tracking thresholds for this scope",
                    "effect_type": "update_stewardship_target",
                    "params": {
                        "stewardship_id": stewardship_id,
                        "zone_freshness_seconds": 300,
                        "max_system_drift_rate": 0.2,
                        "proactive_drift_monitoring": True,
                        "add_key_objects": missing_key_objects,
                    },
                },
                {
                    "path_id": "request_stewardship_improvement",
                    "label": "Create bounded improvement review for stewardship policy",
                    "effect_type": "create_proposal",
                    "params": {
                        "proposal_type": "capability_workflow_improvement",
                        "affected_component": "environment_stewardship",
                    },
                },
                {
                    "path_id": "keep_monitoring",
                    "label": "Keep monitoring and do not mutate stewardship policy yet",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            dedupe_key = f"stewardship_persistent_degradation:stewardship:{stewardship_id}:scope:{managed_scope}:run:{run_id or 'global'}"
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="stewardship_persistent_degradation",
                uncertainty_type="environment_stability",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters="Stewardship is repeatedly detecting degraded environment state, so the maintenance loop may need stronger tracking or a different corrective path.",
                waiting_decision="Whether to immediately stabilize the degraded scope, tighten stewardship tracking, or keep monitoring without policy changes.",
                no_answer_behavior="Stewardship keeps monitoring and uses conservative corrective behavior only.",
                candidate_answer_paths=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="keep_monitoring",
                trigger_evidence_json={
                    "stewardship_id": stewardship_id,
                    "cycle_id": int(cycle.id),
                    "managed_scope": managed_scope,
                    "system_metrics": system_metrics,
                    "execution_truth_signal_count": int(execution_truth_summary.get("signal_count", 0) or 0),
                    "execution_truth_signal_types": execution_truth_summary.get("signal_types", []),
                    "inquiry_candidates": inquiry_candidates,
                    "degraded_signal_count": len(degraded_signals),
                    "missing_key_objects": missing_key_objects,
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective60_source": True,
                    "objective80_execution_truth": bool(execution_truth_summary.get("signal_count", 0)),
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)
            break

    if len(created) < max_count and execution_rows:
        for execution in execution_rows:
            truth = (
                execution.execution_truth_json
                if isinstance(execution.execution_truth_json, dict)
                else {}
            )
            signal_rows = derive_execution_truth_signals(truth)
            if not signal_rows:
                continue

            capability_name = (
                str(execution.capability_name or "workspace capability").strip()
                or "workspace capability"
            )
            signal_types = [
                str(item.get("signal_type", "")).strip()
                for item in signal_rows
                if isinstance(item, dict) and str(item.get("signal_type", "")).strip()
            ]
            if not signal_types:
                continue

            candidate_paths = [
                {
                    "path_id": "request_execution_truth_review",
                    "label": "Create a bounded improvement review for the execution-truth deviation pattern",
                    "effect_type": "create_proposal",
                    "params": {
                        "proposal_type": "capability_workflow_improvement",
                        "affected_component": "execution_truth_bridge",
                    },
                },
                {
                    "path_id": "trigger_execution_rescan",
                    "label": "Trigger a bounded rescan or retry-observation before trusting this runtime pattern",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": capability_name,
                    },
                },
                {
                    "path_id": "keep_monitoring_execution_truth",
                    "label": "Keep monitoring execution truth without changing policy yet",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            scope_refs = [item for item in sorted(execution_truth_scope_refs(execution)) if item and item != "global"]
            managed_scope = scope_refs[0] if scope_refs else "global"
            dedupe_key = f"execution_truth_runtime_mismatch:scope:{managed_scope}:run:{run_id or 'global'}"
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="execution_truth_runtime_mismatch",
                uncertainty_type="execution_runtime_truth",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters="Observed execution truth diverged from expected runtime behavior, so downstream planning assumptions may be too optimistic.",
                waiting_decision="Whether to request a bounded execution-truth improvement review, gather more runtime evidence, or keep monitoring the drift pattern.",
                no_answer_behavior="System keeps execution truth as reasoning evidence only and does not expand the adaptation scope automatically.",
                candidate_answer_paths=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="keep_monitoring_execution_truth",
                trigger_evidence_json={
                    "execution_id": int(execution.id),
                    "managed_scope": managed_scope,
                    "capability_name": capability_name,
                    "execution_status": str(execution.status or "").strip(),
                    "runtime_outcome": str(truth.get("runtime_outcome", "")).strip(),
                    "scope_refs": scope_refs,
                    "signal_types": signal_types,
                    "signal_count": len(signal_rows),
                    "execution_truth": truth,
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective80_execution_truth": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)
            break

    if len(created) < max_count:
        soft_warning_rows = []
        for item in constraint_rows:
            warnings = (
                item.warnings_json if isinstance(item.warnings_json, list) else []
            )
            if any(
                isinstance(warning, dict) and not bool(warning.get("hard", False))
                for warning in warnings
            ):
                soft_warning_rows.append(item)
        if len(soft_warning_rows) >= min_friction:
            dedupe_key = f"repeated_soft_constraint_friction:run:{run_id or 'global'}"
            managed_scope = _inquiry_managed_scope_from_context(
                origin_goal=strategy_goals[0] if strategy_goals else None,
                plan=plan,
                metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
            )
            candidate_paths = [
                    {
                        "path_id": "propose_soft_constraint_adjustment",
                        "label": "Create bounded proposal to adjust soft policy/constraint weighting",
                        "effect_type": "create_proposal",
                        "params": {
                            "proposal_type": "soft_constraint_weight_adjustment",
                            "affected_component": "constraint_engine",
                        },
                    },
                    {
                        "path_id": "keep_policy_and_monitor",
                        "label": "Keep policy unchanged and continue monitoring",
                        "effect_type": "no_action",
                        "params": {},
                    },
                ]
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="repeated_soft_constraint_friction",
                uncertainty_type="constraint_friction",
                origin_strategy_goal_id=int(strategy_goals[0].id) if strategy_goals else None,
                origin_strategy_id=int(strategies[0].id) if strategies else None,
                origin_plan_id=int(plan.id) if plan else None,
                why_answer_matters="Repeated soft-constraint friction indicates policy uncertainty that can change future action quality.",
                waiting_decision="Whether to propose a bounded policy adjustment or continue observation-only mode.",
                no_answer_behavior="No policy mutation is applied; friction remains review-only.",
                candidate_answer_paths=candidate_paths,
                urgency="medium",
                priority="normal",
                safe_default_if_unanswered="keep_policy_and_monitor",
                trigger_evidence_json={
                    "managed_scope": managed_scope,
                    "soft_warning_count": len(soft_warning_rows),
                    "sample_evaluation_ids": [int(item.id) for item in soft_warning_rows[:10]],
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    **({"managed_scope": managed_scope} if managed_scope else {}),
                    "objective62": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) < max_count and strategy_goals and source_rows:
        noisy = [
            item for item in source_rows if int(item.low_confidence_count or 0) >= 2
        ]
        if noisy:
            source_row = noisy[0]
            dedupe_key = (
                f"low_confidence_perception_blocking_goal:source:{int(source_row.id)}"
            )
            candidate_paths = [
                    {
                        "path_id": "set_operator_required_boundary",
                        "label": "Temporarily enforce operator-required autonomy for this uncertainty region",
                        "effect_type": "change_autonomy",
                        "params": {"target_level": "operator_required"},
                    },
                    {
                        "path_id": "reobserve_then_continue",
                        "label": "Trigger reobserve proposal and continue after evidence refresh",
                        "effect_type": "trigger_rescan",
                        "params": {"proposal_type": "target_reobserve"},
                    },
                ]
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="low_confidence_perception_blocking_strategic_goal",
                uncertainty_type="perception_blocking_goal",
                origin_strategy_goal_id=int(strategy_goals[0].id),
                origin_strategy_id=int(strategies[0].id) if strategies else None,
                origin_plan_id=int(plan.id) if plan else None,
                why_answer_matters="Sustained low-confidence perception is blocking strategic-goal execution safety.",
                waiting_decision="Whether to tighten autonomy or refresh evidence before continuing.",
                no_answer_behavior="Autonomy remains conservative and execution waits for stronger evidence.",
                candidate_answer_paths=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="set_operator_required_boundary",
                trigger_evidence_json={
                    "source_id": int(source_row.id),
                    "source_type": source_row.source_type,
                    "low_confidence_count": int(source_row.low_confidence_count or 0),
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) < max_count and plan is not None:
        ranked = (
            plan.ranked_goals_json if isinstance(plan.ranked_goals_json, list) else []
        )
        if len(ranked) >= 2:
            first = ranked[0] if isinstance(ranked[0], dict) else {}
            second = ranked[1] if isinstance(ranked[1], dict) else {}
            score_first = float(first.get("score", 0.0) or 0.0)
            score_second = float(second.get("score", 0.0) or 0.0)
            if abs(score_first - score_second) <= 0.05:
                dedupe_key = f"ambiguous_next_action:plan:{int(plan.id)}"
                managed_scope = _inquiry_managed_scope_from_context(
                    origin_goal=strategy_goals[0] if strategy_goals else None,
                    plan=plan,
                    metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
                )
                candidate_paths = [
                        {
                            "path_id": "unblock_current_plan",
                            "label": "Proceed with top-ranked path and unblock current plan",
                            "effect_type": "unblock_plan",
                            "params": {},
                        },
                        {
                            "path_id": "adjust_strategy_ranking",
                            "label": "Apply strategy-rank shift before selecting next action",
                            "effect_type": "shift_strategy",
                            "params": {"influence_delta": 0.08},
                        },
                        {
                            "path_id": "hold_for_operator_priority",
                            "label": "Keep plan blocked until operator priority preference is provided",
                            "effect_type": "no_action",
                            "params": {},
                        },
                    ]
                row, decision = await _create_governed_inquiry_question(
                    source=source,
                    actor=actor,
                    dedupe_key=dedupe_key,
                    trigger_type="ambiguous_next_action_under_multiple_valid_paths",
                    uncertainty_type="action_path_ambiguity",
                    origin_strategy_goal_id=int(strategy_goals[0].id) if strategy_goals else None,
                    origin_strategy_id=int(strategies[0].id) if strategies else None,
                    origin_plan_id=int(plan.id),
                    why_answer_matters="Multiple valid next actions are near-tied; answer selection can materially change plan quality.",
                    waiting_decision="Which near-tied path should be chosen for the next action stage.",
                    no_answer_behavior="Plan remains conservative and avoids autonomous tie-breaking.",
                    candidate_answer_paths=candidate_paths,
                    urgency="medium",
                    priority="normal",
                    safe_default_if_unanswered="hold_for_operator_priority",
                    trigger_evidence_json={
                        "managed_scope": managed_scope,
                        "score_top_1": round(score_first, 6),
                        "score_top_2": round(score_second, 6),
                        "goal_key_top_1": str(first.get("goal_key", "")),
                        "goal_key_top_2": str(second.get("goal_key", "")),
                    },
                    metadata_json={
                        **(metadata_json if isinstance(metadata_json, dict) else {}),
                        **({"managed_scope": managed_scope} if managed_scope else {}),
                        "objective62": True,
                    },
                    db=db,
                )
                decisions.append(decision)
                if row:
                    created.append(row)

    if len(created) < max_count and commitment_monitoring_rows:
        monitoring = next(
            (
                item
                for item in commitment_monitoring_rows
                if str(item.governance_state or "").strip()
                in {"watch", "drifting", "violating", "expired"}
                or float(item.health_score or 0.0) <= 0.6
            ),
            None,
        )
        if monitoring is not None:
            candidate_paths = [
                {
                    "path_id": "maintain_commitment",
                    "label": "Keep the commitment active and continue monitoring",
                    "effect_type": "no_action",
                    "params": {"commitment_id": int(monitoring.commitment_id)},
                },
                {
                    "path_id": "revoke_commitment",
                    "label": "Revoke the commitment because it is no longer helping",
                    "effect_type": "update_commitment_status",
                    "params": {
                        "commitment_id": int(monitoring.commitment_id),
                        "target_status": "revoked",
                    },
                },
                {
                    "path_id": "expire_commitment_and_reassess",
                    "label": "Expire the commitment now and request fresh operator guidance later",
                    "effect_type": "update_commitment_status",
                    "params": {
                        "commitment_id": int(monitoring.commitment_id),
                        "target_status": "expired",
                    },
                },
            ]
            dedupe_key = (
                f"operator_commitment_drift_detected:commitment:{int(monitoring.commitment_id)}:"
                f"state:{str(monitoring.governance_state or '').strip() or 'unknown'}"
            )
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="operator_commitment_drift_detected",
                uncertainty_type="commitment_alignment",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters="An active operator commitment is drifting away from current workspace evidence and may now be causing unnecessary execution friction.",
                waiting_decision="Whether to keep, revoke, or expire the drifting commitment.",
                no_answer_behavior="System keeps the commitment active and continues monitoring without changing commitment status automatically.",
                candidate_answer_paths=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="maintain_commitment",
                trigger_evidence_json={
                    "monitoring_id": int(monitoring.id),
                    "monitoring_commitment_id": int(monitoring.commitment_id),
                    "commitment_id": int(monitoring.commitment_id),
                    "managed_scope": str(monitoring.managed_scope or "").strip(),
                    "governance_state": str(monitoring.governance_state or "").strip(),
                    "governance_decision": str(monitoring.governance_decision or "").strip(),
                    "drift_score": float(monitoring.drift_score or 0.0),
                    "health_score": float(monitoring.health_score or 0.0),
                    "potential_violation_count": int(
                        monitoring.potential_violation_count or 0
                    ),
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective86_commitment_monitoring": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) < max_count and commitment_outcome_rows:
        outcome = next(
            (
                item
                for item in commitment_outcome_rows
                if str(item.outcome_status or "").strip() in {"ineffective", "harmful", "abandoned"}
                and not bool(
                    (
                        item.metadata_json
                        if isinstance(item.metadata_json, dict)
                        else {}
                    ).get("learning_acknowledged", False)
                )
            ),
            None,
        )
        if outcome is not None:
            candidate_paths = [
                {
                    "path_id": "avoid_similar_commitments",
                    "label": "Avoid similar commitments for this scope until evidence improves",
                    "effect_type": "record_commitment_learning_bias",
                    "params": {
                        "outcome_id": int(outcome.id),
                        "learning_bias": "avoid_similar_commitments",
                    },
                },
                {
                    "path_id": "repeat_only_with_more_evidence",
                    "label": "Allow similar commitments only when stronger evidence is present",
                    "effect_type": "record_commitment_learning_bias",
                    "params": {
                        "outcome_id": int(outcome.id),
                        "learning_bias": "repeat_only_with_more_evidence",
                    },
                },
                {
                    "path_id": "keep_monitoring_learning",
                    "label": "Keep learning in observation mode without changing future bias yet",
                    "effect_type": "no_action",
                    "params": {"outcome_id": int(outcome.id)},
                },
            ]
            dedupe_key = (
                f"operator_commitment_learning_review:outcome:{int(outcome.id)}:"
                f"status:{str(outcome.outcome_status or '').strip() or 'unknown'}"
            )
            row, decision = await _create_governed_inquiry_question(
                source=source,
                actor=actor,
                dedupe_key=dedupe_key,
                trigger_type="operator_commitment_learning_review",
                uncertainty_type="commitment_learning_bias",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters=(
                    "A prior operator commitment ended poorly, and repeating the same pattern"
                    " could reintroduce the same friction or harm."
                ),
                waiting_decision=(
                    "Whether future commitments of this type should be avoided, allowed only"
                    " with more evidence, or kept in observation mode."
                ),
                no_answer_behavior=(
                    "System keeps the learned signal as advisory only and does not tighten"
                    " future commitment bias automatically."
                ),
                candidate_answer_paths=candidate_paths,
                urgency="medium",
                priority="high",
                safe_default_if_unanswered="keep_monitoring_learning",
                trigger_evidence_json={
                    "outcome_id": int(outcome.id),
                    "commitment_id": int(outcome.commitment_id),
                    "managed_scope": str(outcome.managed_scope or "").strip(),
                    "decision_type": str(outcome.decision_type or "").strip(),
                    "outcome_status": str(outcome.outcome_status or "").strip(),
                    "pattern_summary": (
                        outcome.pattern_summary_json
                        if isinstance(outcome.pattern_summary_json, dict)
                        else {}
                    ),
                    "learning_signals": (
                        outcome.learning_signals_json
                        if isinstance(outcome.learning_signals_json, dict)
                        else {}
                    ),
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective87_commitment_learning": True,
                },
                db=db,
            )
            decisions.append(decision)
            if row:
                created.append(row)

    if len(created) > max_count:
        created = created[:max_count]

    await db.flush()
    return {"questions": created, "decisions": decisions}


async def list_inquiry_questions(
    *,
    db: AsyncSession,
    status: str = "",
    uncertainty_type: str = "",
    limit: int = 50,
) -> list[WorkspaceInquiryQuestion]:
    rows = (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion).order_by(
                    WorkspaceInquiryQuestion.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    if status:
        requested = status.strip().lower()
        rows = [
            item for item in rows if str(item.status or "").strip().lower() == requested
        ]
    if uncertainty_type:
        requested_uncertainty = uncertainty_type.strip().lower()
        rows = [
            item
            for item in rows
            if str(item.uncertainty_type or "").strip().lower() == requested_uncertainty
        ]
    return rows[: max(1, min(500, int(limit)))]


async def get_inquiry_question(
    *, question_id: int, db: AsyncSession
) -> WorkspaceInquiryQuestion | None:
    return (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion).where(
                    WorkspaceInquiryQuestion.id == question_id
                )
            )
        )
        .scalars()
        .first()
    )


async def answer_inquiry_question(
    *,
    row: WorkspaceInquiryQuestion,
    actor: str,
    selected_path_id: str,
    answer_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceInquiryQuestion, dict]:
    if str(row.status or "") != "open":
        raise ValueError("inquiry_question_not_open")

    candidate_paths = (
        row.candidate_answer_paths_json
        if isinstance(row.candidate_answer_paths_json, list)
        else []
    )
    selected = None
    for item in candidate_paths:
        if not isinstance(item, dict):
            continue
        if str(item.get("path_id", "")).strip() == selected_path_id:
            selected = item
            break
    if not selected:
        raise ValueError("inquiry_path_not_found")

    effect_type = str(selected.get("effect_type", "no_action")).strip() or "no_action"
    params = (
        selected.get("params", {})
        if isinstance(selected.get("params", {}), dict)
        else {}
    )

    applied_effect: dict = {
        "effect_type": effect_type,
        "selected_path_id": selected_path_id,
        "applied": False,
        "allowed_answer_effects": _allowed_answer_effects(candidate_paths),
        "allowed_downstream_effect": effect_type,
    }

    if (
        effect_type in {"unblock_plan", "shift_strategy_and_unblock"}
        and row.origin_plan_id is not None
    ):
        plan = await db.get(WorkspaceHorizonPlan, int(row.origin_plan_id))
        if plan:
            plan.status = "active"
            plan.metadata_json = {
                **(plan.metadata_json if isinstance(plan.metadata_json, dict) else {}),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "answered_by": actor,
                },
            }
            applied_effect["plan_unblocked"] = True
            applied_effect["plan_id"] = int(plan.id)
            applied_effect["applied"] = True

    if (
        effect_type in {"shift_strategy", "shift_strategy_and_unblock"}
        and row.origin_strategy_id is not None
    ):
        strategy = await db.get(
            WorkspaceEnvironmentStrategy, int(row.origin_strategy_id)
        )
        if strategy:
            delta = float(params.get("influence_delta", 0.1) or 0.1)
            strategy.influence_weight = _bounded(
                float(strategy.influence_weight or 0.0) + delta
            )
            strategy.current_status = "active"
            strategy.status_reason = f"inquiry_answer:{selected_path_id}"
            strategy.metadata_json = {
                **(
                    strategy.metadata_json
                    if isinstance(strategy.metadata_json, dict)
                    else {}
                ),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "influence_delta": delta,
                },
            }
            applied_effect["strategy_shifted"] = True
            applied_effect["strategy_id"] = int(strategy.id)
            applied_effect["strategy_influence_weight"] = float(
                strategy.influence_weight
            )
            applied_effect["applied"] = True

    if effect_type == "trigger_rescan":
        proposal_type = (
            str(params.get("proposal_type", "rescan_zone")).strip() or "rescan_zone"
        )
        related_zone = (
            str(params.get("related_zone", "workspace")).strip() or "workspace"
        )
        proposal = WorkspaceProposal(
            proposal_type=proposal_type,
            title=f"Inquiry-triggered {proposal_type.replace('_', ' ')}",
            description=(
                f"Inquiry question {row.id} selected path '{selected_path_id}' requested additional observation evidence."
            ),
            status="pending",
            confidence=0.72,
            priority_score=0.7,
            priority_reason="inquiry_unresolved_uncertainty",
            source="inquiry",
            related_zone=related_zone,
            related_object_id=None,
            source_execution_id=None,
            trigger_json={
                "question_id": int(row.id),
                "selected_path_id": selected_path_id,
                "uncertainty_type": row.uncertainty_type,
            },
            metadata_json={
                "objective62": True,
                "inquiry_question_id": int(row.id),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(proposal)
        await db.flush()
        applied_effect["workspace_proposal_created"] = True
        applied_effect["workspace_proposal_id"] = int(proposal.id)
        applied_effect["applied"] = True

    if effect_type == "change_autonomy":
        target_level = (
            str(params.get("target_level", "operator_required")).strip()
            or "operator_required"
        )
        profile = (
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
        if profile:
            profile.current_level = target_level
            profile.profile_status = "applied"
            profile.adjustment_reason = f"inquiry_answer:{selected_path_id}"
            profile.metadata_json = {
                **(
                    profile.metadata_json
                    if isinstance(profile.metadata_json, dict)
                    else {}
                ),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "target_level": target_level,
                },
            }
            applied_effect["autonomy_changed"] = True
            applied_effect["autonomy_boundary_id"] = int(profile.id)
            applied_effect["autonomy_level"] = target_level
            applied_effect["applied"] = True

    if effect_type == "create_proposal":
        proposal_type = (
            str(params.get("proposal_type", "policy_adjustment")).strip()
            or "policy_adjustment"
        )
        affected_component = (
            str(params.get("affected_component", "inquiry")).strip() or "inquiry"
        )
        trigger_evidence = (
            row.trigger_evidence_json
            if isinstance(row.trigger_evidence_json, dict)
            else {}
        )
        proposal_evidence = {
            "question_id": int(row.id),
            "selected_path_id": selected_path_id,
            "uncertainty_type": row.uncertainty_type,
        }
        proposal_metadata = {
            "objective62": True,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "inquiry_trigger_type": str(row.trigger_type or "").strip(),
        }
        if str(row.trigger_type or "").strip() == "execution_truth_runtime_mismatch":
            proposal_evidence.update(
                {
                    "execution_id": int(trigger_evidence.get("execution_id", 0) or 0),
                    "capability_name": str(
                        trigger_evidence.get("capability_name", "")
                    ).strip(),
                    "signal_types": trigger_evidence.get("signal_types", []),
                    "signal_count": int(trigger_evidence.get("signal_count", 0) or 0),
                }
            )
            proposal_metadata["objective80_execution_truth"] = True
        proposal = WorkspaceImprovementProposal(
            source="objective62",
            actor=actor,
            proposal_type=proposal_type,
            trigger_pattern="inquiry_answer_generated",
            evidence_summary=f"Created from answered inquiry question {row.id}",
            evidence_json=proposal_evidence,
            affected_component=affected_component,
            suggested_change=f"Investigate uncertainty path '{selected_path_id}' and apply bounded improvement.",
            confidence=0.65,
            safety_class="bounded_review",
            risk_summary="generated_from_inquiry_requires_review",
            test_recommendation="Run focused + full integration regression before promotion",
            status="proposed",
            review_reason="",
            metadata_json=proposal_metadata,
        )
        db.add(proposal)
        await db.flush()
        applied_effect["improvement_proposal_created"] = True
        applied_effect["improvement_proposal_id"] = int(proposal.id)
        applied_effect["applied"] = True

    if effect_type == "update_stewardship_target":
        stewardship_id = int(params.get("stewardship_id", 0) or 0)
        if stewardship_id <= 0:
            trigger_evidence = (
                row.trigger_evidence_json
                if isinstance(row.trigger_evidence_json, dict)
                else {}
            )
            stewardship_id = int(trigger_evidence.get("stewardship_id", 0) or 0)
        stewardship = await _get_stewardship_state(stewardship_id=stewardship_id, db=db)
        if stewardship:
            current_target = (
                stewardship.target_environment_state_json
                if isinstance(stewardship.target_environment_state_json, dict)
                else {}
            )
            updated_target = {
                **current_target,
            }
            scalar_keys = [
                "zone_freshness_seconds",
                "max_system_drift_rate",
                "max_zone_drift_rate",
                "max_zone_uncertainty_score",
                "max_object_uncertainty_score",
                "max_missing_key_objects",
                "proactive_drift_monitoring",
            ]
            for key in scalar_keys:
                if key in params:
                    updated_target[key] = params.get(key)
            if "add_key_objects" in params:
                updated_target["key_objects"] = _merge_unique_strings(
                    current_target.get("key_objects", []),
                    params.get("add_key_objects", []),
                )

            stewardship.target_environment_state_json = updated_target
            stewardship.last_decision_summary = f"Inquiry {row.id} updated stewardship target via path '{selected_path_id}'."
            stewardship.metadata_json = {
                **(
                    stewardship.metadata_json
                    if isinstance(stewardship.metadata_json, dict)
                    else {}
                ),
                "last_inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "answered_by": actor,
                    "updated_target_environment_state": updated_target,
                },
            }
            applied_effect["stewardship_target_updated"] = True
            applied_effect["stewardship_id"] = int(stewardship.id)
            applied_effect["updated_target_environment_state"] = updated_target
            applied_effect["applied"] = True

    if effect_type == "update_commitment_status":
        commitment_id = int(params.get("commitment_id", 0) or 0)
        if commitment_id <= 0:
            trigger_evidence = (
                row.trigger_evidence_json
                if isinstance(row.trigger_evidence_json, dict)
                else {}
            )
            commitment_id = int(trigger_evidence.get("commitment_id", 0) or 0)
        target_status = str(params.get("target_status", "")).strip().lower()
        if commitment_id > 0 and target_status in {
            "revoked",
            "expired",
            "active",
            "satisfied",
            "abandoned",
            "ineffective",
            "harmful",
            "superseded",
        }:
            commitment = await db.get(
                WorkspaceOperatorResolutionCommitment,
                commitment_id,
            )
            if commitment is not None:
                prior_status = str(commitment.status or "").strip()
                commitment.status = target_status
                if target_status == "expired":
                    commitment.expires_at = datetime.now(timezone.utc)
                commitment.metadata_json = {
                    **(
                        commitment.metadata_json
                        if isinstance(commitment.metadata_json, dict)
                        else {}
                    ),
                    "last_inquiry_commitment_update": {
                        "question_id": int(row.id),
                        "selected_path_id": selected_path_id,
                        "answered_by": actor,
                        "target_status": target_status,
                    },
                }
                applied_effect["commitment_status_updated"] = True
                applied_effect["commitment_id"] = int(commitment.id)
                applied_effect["prior_commitment_status"] = prior_status
                applied_effect["commitment_status"] = target_status
                applied_effect["applied"] = True

    if effect_type == "record_commitment_learning_bias":
        outcome_id = int(params.get("outcome_id", 0) or 0)
        learning_bias = str(params.get("learning_bias", "") or "").strip()
        if outcome_id > 0 and learning_bias:
            outcome = await db.get(
                WorkspaceOperatorResolutionCommitmentOutcomeProfile,
                outcome_id,
            )
            if outcome is not None:
                prior_learning = (
                    outcome.learning_signals_json
                    if isinstance(outcome.learning_signals_json, dict)
                    else {}
                )
                repeat_bias = prior_learning.get("repeat_commitment_bias", "neutral")
                inquiry_bias = prior_learning.get("inquiry_bias", "monitor_commitment_pattern")
                if learning_bias == "avoid_similar_commitments":
                    repeat_bias = "avoid"
                    inquiry_bias = "ask_before_similar_commitment"
                elif learning_bias == "repeat_only_with_more_evidence":
                    repeat_bias = "cautious"
                    inquiry_bias = "ask_before_similar_commitment"
                outcome.learning_signals_json = {
                    **prior_learning,
                    "repeat_commitment_bias": repeat_bias,
                    "inquiry_bias": inquiry_bias,
                }
                outcome.metadata_json = {
                    **(
                        outcome.metadata_json
                        if isinstance(outcome.metadata_json, dict)
                        else {}
                    ),
                    "learning_acknowledged": True,
                    "operator_learning_bias": {
                        "question_id": int(row.id),
                        "selected_path_id": selected_path_id,
                        "answered_by": actor,
                        "bias": learning_bias,
                    },
                }
                applied_effect["commitment_learning_bias_recorded"] = True
                applied_effect["outcome_id"] = int(outcome.id)
                applied_effect["learning_bias"] = learning_bias
                applied_effect["applied"] = True

    row.status = "answered"
    row.selected_path_id = selected_path_id
    row.answer_json = {
        **(answer_json if isinstance(answer_json, dict) else {}),
        "selected_path": selected,
    }
    row.applied_effect_json = applied_effect
    row.answered_by = actor
    row.answered_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "objective62_answered": True,
    }
    policy = _policy_metadata(row)
    applied_effect["decision_state"] = str(policy.get("decision_state", "")).strip()
    applied_effect["decision_reason"] = str(policy.get("reason", "")).strip()
    applied_effect["material_state_change"] = bool(applied_effect.get("applied", False))
    applied_effect["state_delta_summary"] = _state_delta_summary(applied_effect)

    await db.flush()
    return row, applied_effect


def to_inquiry_question_out(row: WorkspaceInquiryQuestion) -> dict:
    return _question_payload(row)
