from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    WorkspaceProposalArbitrationOutcome,
    WorkspaceProposalPolicyPreferenceProfile,
)


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _normalize_scope(value: str) -> str:
    return str(value or "").strip() or "global"


def _normalize_type(value: str) -> str:
    return str(value or "").strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_family(proposal_type: str) -> str:
    normalized = _normalize_type(proposal_type)
    family_map = {
        "confirm_target_ready": "workspace_visibility",
        "rescan_zone": "workspace_visibility",
        "monitor_recheck_workspace": "workspace_visibility",
        "monitor_search_adjacent_zone": "workspace_visibility",
        "verify_moved_object": "workspace_visibility",
        "target_confirmation": "target_resolution",
        "target_reobserve": "target_resolution",
        "execution_candidate": "execution",
    }
    return family_map.get(normalized, normalized or "general")


def _freshness_weight(created_at: datetime | None) -> float:
    if created_at is None:
        return 0.05
    resolved = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (_utcnow() - resolved.astimezone(timezone.utc)).total_seconds() / 3600.0)
    if age_hours <= 48.0:
        return 1.0
    if age_hours <= 168.0:
        return 1.0 - (((age_hours - 48.0) / 120.0) * 0.3)
    if age_hours <= 720.0:
        return 0.7 - (((age_hours - 168.0) / 552.0) * 0.55)
    return 0.05


def _outcome_direction(score: float) -> str:
    if score >= 0.6:
        return "positive"
    if score <= 0.4:
        return "negative"
    return "mixed"


def _policy_effects(*, policy_state: str, convergence_confidence: float) -> dict:
    confidence = _bounded(convergence_confidence)
    if policy_state == "preferred":
        delta = min(0.12, 0.04 + (confidence * 0.08))
        return {
            "priority_delta": round(delta, 6),
            "score_cap": None,
            "suppress_before_arbitration": False,
            "downgrade_before_arbitration": False,
            "why_this_proposal_was_deprioritized_before_emission": "",
            "why_this_proposal_was_preferred_before_emission": "Repeated arbitration wins reinforced this proposal shape for this scope.",
        }
    if policy_state == "suppressed":
        delta = min(0.18, 0.08 + (confidence * 0.12))
        return {
            "priority_delta": round(-delta, 6),
            "score_cap": 0.08,
            "suppress_before_arbitration": True,
            "downgrade_before_arbitration": True,
            "why_this_proposal_was_deprioritized_before_emission": "Repeated arbitration losses converged into a bounded suppression policy for this proposal shape.",
            "why_this_proposal_was_preferred_before_emission": "",
        }
    if policy_state == "downgraded":
        delta = min(0.1, 0.03 + (confidence * 0.07))
        return {
            "priority_delta": round(-delta, 6),
            "score_cap": None,
            "suppress_before_arbitration": False,
            "downgrade_before_arbitration": True,
            "why_this_proposal_was_deprioritized_before_emission": "Recent arbitration evidence is trending against this proposal shape, so it was downgraded before arbitration.",
            "why_this_proposal_was_preferred_before_emission": "",
        }
    return {
        "priority_delta": 0.0,
        "score_cap": None,
        "suppress_before_arbitration": False,
        "downgrade_before_arbitration": False,
        "why_this_proposal_was_deprioritized_before_emission": "",
        "why_this_proposal_was_preferred_before_emission": "",
    }


def _recent_outcome_payload(rows: list[WorkspaceProposalArbitrationOutcome]) -> list[dict]:
    return [
        {
            "outcome_id": int(row.id),
            "proposal_id": int(row.proposal_id) if row.proposal_id is not None else None,
            "arbitration_decision": str(row.arbitration_decision or "").strip(),
            "arbitration_posture": str(row.arbitration_posture or "").strip(),
            "downstream_execution_outcome": str(row.downstream_execution_outcome or "").strip(),
            "outcome_score": round(float(row.outcome_score or 0.0), 6),
            "created_at": row.created_at.isoformat() if row.created_at is not None else None,
        }
        for row in rows[:5]
    ]


