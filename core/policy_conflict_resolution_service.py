from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_readiness_service import (
    execution_readiness_confidence,
    execution_readiness_policy_effects,
    execution_readiness_precedence,
    execution_readiness_posture,
    load_latest_execution_readiness,
)
from core.execution_truth_service import execution_truth_freshness
from core.models import (
    WorkspaceExecutionTruthGovernanceProfile,
    WorkspaceOperatorResolutionCommitment,
    WorkspacePolicyConflictProfile,
    WorkspacePolicyConflictResolutionEvent,
    WorkspaceProposal,
)
from core.operator_preference_convergence_service import (
    latest_scope_learned_preference,
    learned_preference_is_actionable,
)
from core.operator_resolution_service import (
    commitment_downstream_effects,
    commitment_is_active,
    commitment_requested_autonomy_level,
    commitment_snapshot,
    latest_active_operator_resolution_commitment,
)


CONFLICT_SOURCE = "objective90"
CONFLICT_DECISION_FAMILY_PROPOSAL = "workspace_proposal_shaping"
CONFLICT_DECISION_FAMILY_STEWARDSHIP = "stewardship_auto_execution"
CONFLICT_DECISION_FAMILY_AUTONOMY = "autonomy_boundary"
CONFLICT_DECISION_FAMILY_INQUIRY = "governed_inquiry_answer_path"
CONFLICT_DECISION_FAMILY_INQUIRY_DECISION = "governed_inquiry_decision_state"
COOLDOWN_WINDOW_MINUTES = 30
OSCILLATION_WINDOW_HOURS = 2
CONTRADICTORY_REOPEN_EFFECTIVE_SCORE_FLOOR = 0.55
CONTRADICTORY_REOPEN_EFFECTIVE_SCORE_MARGIN = 0.05
CONTRADICTORY_REOPEN_ELIGIBLE_SOURCES = {
    "execution_truth_governance",
    "operator_commitment_outcome",
    "proposal_policy_convergence",
    "trigger_evidence",
}
AUTONOMY_LEVELS = [
    "manual_only",
    "operator_required",
    "bounded_auto",
    "strategy_auto",
]


def _canonical_autonomy_level(level: object) -> str:
    normalized = str(level or "").strip() or "operator_required"
    if normalized == "trusted_auto":
        return "strategy_auto"
    return normalized


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _scope_value(raw: object) -> str:
    return str(raw or "").strip() or "global"


def _type_value(raw: object) -> str:
    return str(raw or "").strip()


def _bounded(value: object, *, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except Exception:
        numeric = 0.0
    return max(lo, min(hi, numeric))


def _json_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _json_list(raw: object) -> list:
    return raw if isinstance(raw, list) else []


def _json_safe(raw: object) -> object:
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(raw, dict):
        return {str(key): _json_safe(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return [_json_safe(item) for item in raw]
    if isinstance(raw, tuple):
        return [_json_safe(item) for item in raw]
    return raw


def _cooldown_active(raw: datetime | None) -> bool:
    if raw is None:
        return False
    resolved = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc) > _utcnow()


def _operator_commitment_effects(row: WorkspaceOperatorResolutionCommitment) -> dict:
    decision_type = _type_value(row.decision_type)
    if decision_type == "defer_action":
        return {
            "priority_delta": -0.26,
            "score_cap": 0.34,
            "require_operator_confirmation": True,
            "why_policy_prevailed": "Active operator commitment deferred action for this scope.",
        }
    if decision_type == "require_additional_evidence":
        return {
            "priority_delta": -0.22,
            "score_cap": 0.42,
            "require_operator_confirmation": True,
            "why_policy_prevailed": "Active operator commitment requires additional evidence before action.",
        }
    if decision_type == "lower_autonomy_for_scope":
        return {
            "priority_delta": -0.18,
            "score_cap": 0.5,
            "require_operator_confirmation": True,
            "why_policy_prevailed": "Active operator commitment lowered autonomy for this scope.",
        }
    return {
        "priority_delta": 0.0,
        "score_cap": None,
        "require_operator_confirmation": False,
        "why_policy_prevailed": "",
    }


def _commitment_posture(row: WorkspaceOperatorResolutionCommitment) -> str:
    decision_type = _type_value(row.decision_type)
    if decision_type in {
        "defer_action",
        "require_additional_evidence",
        "lower_autonomy_for_scope",
    }:
        return "caution"
    if decision_type in {"approve_current_path", "increase_autonomy_for_scope"}:
        return "promote"
    return "advisory"


def _governance_snapshot(row: WorkspaceExecutionTruthGovernanceProfile) -> dict:
    summary = _json_dict(row.execution_truth_summary_json)
    freshness = execution_truth_freshness(summary)
    return {
        "governance_id": int(row.id),
        "managed_scope": _scope_value(row.managed_scope),
        "status": _type_value(row.status),
        "confidence": round(float(row.confidence or 0.0), 6),
        "governance_state": _type_value(row.governance_state),
        "governance_decision": _type_value(row.governance_decision),
        "governance_reason": str(row.governance_reason or "").strip(),
        "downstream_actions": _json_dict(row.downstream_actions_json),
        "execution_truth_summary": summary,
        "freshness": freshness,
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
    }


def _governance_posture(row: WorkspaceExecutionTruthGovernanceProfile) -> str:
    decision = _type_value(row.governance_decision)
    if decision in {
        "escalate_to_operator",
        "lower_autonomy_boundary",
        "request_operator_review",
        "defer_action",
    }:
        return "caution"
    return "advisory"


def _governance_effects(row: WorkspaceExecutionTruthGovernanceProfile) -> dict:
    decision = _type_value(row.governance_decision)
    if decision in {"escalate_to_operator", "request_operator_review"}:
        return {
            "priority_delta": -0.18,
            "score_cap": 0.52,
            "require_operator_confirmation": True,
            "why_policy_prevailed": "Recent execution-truth governance requested operator review for this scope.",
        }
    if decision == "lower_autonomy_boundary":
        return {
            "priority_delta": -0.14,
            "score_cap": 0.58,
            "require_operator_confirmation": True,
            "why_policy_prevailed": "Recent execution-truth governance lowered the autonomy boundary for this scope.",
        }
    return {
        "priority_delta": 0.0,
        "score_cap": None,
        "require_operator_confirmation": False,
        "why_policy_prevailed": "",
    }


def _governance_freshness_weight(snapshot: dict) -> float:
    freshness = _json_dict(snapshot.get("freshness", {}))
    if freshness:
        return float(freshness.get("freshness_weight", 0.0) or 0.0)
    summary = _json_dict(snapshot.get("execution_truth_summary", {}))
    derived = execution_truth_freshness(summary)
    return float(_json_dict(derived).get("freshness_weight", 0.0) or 0.0)


def _proposal_posture(proposal_policy: dict) -> str:
    state = _type_value(proposal_policy.get("policy_state"))
    if state == "preferred":
        return "promote"
    if state in {"suppressed", "downgraded"}:
        return "caution"
    return "advisory"


def _learned_preference_posture(preference: dict) -> str:
    direction = _type_value(preference.get("preference_direction"))
    if direction == "reinforce":
        return "promote"
    if direction == "avoid":
        return "caution"
    return "advisory"


def _candidate_sort_key(candidate: dict) -> tuple[float, float, float]:
    return (
        float(candidate.get("precedence_rank", 0.0) or 0.0),
        float(candidate.get("effective_score", 0.0) or 0.0),
        float(candidate.get("confidence", 0.0) or 0.0),
    )


def _candidate_by_source(candidates: list[dict], source: str) -> dict:
    normalized = str(source or "").strip()
    for candidate in candidates:
        if str(candidate.get("policy_source") or "").strip() == normalized:
            return candidate
    return {}


def _contradictory_fresh_evidence_reopens_cooldown(
    *,
    previous_winner: str,
    new_winner: dict,
    candidates: list[dict],
) -> bool:
    previous_source = _type_value(previous_winner)
    if previous_source == "operator_commitment":
        return False

    winning_source = _type_value(new_winner.get("policy_source"))
    if not winning_source or winning_source not in CONTRADICTORY_REOPEN_ELIGIBLE_SOURCES:
        return False

    freshness_weight = float(new_winner.get("freshness_weight", 0.0) or 0.0)
    effective_score = float(new_winner.get("effective_score", 0.0) or 0.0)
    if freshness_weight < 0.6:
        return False
    if effective_score < CONTRADICTORY_REOPEN_EFFECTIVE_SCORE_FLOOR:
        return False

    previous_candidate = _candidate_by_source(candidates, previous_source)
    previous_score = float(previous_candidate.get("effective_score", 0.0) or 0.0)
    previous_precedence = float(previous_candidate.get("precedence_rank", 0.0) or 0.0)
    winning_precedence = float(new_winner.get("precedence_rank", 0.0) or 0.0)
    winning_snapshot = _json_dict(new_winner.get("snapshot", {}))
    current_signal_strength = float(winning_snapshot.get("signal_strength", 0.0) or 0.0)
    previous_signal_strength = float(winning_snapshot.get("previous_signal_strength", 0.0) or 0.0)
    if previous_candidate and winning_precedence > previous_precedence:
        return True
    if (
        winning_source == "trigger_evidence"
        and current_signal_strength > previous_signal_strength
        and effective_score >= max(CONTRADICTORY_REOPEN_EFFECTIVE_SCORE_FLOOR, previous_score)
    ):
        return True
    if previous_candidate and effective_score < (previous_score + CONTRADICTORY_REOPEN_EFFECTIVE_SCORE_MARGIN):
        return False

    return True


def _candidate_payload(
    *,
    source: str,
    posture: str,
    precedence_rank: float,
    confidence: float,
    freshness_weight: float,
    rationale: str,
    snapshot: dict,
    policy_effects_json: dict,
) -> dict:
    bounded_confidence = _bounded(confidence)
    bounded_freshness = _bounded(freshness_weight)
    return {
        "policy_source": source,
        "posture": posture,
        "precedence_rank": round(float(precedence_rank), 6),
        "confidence": round(bounded_confidence, 6),
        "freshness_weight": round(bounded_freshness, 6),
        "effective_score": round(bounded_confidence * bounded_freshness, 6),
        "rationale": str(rationale or "").strip(),
        "policy_effects_json": _json_dict(_json_safe(policy_effects_json)),
        "snapshot": _json_dict(_json_safe(snapshot)),
    }


def _precedence_rule(*, winner: str, loser_sources: list[str]) -> str:
    losers = set(loser_sources)
    if winner == "operator_commitment" and "proposal_policy_convergence" in losers:
        return "operator_commitment_over_proposal_policy"
    if winner == "operator_commitment" and "proposal_arbitration_review" in losers:
        return "operator_commitment_over_proposal_arbitration_review"
    if winner == "operator_commitment" and "execution_truth_governance" in losers:
        return "operator_commitment_over_execution_truth_governance"
    if winner == "operator_commitment" and "execution_readiness" in losers:
        return "operator_commitment_over_execution_readiness"
    if winner == "operator_commitment" and "learned_preference" in losers:
        return "operator_commitment_over_learned_preference"
    if winner == "execution_readiness" and "execution_truth_governance" in losers:
        return "execution_readiness_over_execution_truth_governance"
    if winner == "execution_readiness" and "proposal_policy_convergence" in losers:
        return "execution_readiness_over_proposal_policy"
    if winner == "execution_readiness" and "learned_preference" in losers:
        return "execution_readiness_over_learned_preference"
    if winner == "execution_truth_governance" and "proposal_policy_convergence" in losers:
        return "recent_execution_truth_over_proposal_policy"
    if winner == "execution_truth_governance" and "proposal_arbitration_review" in losers:
        return "recent_execution_truth_over_proposal_arbitration_review"
    if winner == "execution_truth_governance" and "learned_preference" in losers:
        return "recent_execution_truth_over_learned_preference"
    if winner == "proposal_policy_convergence" and "learned_preference" in losers:
        return "proposal_policy_over_learned_preference"
    return "higher_precedence_policy_won"


def _profile_payload(row: WorkspacePolicyConflictProfile) -> dict:
    cooldown_until = getattr(row, "cooldown_until", None)
    return {
        "profile_id": int(row.id),
        "managed_scope": _scope_value(row.managed_scope),
        "decision_family": _type_value(row.decision_family),
        "proposal_type": _type_value(row.proposal_type),
        "conflict_state": _type_value(row.conflict_state),
        "winning_policy_source": _type_value(row.winning_policy_source),
        "losing_policy_sources": [str(item).strip() for item in _json_list(row.losing_policy_sources_json) if str(item).strip()],
        "precedence_rule": _type_value(row.precedence_rule),
        "conflict_confidence": round(float(row.conflict_confidence or 0.0), 6),
        "oscillation_count": int(row.oscillation_count or 0),
        "cooldown_until": cooldown_until.isoformat() if cooldown_until is not None else None,
        "cooldown_active": _cooldown_active(cooldown_until),
        "resolution_reason_json": _json_dict(row.resolution_reason_json),
        "evidence_summary_json": _json_dict(row.evidence_summary_json),
        "candidate_policies_json": _json_list(row.candidate_policies_json),
        "policy_effects_json": _json_dict(row.policy_effects_json),
        "metadata_json": _json_dict(row.metadata_json),
        "updated_at": getattr(row, "updated_at", None),
    }


def _selected_policy_effects(
    *,
    winner: dict,
    conflict_state: str,
    winning_policy_source: str,
    effect_mode: str,
    effect_sources: set[str] | None,
) -> dict:
    if effect_sources is not None and winning_policy_source not in effect_sources:
        return {}
    effects = _json_dict(winner.get("policy_effects_json", {}))
    if not effects:
        return {}
    if effect_mode == "winner_always":
        return effects
    if effect_mode == "active_conflict_only" and conflict_state in {"active_conflict", "cooldown_held"}:
        return effects
    return {}


async def _resolve_policy_conflict_profile(
    *,
    db: AsyncSession,
    managed_scope: str,
    decision_family: str,
    proposal_type: str,
    actor: str,
    proposal_id: int | None,
    candidates: list[dict],
    metadata_json: dict,
    effect_mode: str,
    effect_sources: set[str] | None = None,
) -> dict:
    scope = _scope_value(managed_scope)
    normalized_type = _type_value(proposal_type)
    now = _utcnow()

    meaningful = [
        candidate
        for candidate in candidates
        if str(candidate.get("posture") or "") in {"promote", "caution"}
        and float(candidate.get("effective_score", 0.0) or 0.0) > 0.0
    ]
    meaningful.sort(key=_candidate_sort_key, reverse=True)

    promoting = [candidate for candidate in meaningful if candidate.get("posture") == "promote"]
    cautioning = [candidate for candidate in meaningful if candidate.get("posture") == "caution"]
    losing_policy_sources = [
        str(candidate.get("policy_source") or "").strip()
        for candidate in meaningful[1:]
        if str(candidate.get("policy_source") or "").strip()
    ]
    winner = meaningful[0] if meaningful else {}
    winning_policy_source = _type_value(winner.get("policy_source"))
    conflict_state = "advisory"
    if promoting and cautioning:
        conflict_state = "active_conflict"
    elif len(meaningful) > 1:
        conflict_state = "aligned"
    elif meaningful:
        conflict_state = "single_source"

    precedence_rule = (
        _precedence_rule(
            winner=winning_policy_source,
            loser_sources=losing_policy_sources,
        )
        if winning_policy_source
        else ""
    )
    applied_effects = _selected_policy_effects(
        winner=winner,
        conflict_state=conflict_state,
        winning_policy_source=winning_policy_source,
        effect_mode=effect_mode,
        effect_sources=effect_sources,
    )

    existing = (
        (
            await db.execute(
                select(WorkspacePolicyConflictProfile)
                .where(WorkspacePolicyConflictProfile.managed_scope == scope)
                .where(WorkspacePolicyConflictProfile.decision_family == decision_family)
                .where(WorkspacePolicyConflictProfile.proposal_type == normalized_type)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    oscillation_count = int(getattr(existing, "oscillation_count", 0) or 0)
    cooldown_until = getattr(existing, "cooldown_until", None)
    previous_winner = _type_value(getattr(existing, "winning_policy_source", ""))
    previous_effects = _json_dict(getattr(existing, "policy_effects_json", {})) if existing is not None else {}
    previous_reason = _json_dict(getattr(existing, "resolution_reason_json", {})) if existing is not None else {}
    previous_updated = getattr(existing, "updated_at", None) or getattr(existing, "created_at", None)
    if previous_updated is not None and previous_updated.tzinfo is None:
        previous_updated = previous_updated.replace(tzinfo=timezone.utc)
    if previous_winner and winning_policy_source and previous_winner != winning_policy_source:
        if previous_updated is not None and now - previous_updated <= timedelta(hours=OSCILLATION_WINDOW_HOURS):
            oscillation_count += 1
        else:
            oscillation_count = 1
    elif previous_winner and previous_winner == winning_policy_source:
        oscillation_count = int(getattr(existing, "oscillation_count", 0) or 0)

    reopened_by_contradictory_fresh_evidence = False
    if (
        existing is not None
        and previous_winner
        and winning_policy_source
        and previous_winner != winning_policy_source
        and _cooldown_active(cooldown_until)
    ):
        if _contradictory_fresh_evidence_reopens_cooldown(
            previous_winner=previous_winner,
            new_winner=winner,
            candidates=meaningful,
        ):
            reopened_by_contradictory_fresh_evidence = True
            precedence_rule = "contradictory_fresh_evidence_reopened"
            cooldown_until = None
        else:
            winning_policy_source = previous_winner
            losing_policy_sources = [
                source
                for source in [
                    str(winner.get("policy_source") or "").strip(),
                    *losing_policy_sources,
                ]
                if source and source != previous_winner
            ]
            precedence_rule = "cooldown_hold_down"
            conflict_state = "cooldown_held"
            applied_effects = previous_effects or applied_effects
            winner = {
                **winner,
                "policy_source": previous_winner,
                "policy_effects_json": applied_effects,
            }

    if conflict_state == "active_conflict" and oscillation_count >= 2 and winning_policy_source != "operator_commitment":
        cooldown_until = now + timedelta(minutes=COOLDOWN_WINDOW_MINUTES)
    elif not _cooldown_active(cooldown_until):
        cooldown_until = None

    resolution_reason_json = {
        "summary": str(winner.get("rationale") or previous_reason.get("summary") or "").strip(),
        "why_policy_a_overrode_policy_b": str(applied_effects.get("why_policy_prevailed") or "").strip(),
        "winner_posture": str(winner.get("posture") or "").strip(),
        "losing_policy_sources": losing_policy_sources,
        "reopened_by_contradictory_fresh_evidence": reopened_by_contradictory_fresh_evidence,
    }
    evidence_summary_json = {
        "candidate_count": len(candidates),
        "meaningful_candidate_count": len(meaningful),
        "promoting_sources": [str(item.get("policy_source") or "").strip() for item in promoting],
        "cautioning_sources": [str(item.get("policy_source") or "").strip() for item in cautioning],
        "contradictory_recent_signal": reopened_by_contradictory_fresh_evidence,
    }

    if existing is None:
        existing = WorkspacePolicyConflictProfile(
            source=CONFLICT_SOURCE,
            actor=actor,
            managed_scope=scope,
            decision_family=decision_family,
            proposal_type=normalized_type,
        )
        db.add(existing)

    existing.source = CONFLICT_SOURCE
    existing.actor = actor
    existing.managed_scope = scope
    existing.decision_family = decision_family
    existing.proposal_type = normalized_type
    existing.conflict_state = conflict_state
    existing.winning_policy_source = winning_policy_source
    existing.losing_policy_sources_json = losing_policy_sources
    existing.precedence_rule = precedence_rule
    existing.conflict_confidence = float(winner.get("effective_score", 0.0) or 0.0)
    existing.oscillation_count = oscillation_count
    existing.cooldown_until = cooldown_until
    existing.resolution_reason_json = resolution_reason_json
    existing.evidence_summary_json = evidence_summary_json
    existing.candidate_policies_json = candidates
    existing.policy_effects_json = applied_effects
    existing.metadata_json = _json_dict(_json_safe(metadata_json))
    await db.flush()

    event = WorkspacePolicyConflictResolutionEvent(
        source=CONFLICT_SOURCE,
        actor=actor,
        profile_id=int(existing.id),
        proposal_id=proposal_id,
        managed_scope=scope,
        decision_family=decision_family,
        proposal_type=normalized_type,
        event_type=(
            "reopened"
            if reopened_by_contradictory_fresh_evidence
            else ("cooldown_held" if conflict_state == "cooldown_held" else "resolved")
        ),
        winning_policy_source=winning_policy_source,
        losing_policy_sources_json=losing_policy_sources,
        precedence_rule=precedence_rule,
        conflict_state=conflict_state,
        conflict_confidence=float(winner.get("effective_score", 0.0) or 0.0),
        oscillation_count=oscillation_count,
        resolution_reason_json=resolution_reason_json,
        evidence_summary_json=evidence_summary_json,
        candidate_policies_json=candidates,
        policy_effects_json=applied_effects,
        metadata_json=_json_dict(_json_safe(metadata_json)),
    )
    db.add(event)
    await db.flush()
    return _profile_payload(existing)


async def _latest_governance(
    *, managed_scope: str, db: AsyncSession
) -> WorkspaceExecutionTruthGovernanceProfile | None:
    scope = _scope_value(managed_scope)
    rows = (
        (
            await db.execute(
                select(WorkspaceExecutionTruthGovernanceProfile)
                .where(WorkspaceExecutionTruthGovernanceProfile.managed_scope == scope)
                .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if rows:
        return rows[0]
    if scope == "global":
        return None
    return (
        (
            await db.execute(
                select(WorkspaceExecutionTruthGovernanceProfile)
                .where(WorkspaceExecutionTruthGovernanceProfile.managed_scope == "global")
                .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _empty_effects() -> dict:
    return {
        "priority_delta": 0.0,
        "score_cap": None,
        "require_operator_confirmation": False,
        "why_policy_prevailed": "",
    }


def _inquiry_path_id(path: dict) -> str:
    return str(path.get("path_id") or "").strip()


def _inquiry_effect_type(path: dict) -> str:
    return str(path.get("effect_type") or "").strip()


def _inquiry_decision_reason(*, trigger_type: str, evidence_score: float) -> str:
    if evidence_score >= 0.7:
        if _type_value(trigger_type) == "stewardship_persistent_degradation":
            return "persistent_degradation_with_actionable_uncertainty"
        return "high_evidence_uncertainty_blocks_progress"
    return "moderate_evidence_uncertainty"


def _inquiry_cooldown_confidence(*, prior_evidence_score: float, remaining_ratio: float) -> float:
    bounded_ratio = _bounded(remaining_ratio)
    baseline = max(0.52, min(0.82, prior_evidence_score + 0.05))
    return _bounded(baseline * max(0.8, bounded_ratio))


def _inquiry_trigger_signal_strength(*, trigger_type: str, trigger_evidence: dict) -> float:
    evidence = _json_dict(trigger_evidence)
    normalized_trigger = _type_value(trigger_type)
    if not evidence:
        return 0.0
    if normalized_trigger == "stewardship_persistent_degradation":
        return (
            float(evidence.get("degraded_signal_count", 0) or 0)
            + (float(evidence.get("execution_truth_signal_count", 0) or 0) * 0.35)
            + (len(_json_list(evidence.get("inquiry_candidates", []))) * 0.4)
            + (len(_json_list(evidence.get("missing_key_objects", []))) * 0.4)
        )
    if normalized_trigger == "target_confidence_too_low":
        return float(evidence.get("warning_count", 0) or 0)
    if normalized_trigger == "execution_truth_runtime_mismatch":
        return float(evidence.get("signal_count", 0) or 0)
    numeric_total = 0.0
    for value in evidence.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric_total += float(value)
    return numeric_total


def _inquiry_trigger_freshness_weight(
    *,
    trigger_type: str,
    trigger_evidence: dict,
    previous_trigger_evidence: dict,
) -> float:
    current_strength = _inquiry_trigger_signal_strength(
        trigger_type=trigger_type,
        trigger_evidence=trigger_evidence,
    )
    previous_strength = _inquiry_trigger_signal_strength(
        trigger_type=trigger_type,
        trigger_evidence=previous_trigger_evidence,
    )
    if current_strength <= 0.0:
        return 0.8
    if current_strength > previous_strength:
        return 1.0
    if current_strength == previous_strength and current_strength > 0.0:
        return 0.8
    return 0.72


def _inquiry_safe_effect_types_for_commitment(decision_type: str) -> tuple[list[str], list[str]]:
    normalized = _type_value(decision_type)
    if normalized in {"require_additional_evidence", "defer_action"}:
        return (
            ["trigger_rescan", "update_commitment_status", "no_action"],
            [
                "create_proposal",
                "update_stewardship_target",
                "change_autonomy",
                "record_commitment_learning_bias",
                "shift_strategy",
                "shift_strategy_and_unblock",
                "unblock_plan",
            ],
        )
    if normalized == "lower_autonomy_for_scope":
        return (
            ["trigger_rescan", "update_commitment_status", "no_action"],
            ["change_autonomy"],
        )
    return ([], [])


async def resolve_inquiry_decision_policy_conflict(
    *,
    managed_scope: str,
    trigger_type: str,
    evidence_score: float,
    cooldown_seconds: int,
    cooldown_remaining_seconds: int,
    latest_answered: object | None,
    latest_answered_policy: dict,
    trigger_evidence: dict,
    suppression_commitment: WorkspaceOperatorResolutionCommitment | None,
    autonomy_profile: object | None,
    db: AsyncSession,
) -> dict:
    scope = _scope_value(managed_scope)
    normalized_trigger_type = _type_value(trigger_type)
    bounded_evidence = _bounded(evidence_score)
    candidates: list[dict] = []
    suppressive_sources_present = False
    existing_profile = (
        (
            await db.execute(
                select(WorkspacePolicyConflictProfile)
                .where(WorkspacePolicyConflictProfile.managed_scope == scope)
                .where(
                    WorkspacePolicyConflictProfile.decision_family
                    == CONFLICT_DECISION_FAMILY_INQUIRY_DECISION
                )
                .where(WorkspacePolicyConflictProfile.proposal_type == normalized_trigger_type)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    existing_cooldown_active = _cooldown_active(
        getattr(existing_profile, "cooldown_until", None)
    )
    existing_winner = _type_value(
        getattr(existing_profile, "winning_policy_source", "")
    )
    if existing_cooldown_active and existing_winner in {
        "recent_inquiry_cooldown",
        "autonomy_boundary",
        "inquiry_evidence_floor",
    }:
        suppressive_sources_present = True

    if suppression_commitment is not None and commitment_is_active(suppression_commitment):
        suppressive_sources_present = True
        candidates.append(
            _candidate_payload(
                source="operator_commitment",
                posture="caution",
                precedence_rank=100.0,
                confidence=float(suppression_commitment.confidence or 0.0),
                freshness_weight=1.0,
                rationale=str(suppression_commitment.reason or "").strip(),
                snapshot={
                    **commitment_snapshot(suppression_commitment),
                    "trigger_type": normalized_trigger_type,
                },
                policy_effects_json={
                    "decision_state": "deferred_due_to_operator_commitment",
                    "reason": "active_operator_resolution_commitment_requires_more_evidence",
                    "duplicate_suppressed": True,
                    "suppression_reason": "active_operator_resolution_commitment",
                    "operator_resolution_commitment": {
                        "commitment_id": int(suppression_commitment.id),
                        "managed_scope": _scope_value(suppression_commitment.managed_scope),
                        "decision_type": _type_value(suppression_commitment.decision_type),
                        "authority_level": _type_value(suppression_commitment.authority_level),
                        "expires_at": getattr(suppression_commitment, "expires_at", None),
                    },
                    "why_policy_prevailed": "Active operator commitment requires additional evidence before reopening inquiry for this scope.",
                },
            )
        )

    active_cooldown = (
        latest_answered is not None
        and getattr(latest_answered, "answered_at", None) is not None
        and cooldown_seconds > 0
        and cooldown_remaining_seconds > 0
    )
    if active_cooldown:
        suppressive_sources_present = True
        prior_evidence_score = float(latest_answered_policy.get("evidence_score", bounded_evidence) or bounded_evidence)
        remaining_ratio = float(cooldown_remaining_seconds) / float(max(1, cooldown_seconds))
        candidates.append(
            _candidate_payload(
                source="recent_inquiry_cooldown",
                posture="caution",
                precedence_rank=88.0,
                confidence=_inquiry_cooldown_confidence(
                    prior_evidence_score=prior_evidence_score,
                    remaining_ratio=remaining_ratio,
                ),
                freshness_weight=max(0.85, _bounded(remaining_ratio)),
                rationale="A recent answered inquiry for this scope is still inside its cooldown window.",
                snapshot={
                    "trigger_type": normalized_trigger_type,
                    "question_id": int(getattr(latest_answered, "id", 0) or 0),
                    "answered_at": getattr(latest_answered, "answered_at", None),
                    "cooldown_seconds": int(cooldown_seconds),
                    "cooldown_remaining_seconds": int(cooldown_remaining_seconds),
                    "prior_evidence_score": round(prior_evidence_score, 6),
                },
                policy_effects_json={
                    "decision_state": "deferred_due_to_cooldown",
                    "reason": "recent_answer_reused",
                    "cooldown_active": True,
                    "cooldown_remaining_seconds": int(cooldown_remaining_seconds),
                    "duplicate_suppressed": True,
                    "recent_answer_reused": True,
                    "reused_question_id": int(getattr(latest_answered, "id", 0) or 0),
                    "reused_answered_at": getattr(latest_answered, "answered_at", None),
                    "suppression_reason": "recent_answer_still_valid",
                    "why_policy_prevailed": "A recent inquiry answer is still active for this scope, so duplicate inquiry stays in cooldown until stronger contradictory evidence appears.",
                },
            )
        )

    autonomy_level = _type_value(getattr(autonomy_profile, "current_level", ""))
    if (
        autonomy_profile is not None
        and autonomy_level in {"bounded_auto", "full_auto"}
        and normalized_trigger_type
        not in {"operator_commitment_drift_detected", "operator_commitment_learning_review"}
        and bounded_evidence >= 0.45
        and bounded_evidence < 0.7
    ):
        suppressive_sources_present = True
        candidates.append(
            _candidate_payload(
                source="autonomy_boundary",
                posture="caution",
                precedence_rank=65.0,
                confidence=max(0.55, float(getattr(autonomy_profile, "confidence", 0.0) or 0.0)),
                freshness_weight=1.0,
                rationale=str(getattr(autonomy_profile, "adjustment_reason", "") or "").strip()
                or "Current autonomy boundary is confident enough to avoid optional operator inquiry for this scope.",
                snapshot={
                    "scope": _scope_value(getattr(autonomy_profile, "scope", scope)),
                    "current_level": autonomy_level,
                    "confidence": float(getattr(autonomy_profile, "confidence", 0.0) or 0.0),
                },
                policy_effects_json={
                    "decision_state": "suppressed_high_confidence_autonomy",
                    "reason": "autonomy_confident_enough_without_operator_inquiry",
                    "suppression_reason": "high_confidence_autonomy",
                    "autonomy_level": autonomy_level,
                    "why_policy_prevailed": "High-confidence autonomy kept this inquiry suppressed because the evidence only supported optional refinement.",
                },
            )
        )

    if bounded_evidence < 0.45:
        candidates.append(
            _candidate_payload(
                source="inquiry_evidence_floor",
                posture="caution",
                precedence_rank=60.0,
                confidence=max(0.55, 0.7 - bounded_evidence),
                freshness_weight=1.0,
                rationale="Inquiry trigger evidence stayed below the minimum threshold for operator escalation.",
                snapshot={
                    "trigger_type": normalized_trigger_type,
                    "evidence_score": round(bounded_evidence, 6),
                },
                policy_effects_json={
                    "decision_state": "suppressed_low_evidence",
                    "reason": "evidence_below_minimum_threshold",
                    "suppression_reason": "low_evidence",
                    "why_policy_prevailed": "Inquiry evidence remained below the minimum threshold, so the inquiry stayed suppressed.",
                },
            )
        )
    elif suppressive_sources_present:
        previous_trigger_evidence = (
            _json_dict(getattr(latest_answered, "trigger_evidence_json", {}))
            if latest_answered is not None
            else {}
        )
        trigger_freshness_weight = _inquiry_trigger_freshness_weight(
            trigger_type=normalized_trigger_type,
            trigger_evidence=_json_dict(trigger_evidence),
            previous_trigger_evidence=previous_trigger_evidence,
        )
        candidates.append(
            _candidate_payload(
                source="trigger_evidence",
                posture="promote",
                precedence_rank=45.0 if bounded_evidence >= 0.7 else 35.0,
                confidence=bounded_evidence,
                freshness_weight=trigger_freshness_weight,
                rationale="Fresh inquiry trigger evidence is strong enough to justify reopening or advancing inquiry for this scope.",
                snapshot={
                    "trigger_type": normalized_trigger_type,
                    "evidence_score": round(bounded_evidence, 6),
                    "signal_strength": round(
                        _inquiry_trigger_signal_strength(
                            trigger_type=normalized_trigger_type,
                            trigger_evidence=_json_dict(trigger_evidence),
                        ),
                        6,
                    ),
                    "previous_signal_strength": round(
                        _inquiry_trigger_signal_strength(
                            trigger_type=normalized_trigger_type,
                            trigger_evidence=previous_trigger_evidence,
                        ),
                        6,
                    ),
                },
                policy_effects_json={
                    "decision_state": (
                        "required_for_progress"
                        if bounded_evidence >= 0.7
                        else "optional_for_refinement"
                    ),
                    "reason": _inquiry_decision_reason(
                        trigger_type=normalized_trigger_type,
                        evidence_score=bounded_evidence,
                    ),
                    "cooldown_active": False,
                    "cooldown_remaining_seconds": 0,
                    "duplicate_suppressed": False,
                    "recent_answer_reused": False,
                    "reused_question_id": None,
                    "reused_answered_at": None,
                    "suppression_reason": "",
                    "why_policy_prevailed": "Fresh inquiry evidence overrode the previous suppression state for this scope.",
                },
            )
        )

    if not candidates:
        return {}

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_INQUIRY_DECISION,
        proposal_type=normalized_trigger_type,
        actor="inquiry",
        proposal_id=None,
        candidates=candidates,
        metadata_json={
            "trigger_type": normalized_trigger_type,
            "evidence_score": round(bounded_evidence, 6),
            "cooldown_seconds": int(max(0, cooldown_seconds)),
            "cooldown_remaining_seconds": int(max(0, cooldown_remaining_seconds)),
            "autonomy_level": autonomy_level,
        },
        effect_mode="winner_always",
    )


async def resolve_inquiry_answer_path_policy_conflict(
    *,
    managed_scope: str,
    trigger_type: str,
    candidate_paths: list[dict],
    db: AsyncSession,
) -> dict:
    scope = _scope_value(managed_scope)
    if not scope or not isinstance(candidate_paths, list) or not candidate_paths:
        return {}

    candidates: list[dict] = []

    operator_resolution_commitment = await latest_active_operator_resolution_commitment(
        scope=scope,
        db=db,
        limit=20,
    )
    if (
        operator_resolution_commitment is not None
        and commitment_is_active(operator_resolution_commitment)
    ):
        preferred_effect_types, masked_effect_types = _inquiry_safe_effect_types_for_commitment(
            _type_value(operator_resolution_commitment.decision_type)
        )
        if preferred_effect_types or masked_effect_types:
            preferred_path_ids = [
                _inquiry_path_id(path)
                for path in candidate_paths
                if _inquiry_effect_type(path) in set(preferred_effect_types)
            ]
            masked_path_ids = [
                _inquiry_path_id(path)
                for path in candidate_paths
                if _inquiry_effect_type(path) in set(masked_effect_types)
            ]
            candidates.append(
                _candidate_payload(
                    source="operator_commitment",
                    posture="caution",
                    precedence_rank=100.0,
                    confidence=float(operator_resolution_commitment.confidence or 0.0),
                    freshness_weight=1.0,
                    rationale=str(operator_resolution_commitment.reason or "").strip(),
                    snapshot={
                        **commitment_snapshot(operator_resolution_commitment),
                        "trigger_type": _type_value(trigger_type),
                    },
                    policy_effects_json={
                        "preferred_path_ids": preferred_path_ids,
                        "preferred_effect_types": preferred_effect_types,
                        "masked_path_ids": masked_path_ids,
                        "disallowed_effect_types": masked_effect_types,
                        "why_policy_prevailed": "Active operator commitment requires safer inquiry answer paths for this scope.",
                    },
                )
            )

    promoted_paths = [
        path
        for path in candidate_paths
        if bool(
            (path.get("proposal_arbitration_learning", {}) if isinstance(path.get("proposal_arbitration_learning", {}), dict) else {}).get("applied", False)
        )
        and float(path.get("proposal_arbitration_weight", 0.0) or 0.0) > 0.0
    ]
    if promoted_paths:
        preferred_path_ids = [_inquiry_path_id(path) for path in promoted_paths if _inquiry_path_id(path)]
        lead_path = promoted_paths[0]
        lead_learning = (
            lead_path.get("proposal_arbitration_learning", {})
            if isinstance(lead_path.get("proposal_arbitration_learning", {}), dict)
            else {}
        )
        learning_confidence = min(
            0.9,
            0.55
            + max(0.0, float(lead_path.get("proposal_arbitration_weight", 0.0) or 0.0))
            + min(0.15, int(lead_learning.get("sample_count", 0) or 0) * 0.02),
        )
        candidates.append(
            _candidate_payload(
                source="proposal_arbitration_review",
                posture="promote",
                precedence_rank=35.0,
                confidence=learning_confidence,
                freshness_weight=1.0,
                rationale="Recent proposal arbitration learning preferred a higher-leverage inquiry path for this scope.",
                snapshot={
                    "trigger_type": _type_value(trigger_type),
                    "related_zone": scope,
                    "preferred_path_ids": preferred_path_ids,
                    "proposal_types": lead_learning.get("proposal_types", []),
                    "sample_count": int(lead_learning.get("sample_count", 0) or 0),
                },
                policy_effects_json={
                    "preferred_path_ids": preferred_path_ids,
                    "preferred_effect_types": [
                        _inquiry_effect_type(lead_path)
                    ]
                    if _inquiry_effect_type(lead_path)
                    else [],
                    "why_policy_prevailed": "Recent proposal arbitration learning preferred this inquiry path family for this scope.",
                },
            )
        )

    if not candidates:
        return {}

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_INQUIRY,
        proposal_type=_type_value(trigger_type),
        actor="inquiry",
        proposal_id=None,
        candidates=candidates,
        metadata_json={
            "trigger_type": _type_value(trigger_type),
            "candidate_path_count": len(candidate_paths),
        },
        effect_mode="winner_always",
    )


async def resolve_workspace_proposal_policy_conflict(
    *,
    proposal: WorkspaceProposal | None = None,
    proposal_type: str,
    related_zone: str,
    proposal_policy_convergence: dict | None,
    db: AsyncSession,
) -> dict:
    scope = _scope_value(related_zone)
    normalized_type = _type_value(proposal_type)
    proposal_policy = proposal_policy_convergence if isinstance(proposal_policy_convergence, dict) else {}
    candidates: list[dict] = []
    readiness = load_latest_execution_readiness(
        action=normalized_type or "workspace_proposal",
        capability_name=normalized_type,
        managed_scope=scope,
        requested_executor="tod",
        metadata_json={
            "proposal_id": int(proposal.id) if proposal is not None else None,
            "proposal_type": normalized_type,
            "managed_scope": scope,
        },
    )
    candidates.append(
        _candidate_payload(
            source="execution_readiness",
            posture=execution_readiness_posture(readiness),
            precedence_rank=execution_readiness_precedence(
                readiness,
                blocking_rank=90.0,
                advisory_rank=55.0,
                ready_rank=35.0,
            ),
            confidence=execution_readiness_confidence(readiness),
            freshness_weight=1.0,
            rationale=str(readiness.get("detail") or "execution readiness is shaping proposal posture").strip(),
            snapshot=readiness,
            policy_effects_json=execution_readiness_policy_effects(
                readiness=readiness,
                surface="proposal",
            ),
        )
    )

    commitment = await latest_active_operator_resolution_commitment(
        scope=scope,
        db=db,
        limit=20,
    )
    if commitment is not None and commitment_is_active(commitment):
        candidates.append(
            _candidate_payload(
                source="operator_commitment",
                posture=_commitment_posture(commitment),
                precedence_rank=100.0,
                confidence=float(commitment.confidence or 0.0),
                freshness_weight=1.0,
                rationale=str(commitment.reason or "").strip(),
                snapshot=commitment_snapshot(commitment),
                policy_effects_json=_operator_commitment_effects(commitment),
            )
        )

    governance = await _latest_governance(managed_scope=scope, db=db)
    if governance is not None:
        governance_snapshot = _governance_snapshot(governance)
        freshness = _json_dict(governance_snapshot.get("freshness", {}))
        candidates.append(
            _candidate_payload(
                source="execution_truth_governance",
                posture=_governance_posture(governance),
                precedence_rank=80.0,
                confidence=float(governance.confidence or 0.0),
                freshness_weight=float(freshness.get("freshness_weight", 0.0) or 0.0),
                rationale=str(governance.governance_reason or "").strip(),
                snapshot=governance_snapshot,
                policy_effects_json=_governance_effects(governance),
            )
        )

    if proposal_policy:
        evidence_summary = _json_dict(proposal_policy.get("evidence_summary_json", {}))
        candidates.append(
            _candidate_payload(
                source="proposal_policy_convergence",
                posture=_proposal_posture(proposal_policy),
                precedence_rank=40.0,
                confidence=float(proposal_policy.get("convergence_confidence", 0.0) or 0.0),
                freshness_weight=float(evidence_summary.get("freshness_average", 0.0) or 0.0),
                rationale=str(proposal_policy.get("rationale") or "").strip(),
                snapshot=proposal_policy,
                policy_effects_json=_json_dict(proposal_policy.get("policy_effects_json", {})),
            )
        )

    learned_preference = await latest_scope_learned_preference(
        db=db,
        managed_scope=scope,
        operator_commitment=commitment,
    )
    if isinstance(learned_preference, dict):
        candidates.append(
            _candidate_payload(
                source="learned_preference",
                posture=_learned_preference_posture(learned_preference),
                precedence_rank=30.0,
                confidence=float(learned_preference.get("confidence_score", 0.0) or 0.0),
                freshness_weight=1.0,
                rationale=str(
                    _json_dict(learned_preference.get("arbitration_reasoning_json", {})).get("reason") or ""
                ).strip(),
                snapshot=learned_preference,
                policy_effects_json=_empty_effects(),
            )
        )

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_PROPOSAL,
        proposal_type=normalized_type,
        actor="workspace",
        proposal_id=(int(proposal.id) if proposal is not None else None),
        candidates=candidates,
        metadata_json={
            "proposal_id": int(proposal.id) if proposal is not None else None,
            "proposal_policy_applied": bool(proposal_policy.get("applied", False)) if proposal_policy else False,
            "learned_preference_actionable": learned_preference_is_actionable(learned_preference),
            "execution_readiness": readiness,
        },
        effect_mode="active_conflict_only",
        effect_sources={"operator_commitment", "execution_truth_governance", "execution_readiness"},
    )


def _stewardship_boundary_effects(*, autonomy_level: str, boundary_confidence: float) -> dict:
    level = _canonical_autonomy_level(autonomy_level)
    if level in {"manual_only", "operator_required"} and _bounded(boundary_confidence) >= 0.5:
        return {
            "allow_auto_execution": False,
            "last_decision_summary": "defer_to_operator_boundary",
            "why_policy_prevailed": "Current autonomy boundary keeps this scope below unattended stewardship execution.",
        }
    if level == "strategy_auto" and _bounded(boundary_confidence) >= 0.65:
        return {
            "allow_auto_execution": True,
            "last_decision_summary": "respect_autonomy_boundary",
            "why_policy_prevailed": "Current autonomy boundary allows strategy-level automatic stewardship in this scope.",
        }
    return {}


async def resolve_stewardship_policy_conflict(
    *,
    managed_scope: str,
    requested_auto_execution: bool,
    execution_truth_governance: dict,
    operator_resolution_commitment: WorkspaceOperatorResolutionCommitment | None,
    learned_preference: dict | None,
    autonomy_level: str,
    boundary_confidence: float,
    db: AsyncSession,
) -> dict:
    scope = _scope_value(managed_scope)
    candidates: list[dict] = []
    readiness = load_latest_execution_readiness(
        action="stewardship_auto_execution",
        capability_name="stewardship",
        managed_scope=scope,
        requested_executor="tod",
        metadata_json={"managed_scope": scope},
    )
    candidates.append(
        _candidate_payload(
            source="execution_readiness",
            posture=execution_readiness_posture(readiness),
            precedence_rank=execution_readiness_precedence(
                readiness,
                blocking_rank=90.0,
                advisory_rank=55.0,
                ready_rank=35.0,
            ),
            confidence=execution_readiness_confidence(readiness),
            freshness_weight=1.0,
            rationale=str(readiness.get("detail") or "execution readiness is shaping stewardship posture").strip(),
            snapshot=readiness,
            policy_effects_json=execution_readiness_policy_effects(
                readiness=readiness,
                surface="stewardship",
            ),
        )
    )

    governance_decision = _type_value(execution_truth_governance.get("governance_decision"))
    governance_actions = _json_dict(execution_truth_governance.get("downstream_actions", {}))
    governance_blocks_auto = not bool(governance_actions.get("stewardship_auto_execute_allowed", True))
    if governance_decision and governance_decision != "monitor_only":
        candidates.append(
            _candidate_payload(
                source="execution_truth_governance",
                posture="caution" if governance_blocks_auto else "advisory",
                precedence_rank=80.0,
                confidence=float(execution_truth_governance.get("confidence", 0.0) or 0.0),
                freshness_weight=_governance_freshness_weight(execution_truth_governance),
                rationale=str(execution_truth_governance.get("governance_reason") or "").strip(),
                snapshot=execution_truth_governance,
                policy_effects_json={
                    "allow_auto_execution": False if governance_blocks_auto else requested_auto_execution,
                    "last_decision_summary": "defer_to_execution_truth_governance" if governance_blocks_auto else "execution_truth_governance_allows_auto_execution",
                    "why_policy_prevailed": "Recent execution-truth governance requested slower stewardship execution for this scope." if governance_blocks_auto else "",
                },
            )
        )

    if operator_resolution_commitment is not None and commitment_is_active(operator_resolution_commitment):
        effects = commitment_downstream_effects(operator_resolution_commitment)
        blocks_auto = bool(effects.get("stewardship_defer_actions", False)) or str(effects.get("stewardship_mode", "") or "").strip() == "deferred"
        if not blocks_auto and _type_value(operator_resolution_commitment.decision_type) in {"defer_action", "require_additional_evidence"}:
            blocks_auto = True
        candidates.append(
            _candidate_payload(
                source="operator_commitment",
                posture="caution" if blocks_auto else _commitment_posture(operator_resolution_commitment),
                precedence_rank=100.0,
                confidence=float(operator_resolution_commitment.confidence or 0.0),
                freshness_weight=1.0,
                rationale=str(operator_resolution_commitment.reason or "").strip(),
                snapshot=commitment_snapshot(operator_resolution_commitment),
                policy_effects_json={
                    "allow_auto_execution": False if blocks_auto else requested_auto_execution,
                    "last_decision_summary": "defer_to_operator_commitment" if blocks_auto else "operator_commitment_allows_auto_execution",
                    "why_policy_prevailed": "Active operator commitment deferred stewardship action for this scope." if blocks_auto else "",
                },
            )
        )

    boundary_effects = _stewardship_boundary_effects(
        autonomy_level=autonomy_level,
        boundary_confidence=boundary_confidence,
    )
    if boundary_effects:
        candidates.append(
            _candidate_payload(
                source="autonomy_boundary",
                posture="promote" if bool(boundary_effects.get("allow_auto_execution", False)) else "caution",
                precedence_rank=60.0,
                confidence=float(boundary_confidence or 0.0),
                freshness_weight=1.0,
                rationale=str(boundary_effects.get("why_policy_prevailed") or "").strip(),
                snapshot={
                    "current_level": _type_value(autonomy_level),
                    "confidence": round(float(boundary_confidence or 0.0), 6),
                },
                policy_effects_json=boundary_effects,
            )
        )

    if isinstance(learned_preference, dict):
        delta = learned_preference.get("policy_effects_json", {}) if isinstance(learned_preference.get("policy_effects_json", {}), dict) else {}
        candidates.append(
            _candidate_payload(
                source="learned_preference",
                posture=_learned_preference_posture(learned_preference),
                precedence_rank=30.0,
                confidence=float(learned_preference.get("confidence_score", 0.0) or 0.0),
                freshness_weight=1.0,
                rationale=str(_json_dict(learned_preference.get("arbitration_reasoning_json", {})).get("reason") or "").strip(),
                snapshot=learned_preference,
                policy_effects_json={
                    "stewardship_priority_delta": float(delta.get("stewardship_priority_delta", 0.0) or 0.0),
                    "why_policy_prevailed": "Learned operator preference is currently shaping stewardship posture for this scope.",
                },
            )
        )

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_STEWARDSHIP,
        proposal_type="",
        actor="stewardship",
        proposal_id=None,
        candidates=candidates,
        metadata_json={
            "requested_auto_execution": bool(requested_auto_execution),
            "autonomy_level": _type_value(autonomy_level),
            "boundary_confidence": round(float(boundary_confidence or 0.0), 6),
            "learned_preference_actionable": learned_preference_is_actionable(learned_preference),
            "execution_readiness": readiness,
        },
        effect_mode="winner_always",
    )


def _autonomy_target_posture(*, baseline_level: str, requested_level: str) -> str:
    baseline = _type_value(baseline_level)
    requested = _type_value(requested_level)
    if baseline not in AUTONOMY_LEVELS or requested not in AUTONOMY_LEVELS:
        return "advisory"
    if AUTONOMY_LEVELS.index(requested) < AUTONOMY_LEVELS.index(baseline):
        return "caution"
    if AUTONOMY_LEVELS.index(requested) > AUTONOMY_LEVELS.index(baseline):
        return "promote"
    return "advisory"


async def resolve_autonomy_boundary_policy_conflict(
    *,
    managed_scope: str,
    baseline_target_level: str,
    execution_truth_governance: dict,
    operator_resolution_commitment: WorkspaceOperatorResolutionCommitment | None,
    operator_resolution_outcome: object | None,
    learned_preference: dict | None,
    proposal_arbitration_review: dict,
    db: AsyncSession,
) -> dict:
    scope = _scope_value(managed_scope)
    candidates: list[dict] = []
    readiness = load_latest_execution_readiness(
        action="autonomy_boundary",
        capability_name="autonomy_boundary",
        managed_scope=scope,
        requested_executor="tod",
        metadata_json={"managed_scope": scope},
    )
    readiness_effects = execution_readiness_policy_effects(
        readiness=readiness,
        surface="autonomy",
    )
    if _type_value(readiness_effects.get("target_level")) in AUTONOMY_LEVELS:
        candidates.append(
            _candidate_payload(
                source="execution_readiness",
                posture=execution_readiness_posture(readiness),
                precedence_rank=execution_readiness_precedence(
                    readiness,
                    blocking_rank=90.0,
                    advisory_rank=55.0,
                    ready_rank=35.0,
                ),
                confidence=execution_readiness_confidence(readiness),
                freshness_weight=1.0,
                rationale=str(readiness.get("detail") or "execution readiness is shaping autonomy posture").strip(),
                snapshot=readiness,
                policy_effects_json=readiness_effects,
            )
        )

    governance_decision = _type_value(execution_truth_governance.get("governance_decision"))
    governance_actions = _json_dict(execution_truth_governance.get("downstream_actions", {}))
    if governance_decision and governance_decision != "monitor_only":
        governance_target = baseline_target_level
        if governance_decision in {"lower_autonomy_boundary", "require_sandbox_experiment"}:
            try:
                governance_target = AUTONOMY_LEVELS[max(0, AUTONOMY_LEVELS.index(baseline_target_level) - 1)]
            except ValueError:
                governance_target = "operator_required"
        elif governance_decision == "escalate_to_operator":
            governance_target = "operator_required"
        capped_level = _type_value(governance_actions.get("autonomy_level_cap"))
        if capped_level in AUTONOMY_LEVELS and AUTONOMY_LEVELS.index(governance_target) > AUTONOMY_LEVELS.index(capped_level):
            governance_target = capped_level
        governance_snapshot = dict(execution_truth_governance)
        candidates.append(
            _candidate_payload(
                source="execution_truth_governance",
                posture=_autonomy_target_posture(baseline_level=baseline_target_level, requested_level=governance_target),
                precedence_rank=80.0,
                confidence=float(execution_truth_governance.get("confidence", 0.0) or 0.0),
                freshness_weight=_governance_freshness_weight(execution_truth_governance),
                rationale=str(execution_truth_governance.get("governance_reason") or "").strip(),
                snapshot=governance_snapshot,
                policy_effects_json={
                    "target_level": governance_target,
                    "why_policy_prevailed": "Recent execution-truth governance adjusted the autonomy boundary for this scope.",
                },
            )
        )

    if operator_resolution_commitment is not None and commitment_is_active(operator_resolution_commitment):
        requested_level = commitment_requested_autonomy_level(operator_resolution_commitment)
        if requested_level in AUTONOMY_LEVELS:
            candidates.append(
                _candidate_payload(
                    source="operator_commitment",
                    posture=_autonomy_target_posture(baseline_level=baseline_target_level, requested_level=requested_level),
                    precedence_rank=100.0,
                    confidence=float(operator_resolution_commitment.confidence or 0.0),
                    freshness_weight=1.0,
                    rationale=str(operator_resolution_commitment.reason or "").strip(),
                    snapshot=commitment_snapshot(operator_resolution_commitment),
                    policy_effects_json={
                        "target_level": requested_level,
                        "why_policy_prevailed": "Active operator commitment set the autonomy posture for this scope.",
                    },
                )
            )

    if operator_resolution_outcome is not None:
        outcome_status = _type_value(getattr(operator_resolution_outcome, "outcome_status", ""))
        learning = _json_dict(getattr(operator_resolution_outcome, "learning_signals_json", {}))
        outcome_target = ""
        if outcome_status == "harmful":
            outcome_target = "operator_required"
        elif outcome_status in {"ineffective", "abandoned"} and baseline_target_level in AUTONOMY_LEVELS and AUTONOMY_LEVELS.index(baseline_target_level) > AUTONOMY_LEVELS.index("operator_required"):
            outcome_target = "operator_required"
        cap = _type_value(learning.get("autonomy_level_cap"))
        if cap in AUTONOMY_LEVELS:
            if not outcome_target or AUTONOMY_LEVELS.index(outcome_target) > AUTONOMY_LEVELS.index(cap):
                outcome_target = cap
        if outcome_target:
            candidates.append(
                _candidate_payload(
                    source="operator_commitment_outcome",
                    posture=_autonomy_target_posture(baseline_level=baseline_target_level, requested_level=outcome_target),
                    precedence_rank=70.0,
                    confidence=float(getattr(operator_resolution_outcome, "learning_confidence", 0.0) or 0.0),
                    freshness_weight=1.0,
                    rationale=str(getattr(operator_resolution_outcome, "outcome_reason", "") or "").strip(),
                    snapshot={
                        "outcome_id": int(getattr(operator_resolution_outcome, "id", 0) or 0),
                        "outcome_status": outcome_status,
                        "learning_signals": learning,
                    },
                    policy_effects_json={
                        "target_level": outcome_target,
                        "why_policy_prevailed": "Recent commitment outcome learning lowered autonomy for this scope.",
                    },
                )
            )

    if isinstance(learned_preference, dict):
        preferred_level = _type_value(_json_dict(learned_preference.get("policy_effects_json", {})).get("preferred_autonomy_level"))
        if preferred_level in AUTONOMY_LEVELS:
            candidates.append(
                _candidate_payload(
                    source="learned_preference",
                    posture=_autonomy_target_posture(baseline_level=baseline_target_level, requested_level=preferred_level),
                    precedence_rank=30.0,
                    confidence=float(learned_preference.get("confidence_score", 0.0) or 0.0),
                    freshness_weight=1.0,
                    rationale=str(_json_dict(learned_preference.get("arbitration_reasoning_json", {})).get("reason") or "").strip(),
                    snapshot=learned_preference,
                    policy_effects_json={
                        "target_level": preferred_level,
                        "why_policy_prevailed": "Learned operator preference shaped the autonomy posture for this scope.",
                    },
                )
            )

    if isinstance(proposal_arbitration_review, dict) and bool(proposal_arbitration_review.get("applied", False)):
        review_target = _type_value(proposal_arbitration_review.get("target_level_cap"))
        if review_target in AUTONOMY_LEVELS:
            candidates.append(
                _candidate_payload(
                    source="proposal_arbitration_review",
                    posture=_autonomy_target_posture(baseline_level=baseline_target_level, requested_level=review_target),
                    precedence_rank=35.0,
                    confidence=float(proposal_arbitration_review.get("review_weight", 0.0) or 0.0),
                    freshness_weight=1.0,
                    rationale=str(proposal_arbitration_review.get("rationale") or "").strip(),
                    snapshot=proposal_arbitration_review,
                    policy_effects_json={
                        "target_level": review_target,
                        "why_policy_prevailed": "Proposal arbitration learning capped autonomy until this scope stabilizes.",
                    },
                )
            )

    return await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_AUTONOMY,
        proposal_type="",
        actor="autonomy",
        proposal_id=None,
        candidates=candidates,
        metadata_json={
            "baseline_target_level": _type_value(baseline_target_level),
            "learned_preference_actionable": learned_preference_is_actionable(learned_preference),
            "execution_readiness": readiness,
        },
        effect_mode="winner_always",
    )


async def list_workspace_policy_conflict_profiles(
    *,
    db: AsyncSession,
    managed_scope: str = "",
    decision_family: str = "",
    proposal_type: str = "",
    conflict_state: str = "",
    limit: int = 50,
) -> list[dict]:
    stmt = select(WorkspacePolicyConflictProfile)
    if str(managed_scope or "").strip():
        stmt = stmt.where(WorkspacePolicyConflictProfile.managed_scope == _scope_value(managed_scope))
    if _type_value(decision_family):
        stmt = stmt.where(WorkspacePolicyConflictProfile.decision_family == _type_value(decision_family))
    if _type_value(proposal_type):
        stmt = stmt.where(WorkspacePolicyConflictProfile.proposal_type == _type_value(proposal_type))
    if _type_value(conflict_state):
        stmt = stmt.where(WorkspacePolicyConflictProfile.conflict_state == _type_value(conflict_state))
    stmt = stmt.order_by(WorkspacePolicyConflictProfile.id.desc()).limit(max(1, min(int(limit), 200)))
    rows = list((await db.execute(stmt)).scalars().all())
    return [_profile_payload(row) for row in rows]