def _policy_payload(
    *,
    proposal_type: str,
    managed_scope: str,
    rows: list[WorkspaceProposalArbitrationOutcome],
) -> dict:
    normalized_type = _normalize_type(proposal_type)
    scope = _normalize_scope(managed_scope)
    family = _proposal_family(normalized_type)
    sample_count = len(rows)
    if sample_count <= 0:
        return {
            "profile_id": None,
            "managed_scope": scope,
            "proposal_family": family,
            "proposal_type": normalized_type,
            "policy_state": "advisory",
            "preference_direction": "monitor",
            "convergence_confidence": 0.0,
            "sample_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "merge_count": 0,
            "weighted_success_rate": 0.5,
            "recent_success_rate": 0.5,
            "contradictory_recent_signal": False,
            "stale_signal": False,
            "suppression_threshold_met": False,
            "policy_effects_json": _policy_effects(policy_state="advisory", convergence_confidence=0.0),
            "evidence_summary_json": {"recent_outcomes": [], "weight_total": 0.0},
            "metadata_json": {"objective89_proposal_policy_convergence": True},
            "rationale": "No proposal arbitration history exists yet for this proposal shape.",
            "applied": False,
            "updated_at": None,
        }

    win_count = sum(1 for row in rows if str(row.arbitration_decision or "").strip() == "won")
    loss_count = sum(
        1
        for row in rows
        if str(row.arbitration_decision or "").strip() in {"lost", "suppressed", "superseded"}
    )
    merge_count = sum(1 for row in rows if str(row.arbitration_decision or "").strip() == "merged")
    weighted_scores = []
    freshness_scores = []
    for row in rows:
        weight = _freshness_weight(row.created_at)
        freshness_scores.append(weight)
        weighted_scores.append((float(row.outcome_score or 0.0), weight))
    total_weight = sum(weight for _, weight in weighted_scores)
    weighted_success_rate = (
        sum(score * weight for score, weight in weighted_scores) / total_weight if total_weight > 0 else 0.5
    )
    recent_rows = rows[:3]
    recent_success_rate = (
        sum(float(row.outcome_score or 0.0) for row in recent_rows) / float(len(recent_rows))
        if recent_rows
        else 0.5
    )
    base_direction = _outcome_direction(weighted_success_rate)
    recent_direction = _outcome_direction(recent_success_rate)
    contradictory_recent_signal = (
        base_direction in {"positive", "negative"}
        and recent_direction in {"positive", "negative"}
        and base_direction != recent_direction
    )
    freshness_average = (sum(freshness_scores) / float(len(freshness_scores))) if freshness_scores else 0.0
    convergence_confidence = _bounded(
        (min(float(sample_count) / 6.0, 1.0) * 0.45)
        + (_bounded(abs(weighted_success_rate - 0.5) * 2.0) * 0.35)
        + (_bounded(freshness_average) * 0.2)
    )
    stale_signal = freshness_average < 0.25

    policy_state = "advisory"
    preference_direction = "monitor"
    if sample_count >= 4 and stale_signal:
        policy_state = "stale"
    elif sample_count >= 4 and contradictory_recent_signal:
        policy_state = "reopened"
        preference_direction = "mixed"
    elif sample_count >= 4 and weighted_success_rate <= 0.33 and convergence_confidence >= 0.55:
        policy_state = "suppressed"
        preference_direction = "avoid"
    elif sample_count >= 4 and weighted_success_rate <= 0.45 and convergence_confidence >= 0.45:
        policy_state = "downgraded"
        preference_direction = "avoid"
    elif sample_count >= 4 and weighted_success_rate >= 0.67 and convergence_confidence >= 0.55:
        policy_state = "preferred"
        preference_direction = "reinforce"

    policy_effects_json = _policy_effects(
        policy_state=policy_state,
        convergence_confidence=convergence_confidence,
    )
    suppression_threshold_met = bool(policy_effects_json.get("suppress_before_arbitration", False))
    rationale = (
        f"Proposal policy convergence observed weighted_success_rate={weighted_success_rate:.3f} across {sample_count} arbitration outcomes "
        f"for proposal_type={normalized_type or 'unknown'} in scope={scope}."
    )
    if policy_state == "reopened":
        rationale = f"{rationale} Fresh contradictory evidence reopened the policy, so shaping remains advisory."
    elif policy_state == "stale":
        rationale = f"{rationale} Evidence is stale, so policy shaping remains advisory."
    elif policy_state == "suppressed":
        rationale = f"{rationale} Repeated losses crossed the bounded suppression threshold."
    elif policy_state == "downgraded":
        rationale = f"{rationale} Repeated weaker outcomes triggered a bounded downgrade."
    elif policy_state == "preferred":
        rationale = f"{rationale} Repeated wins reinforced this proposal shape as preferred for the scope."

    return {
        "profile_id": None,
        "managed_scope": scope,
        "proposal_family": family,
        "proposal_type": normalized_type,
        "policy_state": policy_state,
        "preference_direction": preference_direction,
        "convergence_confidence": round(convergence_confidence, 6),
        "sample_count": sample_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "merge_count": merge_count,
        "weighted_success_rate": round(weighted_success_rate, 6),
        "recent_success_rate": round(recent_success_rate, 6),
        "contradictory_recent_signal": contradictory_recent_signal,
        "stale_signal": stale_signal,
        "suppression_threshold_met": suppression_threshold_met,
        "policy_effects_json": policy_effects_json,
        "evidence_summary_json": {
            "recent_outcomes": _recent_outcome_payload(rows),
            "weight_total": round(total_weight, 6),
            "freshness_average": round(freshness_average, 6),
            "base_direction": base_direction,
            "recent_direction": recent_direction,
        },
        "metadata_json": {
            "objective89_proposal_policy_convergence": True,
            "proposal_family": family,
        },
        "rationale": rationale,
        "applied": bool(policy_state in {"preferred", "suppressed", "downgraded"}),
        "updated_at": None,
    }


def _to_profile_payload(row: WorkspaceProposalPolicyPreferenceProfile) -> dict:
    evidence_summary_json = row.evidence_summary_json if isinstance(row.evidence_summary_json, dict) else {}
    policy_effects_json = row.policy_effects_json if isinstance(row.policy_effects_json, dict) else {}
    metadata_json = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return {
        "profile_id": int(row.id),
        "managed_scope": str(row.managed_scope or "").strip(),
        "proposal_family": str(row.proposal_family or "").strip(),
        "proposal_type": str(row.proposal_type or "").strip(),
        "policy_state": str(row.policy_state or "").strip(),
        "preference_direction": str(row.preference_direction or "").strip(),
        "convergence_confidence": round(float(row.convergence_confidence or 0.0), 6),
        "sample_count": int(row.sample_count or 0),
        "win_count": int(row.win_count or 0),
        "loss_count": int(row.loss_count or 0),
        "merge_count": int(row.merge_count or 0),
        "weighted_success_rate": round(float(evidence_summary_json.get("weighted_success_rate", 0.5) or 0.5), 6),
        "recent_success_rate": round(float(evidence_summary_json.get("recent_success_rate", 0.5) or 0.5), 6),
        "contradictory_recent_signal": bool(evidence_summary_json.get("contradictory_recent_signal", False)),
        "stale_signal": bool(evidence_summary_json.get("stale_signal", False)),
        "suppression_threshold_met": bool(row.suppression_threshold_met),
        "policy_effects_json": policy_effects_json,
        "evidence_summary_json": evidence_summary_json,
        "metadata_json": metadata_json,
        "rationale": str(metadata_json.get("rationale", "")).strip(),
        "applied": bool(metadata_json.get("applied", False)),
        "updated_at": getattr(row, "updated_at", None),
    }


async def converge_workspace_proposal_policy_preference(
    *,
    proposal_type: str,
    related_zone: str,
    db: AsyncSession,
) -> dict:
    normalized_type = _normalize_type(proposal_type)
    scope = _normalize_scope(related_zone)
    if not normalized_type:
        return _policy_payload(proposal_type="", managed_scope=scope, rows=[])

    scoped_rows = list(
        (
            await db.execute(
                select(WorkspaceProposalArbitrationOutcome)
                .where(WorkspaceProposalArbitrationOutcome.proposal_type == normalized_type)
                .where(WorkspaceProposalArbitrationOutcome.related_zone == scope)
                .order_by(WorkspaceProposalArbitrationOutcome.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    rows = scoped_rows
    if not rows:
        rows = list(
            (
                await db.execute(
                    select(WorkspaceProposalArbitrationOutcome)
                    .where(WorkspaceProposalArbitrationOutcome.proposal_type == normalized_type)
                    .order_by(WorkspaceProposalArbitrationOutcome.id.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )

    payload = _policy_payload(
        proposal_type=normalized_type,
        managed_scope=scope,
        rows=rows,
    )
    existing = (
        (
            await db.execute(
                select(WorkspaceProposalPolicyPreferenceProfile)
                .where(WorkspaceProposalPolicyPreferenceProfile.managed_scope == scope)
                .where(WorkspaceProposalPolicyPreferenceProfile.proposal_type == normalized_type)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = WorkspaceProposalPolicyPreferenceProfile(
            managed_scope=scope,
            proposal_family=str(payload.get("proposal_family", "general") or "general"),
            proposal_type=normalized_type,
        )
        db.add(existing)

    policy_effects_json = payload.get("policy_effects_json", {}) if isinstance(payload.get("policy_effects_json", {}), dict) else {}
    evidence_summary_json = payload.get("evidence_summary_json", {}) if isinstance(payload.get("evidence_summary_json", {}), dict) else {}
    existing.proposal_family = str(payload.get("proposal_family", "general") or "general")
    existing.policy_state = str(payload.get("policy_state", "advisory") or "advisory")
    existing.preference_direction = str(payload.get("preference_direction", "monitor") or "monitor")
    existing.convergence_confidence = float(payload.get("convergence_confidence", 0.0) or 0.0)
    existing.sample_count = int(payload.get("sample_count", 0) or 0)
    existing.win_count = int(payload.get("win_count", 0) or 0)
    existing.loss_count = int(payload.get("loss_count", 0) or 0)
    existing.merge_count = int(payload.get("merge_count", 0) or 0)
    existing.suppression_threshold_met = bool(payload.get("suppression_threshold_met", False))
    existing.policy_effects_json = policy_effects_json
    existing.evidence_summary_json = {
        **evidence_summary_json,
        "weighted_success_rate": float(payload.get("weighted_success_rate", 0.5) or 0.5),
        "recent_success_rate": float(payload.get("recent_success_rate", 0.5) or 0.5),
        "contradictory_recent_signal": bool(payload.get("contradictory_recent_signal", False)),
        "stale_signal": bool(payload.get("stale_signal", False)),
    }
    existing.metadata_json = {
        **(payload.get("metadata_json", {}) if isinstance(payload.get("metadata_json", {}), dict) else {}),
        "rationale": str(payload.get("rationale", "")).strip(),
        "applied": bool(payload.get("applied", False)),
    }
    await db.flush()
    return _to_profile_payload(existing)


async def list_workspace_proposal_policy_preferences(
    *,
    db: AsyncSession,
    related_zone: str = "",
    proposal_type: str = "",
    limit: int = 50,
) -> list[dict]:
    stmt = select(WorkspaceProposalPolicyPreferenceProfile).order_by(WorkspaceProposalPolicyPreferenceProfile.id.desc())
    if _normalize_scope(related_zone) != "global" or str(related_zone or "").strip():
        stmt = stmt.where(WorkspaceProposalPolicyPreferenceProfile.managed_scope == _normalize_scope(related_zone))
    if _normalize_type(proposal_type):
        stmt = stmt.where(WorkspaceProposalPolicyPreferenceProfile.proposal_type == _normalize_type(proposal_type))
    stmt = stmt.limit(max(1, min(int(limit), 200)))
    rows = list((await db.execute(stmt)).scalars().all())
    return [_to_profile_payload(row) for row in rows]