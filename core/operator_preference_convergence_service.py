from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import UserPreference, WorkspaceOperatorResolutionCommitmentOutcomeProfile
from core.preferences import upsert_user_preference


LEARNED_PREFERENCE_PREFIX = "operator_learned_preference:"
TERMINAL_FAILURE_OUTCOMES = {"ineffective", "harmful", "abandoned"}
TERMINAL_SUCCESS_OUTCOMES = {"satisfied"}
PREFERENCE_DECAY_START_HOURS = 72
PREFERENCE_STALE_AFTER_HOURS = 240
PREFERENCE_EXPIRY_HOURS = 720
MIN_EFFECTIVE_PREFERENCE_STRENGTH = 0.42


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _json_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_hours(value: datetime | None) -> float:
    resolved = _as_utc(value)
    if resolved is None:
        return float(PREFERENCE_EXPIRY_HOURS)
    delta = _utcnow() - resolved
    return max(0.0, delta.total_seconds() / 3600.0)


def _freshness_score(value: datetime | None) -> float:
    age = _age_hours(value)
    if age <= PREFERENCE_DECAY_START_HOURS:
        return 1.0
    if age >= PREFERENCE_EXPIRY_HOURS:
        return 0.0
    if age <= PREFERENCE_STALE_AFTER_HOURS:
        span = max(1.0, float(PREFERENCE_STALE_AFTER_HOURS - PREFERENCE_DECAY_START_HOURS))
        decay = (age - float(PREFERENCE_DECAY_START_HOURS)) / span
        return _bounded(1.0 - (0.35 * decay))
    span = max(1.0, float(PREFERENCE_EXPIRY_HOURS - PREFERENCE_STALE_AFTER_HOURS))
    decay = (age - float(PREFERENCE_STALE_AFTER_HOURS)) / span
    return _bounded(0.65 - (0.65 * decay))


def _preference_timestamp_rank(value: datetime | None) -> float:
    resolved = _as_utc(value)
    return resolved.timestamp() if resolved is not None else 0.0


def _preference_type(*, managed_scope: str, commitment_family: str, decision_type: str) -> str:
    scope_key = str(managed_scope or "global").strip().replace(" ", "_")[:40] or "global"
    family_key = str(commitment_family or "general").strip().replace(" ", "_")[:24] or "general"
    decision_key = str(decision_type or "default").strip().replace(" ", "_")[:24] or "default"
    return f"{LEARNED_PREFERENCE_PREFIX}{scope_key}:{family_key}:{decision_key}"


def _policy_effects(*, direction: str, strength: float, decision_type: str, commitment_family: str) -> dict:
    strategy_delta = 0.0
    stewardship_delta = 0.0
    inquiry_delta = 0.0
    preferred_autonomy_level = ""

    if commitment_family in {"evidence_gate", "action_timing", "autonomy_posture"} or decision_type in {
        "require_additional_evidence",
        "defer_action",
        "lower_autonomy_for_scope",
    }:
        strategy_delta = 0.08 * strength
        stewardship_delta = 0.1 * strength
        inquiry_delta = 0.15 * strength
        preferred_autonomy_level = "operator_required"
    elif decision_type == "elevate_remediation_priority":
        strategy_delta = 0.06 * strength
        stewardship_delta = 0.08 * strength

    if direction == "avoid":
        strategy_delta *= -1.0
        stewardship_delta *= -1.0
        inquiry_delta *= -1.0
        preferred_autonomy_level = ""

    return {
        "strategy_weight_delta": round(strategy_delta, 6),
        "stewardship_priority_delta": round(stewardship_delta, 6),
        "inquiry_suppression_delta": round(inquiry_delta, 6),
        "preferred_autonomy_level": preferred_autonomy_level,
    }


def _normalize_preference_payload(row: UserPreference) -> dict:
    payload = _json_dict(row.value)
    return {
        "preference_key": str(payload.get("preference_key") or "").strip(),
        "managed_scope": str(payload.get("managed_scope") or "").strip(),
        "preference_family": str(payload.get("preference_family") or "").strip(),
        "decision_type": str(payload.get("decision_type") or "").strip(),
        "preference_status": str(payload.get("preference_status") or "").strip() or "active",
        "preference_direction": str(payload.get("preference_direction") or "").strip(),
        "strength_score": round(float(payload.get("strength_score", 0.0) or 0.0), 6),
        "confidence_score": round(float(payload.get("confidence_score", row.confidence) or row.confidence or 0.0), 6),
        "evidence_count": int(payload.get("evidence_count", 0) or 0),
        "success_count": int(payload.get("success_count", 0) or 0),
        "failure_count": int(payload.get("failure_count", 0) or 0),
        "override_count": int(payload.get("override_count", 0) or 0),
        "conflict_state": str(payload.get("conflict_state") or "none").strip(),
        "winning_rule": str(payload.get("winning_rule") or "").strip(),
        "policy_effects_json": _json_dict(payload.get("policy_effects_json", {})),
        "evidence_summary_json": _json_dict(payload.get("evidence_summary_json", {})),
        "metadata_json": _json_dict(payload.get("metadata_json", {})),
        "source": str(row.source or "").strip(),
        "last_updated": row.last_updated,
        "age_hours": round(float(payload.get("age_hours", 0.0) or 0.0), 6),
        "freshness_score": round(float(payload.get("freshness_score", 0.0) or 0.0), 6),
        "normalized_strength_score": round(float(payload.get("normalized_strength_score", 0.0) or 0.0), 6),
        "effective_strength_score": round(float(payload.get("effective_strength_score", 0.0) or 0.0), 6),
        "arbitration_scope": str(payload.get("arbitration_scope") or "").strip(),
        "arbitration_state": str(payload.get("arbitration_state") or "").strip(),
        "precedence_rule": str(payload.get("precedence_rule") or "").strip(),
        "arbitration_reasoning_json": _json_dict(payload.get("arbitration_reasoning_json", {})),
    }


def _group_key(row: WorkspaceOperatorResolutionCommitmentOutcomeProfile) -> tuple[str, str, str]:
    return (
        str(row.managed_scope or "global").strip() or "global",
        str(row.commitment_family or "general").strip() or "general",
        str(row.decision_type or "approve_current_path").strip() or "approve_current_path",
    )


def _compute_preference_payload(
    *,
    rows: list[WorkspaceOperatorResolutionCommitmentOutcomeProfile],
    min_evidence: int,
) -> dict:
    latest = rows[0]
    managed_scope, commitment_family, decision_type = _group_key(latest)
    outcome_statuses = [str(row.outcome_status or "").strip() for row in rows]
    success_count = sum(1 for status in outcome_statuses if status in TERMINAL_SUCCESS_OUTCOMES)
    failure_count = sum(1 for status in outcome_statuses if status in TERMINAL_FAILURE_OUTCOMES)
    override_count = 0
    for row in rows:
        metadata = _json_dict(row.metadata_json)
        if bool(metadata.get("manual_resolution", False)):
            override_count += 1
        if _json_dict(metadata.get("operator_learning_bias", {})):
            override_count += 1

    evidence_count = len(rows)
    consistency = (max(success_count, failure_count) / evidence_count) if evidence_count else 0.0
    conflict_state = "none"
    winning_rule = ""
    if success_count >= min_evidence and failure_count >= min_evidence:
        conflict_state = "active_conflict"
        winning_rule = "freshest_terminal_outcome"

    if evidence_count < min_evidence:
        preference_status = "insufficient_evidence"
        preference_direction = "monitor"
    elif failure_count > success_count:
        preference_status = "active"
        preference_direction = "avoid"
    elif success_count > failure_count:
        preference_status = "active"
        preference_direction = "reinforce"
    else:
        freshest = str(latest.outcome_status or "").strip()
        preference_status = "active"
        preference_direction = "avoid" if freshest in TERMINAL_FAILURE_OUTCOMES else "reinforce"
        if not winning_rule:
            winning_rule = "freshest_terminal_outcome"
            conflict_state = "active_conflict"

    strength_score = _bounded(
        ((max(success_count, failure_count) + (0.5 * override_count)) / max(1, evidence_count))
    )
    confidence_score = _bounded(0.2 + (evidence_count / 8.0) + (consistency * 0.25) + (override_count * 0.05))
    evidence_summary = {
        "latest_outcome_status": str(latest.outcome_status or "").strip(),
        "latest_outcome_reason": str(latest.outcome_reason or "").strip(),
        "outcome_counts": {
            "satisfied": success_count,
            "ineffective": sum(1 for status in outcome_statuses if status == "ineffective"),
            "harmful": sum(1 for status in outcome_statuses if status == "harmful"),
            "abandoned": sum(1 for status in outcome_statuses if status == "abandoned"),
            "superseded": sum(1 for status in outcome_statuses if status == "superseded"),
        },
        "lookback_rows": evidence_count,
        "latest_pattern_summary": _json_dict(latest.pattern_summary_json),
        "latest_learning_signals": _json_dict(latest.learning_signals_json),
    }
    policy_effects_json = _policy_effects(
        direction=preference_direction,
        strength=strength_score,
        decision_type=decision_type,
        commitment_family=commitment_family,
    )
    preference_key = f"{managed_scope}:{commitment_family}:{decision_type}"
    return {
        "preference_key": preference_key,
        "managed_scope": managed_scope,
        "preference_family": commitment_family,
        "decision_type": decision_type,
        "preference_status": preference_status,
        "preference_direction": preference_direction,
        "strength_score": round(strength_score, 6),
        "confidence_score": round(confidence_score, 6),
        "evidence_count": evidence_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "override_count": override_count,
        "conflict_state": conflict_state,
        "winning_rule": winning_rule,
        "policy_effects_json": policy_effects_json,
        "evidence_summary_json": evidence_summary,
        "metadata_json": {
            "objective88_operator_preference_convergence": True,
            "latest_outcome_id": int(latest.id),
            "lookback_hours": int(latest.evaluation_window_hours or 0),
        },
    }


def _normalized_strength_score(preference: dict) -> float:
    evidence_score = _bounded(float(int(preference.get("evidence_count", 0) or 0)) / 6.0)
    strength = _bounded(float(preference.get("strength_score", 0.0) or 0.0))
    confidence = _bounded(float(preference.get("confidence_score", 0.0) or 0.0))
    base = (strength * 0.55) + (confidence * 0.3) + (evidence_score * 0.15)
    if str(preference.get("conflict_state") or "").strip() == "active_conflict":
        base -= 0.08
    return _bounded(base)


def _effective_strength_score(preference: dict) -> float:
    freshness = _freshness_score(preference.get("last_updated"))
    base = _normalized_strength_score(preference)
    return _bounded(base * freshness)


def _winner_sort_key(preference: dict) -> tuple[float, float, int, float, float]:
    return (
        float(preference.get("effective_strength_score", 0.0) or 0.0),
        float(preference.get("confidence_score", 0.0) or 0.0),
        int(preference.get("evidence_count", 0) or 0),
        float(preference.get("freshness_score", 0.0) or 0.0),
        _preference_timestamp_rank(preference.get("last_updated")),
    )


def _state_priority(preference: dict) -> int:
    state = str(preference.get("arbitration_state") or "").strip()
    priorities = {
        "won_scope": 0,
        "standalone": 1,
        "deferred_to_operator_commitment": 2,
        "lost_scope_conflict": 3,
        "weak_signal": 4,
        "stale_signal": 5,
        "inactive": 6,
    }
    return int(priorities.get(state, 7))


def _base_reasoning_payload(*, preference: dict, candidate_count: int, winner: dict | None) -> dict:
    return {
        "candidate_count": int(candidate_count),
        "winning_preference_key": str((winner or {}).get("preference_key") or "").strip(),
        "winning_effective_strength_score": round(float((winner or {}).get("effective_strength_score", 0.0) or 0.0), 6),
        "age_hours": round(float(preference.get("age_hours", 0.0) or 0.0), 6),
    }


def _resolve_scope_preferences(
    *,
    preferences: list[dict],
    operator_commitment: object | None = None,
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in preferences:
        grouped.setdefault(str(item.get("managed_scope") or "global").strip() or "global", []).append(dict(item))

    resolved: list[dict] = []
    operator_decision_type = str(getattr(operator_commitment, "decision_type", "") or "").strip()
    has_operator_precedence = operator_commitment is not None

    for managed_scope, items in grouped.items():
        prepared: list[dict] = []
        for item in items:
            age_hours = _age_hours(item.get("last_updated"))
            freshness_score = _freshness_score(item.get("last_updated"))
            normalized_strength = _normalized_strength_score(item)
            effective_strength = _effective_strength_score(item)
            prepared.append(
                {
                    **item,
                    "age_hours": round(age_hours, 6),
                    "freshness_score": round(freshness_score, 6),
                    "normalized_strength_score": round(normalized_strength, 6),
                    "effective_strength_score": round(effective_strength, 6),
                    "arbitration_scope": managed_scope,
                    "arbitration_state": "inactive",
                    "precedence_rule": "none",
                    "arbitration_reasoning_json": {},
                }
            )

        actionable_candidates = [
            item
            for item in prepared
            if str(item.get("preference_status") or "").strip() == "active"
            and float(item.get("freshness_score", 0.0) or 0.0) > 0.15
            and float(item.get("effective_strength_score", 0.0) or 0.0) >= MIN_EFFECTIVE_PREFERENCE_STRENGTH
        ]
        winner = max(actionable_candidates, key=_winner_sort_key) if actionable_candidates else None
        candidate_count = len(actionable_candidates)
        runner_up_strength = 0.0
        if candidate_count > 1 and winner is not None:
            runner_up_strength = max(
                float(item.get("effective_strength_score", 0.0) or 0.0)
                for item in actionable_candidates
                if str(item.get("preference_key") or "") != str(winner.get("preference_key") or "")
            )

        for item in prepared:
            base_reasoning = _base_reasoning_payload(preference=item, candidate_count=candidate_count, winner=winner)
            if str(item.get("preference_status") or "").strip() != "active":
                item["arbitration_state"] = "inactive"
                item["precedence_rule"] = "inactive_preference"
                item["arbitration_reasoning_json"] = {
                    **base_reasoning,
                    "reason": "Preference is not active and is retained only for inspection.",
                }
            elif float(item.get("freshness_score", 0.0) or 0.0) <= 0.15:
                item["arbitration_state"] = "stale_signal"
                item["precedence_rule"] = "freshness_decay"
                item["arbitration_reasoning_json"] = {
                    **base_reasoning,
                    "reason": "Preference aged beyond the freshness window and is demoted until reinforced by new evidence.",
                }
            elif float(item.get("effective_strength_score", 0.0) or 0.0) < MIN_EFFECTIVE_PREFERENCE_STRENGTH:
                item["arbitration_state"] = "weak_signal"
                item["precedence_rule"] = "strength_governance"
                item["arbitration_reasoning_json"] = {
                    **base_reasoning,
                    "reason": "Preference stayed below the minimum effective strength threshold after confidence normalization and freshness decay.",
                }
            elif winner is None:
                item["arbitration_state"] = "weak_signal"
                item["precedence_rule"] = "strength_governance"
                item["arbitration_reasoning_json"] = {
                    **base_reasoning,
                    "reason": "No active preference in this scope met the minimum governance threshold for projection.",
                }
            elif str(item.get("preference_key") or "") == str(winner.get("preference_key") or ""):
                if has_operator_precedence:
                    item["arbitration_state"] = "deferred_to_operator_commitment"
                    item["precedence_rule"] = "operator_commitment_precedence"
                    item["arbitration_reasoning_json"] = {
                        **base_reasoning,
                        "operator_commitment_decision_type": operator_decision_type,
                        "reason": (
                            "This learned preference won scope arbitration but is temporarily masked by the active "
                            f"operator commitment {operator_decision_type or 'active_commitment'}."
                        ),
                    }
                elif candidate_count == 1:
                    item["arbitration_state"] = "standalone"
                    item["precedence_rule"] = "scope_effective_strength"
                    item["arbitration_reasoning_json"] = {
                        **base_reasoning,
                        "reason": "This is the only active learned preference in scope with enough strength and freshness to project downstream.",
                    }
                else:
                    item["arbitration_state"] = "won_scope"
                    item["precedence_rule"] = "scope_effective_strength"
                    item["arbitration_reasoning_json"] = {
                        **base_reasoning,
                        "runner_up_effective_strength_score": round(runner_up_strength, 6),
                        "reason": (
                            "This learned preference won scope arbitration on effective strength after evidence normalization "
                            f"and freshness decay ({float(item.get('effective_strength_score', 0.0) or 0.0):.3f} vs {runner_up_strength:.3f})."
                        ),
                    }
            else:
                item["arbitration_state"] = "lost_scope_conflict"
                item["precedence_rule"] = "scope_effective_strength"
                item["arbitration_reasoning_json"] = {
                    **base_reasoning,
                    "reason": (
                        "This learned preference remains inspectable but lost scope arbitration to the stronger active preference "
                        f"{str((winner or {}).get('preference_key') or '').strip()}."
                    ),
                }

        prepared.sort(
            key=lambda item: (
                str(item.get("managed_scope") or ""),
                _state_priority(item),
                -float(item.get("effective_strength_score", 0.0) or 0.0),
                -_preference_timestamp_rank(item.get("last_updated")),
            )
        )
        resolved.extend(prepared)

    resolved.sort(
        key=lambda item: (
            str(item.get("managed_scope") or ""),
            _state_priority(item),
            -float(item.get("effective_strength_score", 0.0) or 0.0),
            -_preference_timestamp_rank(item.get("last_updated")),
        )
    )
    return resolved


def learned_preference_is_actionable(preference: dict | None) -> bool:
    if not isinstance(preference, dict):
        return False
    if str(preference.get("preference_status") or "").strip() != "active":
        return False
    return str(preference.get("arbitration_state") or "").strip() in {"standalone", "won_scope"}


def _arbitration_reason(preference: dict | None) -> str:
    if not isinstance(preference, dict):
        return ""
    reasoning = _json_dict(preference.get("arbitration_reasoning_json", {}))
    return str(reasoning.get("reason") or "").strip()


async def list_learned_preferences(
    *,
    db: AsyncSession,
    managed_scope: str = "",
    limit: int = 50,
) -> list[dict]:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.like(f"{LEARNED_PREFERENCE_PREFIX}%"))
            .order_by(UserPreference.last_updated.desc())
            .limit(max(50, min(int(limit) * 4, 200)))
        )
    ).scalars().all()
    payload = _resolve_scope_preferences(preferences=[_normalize_preference_payload(row) for row in rows])
    if str(managed_scope or "").strip():
        requested_scope = str(managed_scope).strip()
        payload = [item for item in payload if str(item.get("managed_scope") or "").strip() == requested_scope]
    return payload[: max(1, min(int(limit), 200))]


async def latest_scope_learned_preference(
    *,
    db: AsyncSession,
    managed_scope: str,
    operator_commitment: object | None = None,
) -> dict | None:
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.like(f"{LEARNED_PREFERENCE_PREFIX}%"))
            .order_by(UserPreference.last_updated.desc())
            .limit(80)
        )
    ).scalars().all()
    preferences = _resolve_scope_preferences(
        preferences=[
            _normalize_preference_payload(row)
            for row in rows
            if str(_normalize_preference_payload(row).get("managed_scope") or "").strip() == str(managed_scope or "").strip()
        ],
        operator_commitment=operator_commitment,
    )
    return preferences[0] if preferences else None


async def get_learned_preference(
    *,
    db: AsyncSession,
    preference_key: str,
) -> dict | None:
    requested = str(preference_key or "").strip()
    if not requested:
        return None
    rows = (
        await db.execute(
            select(UserPreference)
            .where(UserPreference.user_id == "operator")
            .where(UserPreference.preference_type.like(f"{LEARNED_PREFERENCE_PREFIX}%"))
            .order_by(UserPreference.last_updated.desc())
        )
    ).scalars().all()
    normalized = _resolve_scope_preferences(preferences=[_normalize_preference_payload(row) for row in rows])
    for payload in normalized:
        if str(payload.get("preference_key") or "").strip() == requested:
            return payload
    return None


async def converge_learned_preferences(
    *,
    actor: str,
    source: str,
    db: AsyncSession,
    managed_scope: str = "",
    decision_type: str = "",
    commitment_family: str = "",
    lookback_hours: int = 720,
    min_evidence: int = 3,
) -> list[dict]:
    del actor
    since = _utcnow() - timedelta(hours=max(1, int(lookback_hours)))
    stmt = select(WorkspaceOperatorResolutionCommitmentOutcomeProfile).where(
        WorkspaceOperatorResolutionCommitmentOutcomeProfile.created_at >= since
    )
    if str(managed_scope or "").strip():
        stmt = stmt.where(
            WorkspaceOperatorResolutionCommitmentOutcomeProfile.managed_scope == str(managed_scope).strip()
        )
    if str(decision_type or "").strip():
        stmt = stmt.where(
            WorkspaceOperatorResolutionCommitmentOutcomeProfile.decision_type == str(decision_type).strip()
        )
    if str(commitment_family or "").strip():
        stmt = stmt.where(
            WorkspaceOperatorResolutionCommitmentOutcomeProfile.commitment_family == str(commitment_family).strip()
        )
    rows = (
        await db.execute(
            stmt.order_by(WorkspaceOperatorResolutionCommitmentOutcomeProfile.created_at.desc())
        )
    ).scalars().all()

    grouped: dict[tuple[str, str, str], list[WorkspaceOperatorResolutionCommitmentOutcomeProfile]] = {}
    for row in rows:
        grouped.setdefault(_group_key(row), []).append(row)

    converged: list[dict] = []
    for grouped_rows in grouped.values():
        payload = _compute_preference_payload(rows=grouped_rows, min_evidence=max(1, int(min_evidence)))
        row = await upsert_user_preference(
            db=db,
            preference_type=_preference_type(
                managed_scope=payload["managed_scope"],
                commitment_family=payload["preference_family"],
                decision_type=payload["decision_type"],
            ),
            value=payload,
            confidence=float(payload["confidence_score"]),
            source=source,
            user_id="operator",
        )
        row.last_updated = _utcnow()
        converged.append(_normalize_preference_payload(row))

    converged.sort(key=lambda item: (str(item.get("managed_scope") or ""), str(item.get("decision_type") or "")))
    return converged


def learned_preference_strategy_influence(*, strategy_type: str, preference: dict | None) -> dict:
    if not preference:
        return {"strategy_weight": 0.0, "rationale": "", "preference_key": "", "arbitration_state": "", "precedence_rule": ""}
    if not learned_preference_is_actionable(preference):
        return {
            "strategy_weight": 0.0,
            "rationale": _arbitration_reason(preference),
            "preference_key": str(preference.get("preference_key") or ""),
            "arbitration_state": str(preference.get("arbitration_state") or ""),
            "precedence_rule": str(preference.get("precedence_rule") or ""),
        }
    effects = _json_dict(preference.get("policy_effects_json", {}))
    delta = float(effects.get("strategy_weight_delta", 0.0) or 0.0)
    direction = str(preference.get("preference_direction") or "").strip()
    decision_type = str(preference.get("decision_type") or "").strip()
    if direction == "reinforce":
        if strategy_type not in {
            "stabilize_uncertain_zones_before_action",
            "maintain_workspace_readiness",
            "prioritize_development_improvements_affecting_active_workflows",
        }:
            delta *= 0.4
    elif direction == "avoid":
        if strategy_type in {
            "prioritize_development_improvements_affecting_active_workflows",
            "reduce_operator_interruption_load",
        }:
            delta *= -1.0
        elif strategy_type not in {
            "stabilize_uncertain_zones_before_action",
            "maintain_workspace_readiness",
        }:
            delta *= 0.25
    delta = max(-0.2, min(0.2, delta))
    if abs(delta) < 1e-9:
        return {
            "strategy_weight": 0.0,
            "rationale": "",
            "preference_key": str(preference.get("preference_key") or ""),
            "arbitration_state": str(preference.get("arbitration_state") or ""),
            "precedence_rule": str(preference.get("precedence_rule") or ""),
        }
    rationale = (
        f"Learned operator preference {direction or 'active'} adjusted strategy pressure"
        f" after repeated {decision_type or 'operator'} outcomes in this scope."
    )
    return {
        "strategy_weight": round(delta, 6),
        "rationale": rationale,
        "preference_key": str(preference.get("preference_key") or ""),
        "arbitration_state": str(preference.get("arbitration_state") or ""),
        "precedence_rule": str(preference.get("precedence_rule") or ""),
    }


def learned_preference_autonomy_influence(*, preference: dict | None) -> dict:
    if not preference:
        return {"preferred_autonomy_level": "", "rationale": "", "confidence": 0.0}
    if not learned_preference_is_actionable(preference):
        return {
            "preferred_autonomy_level": "",
            "rationale": _arbitration_reason(preference),
            "confidence": 0.0,
        }
    effects = _json_dict(preference.get("policy_effects_json", {}))
    preferred = str(effects.get("preferred_autonomy_level") or "").strip()
    if not preferred:
        return {"preferred_autonomy_level": "", "rationale": "", "confidence": 0.0}
    rationale = (
        f"Learned operator preference {str(preference.get('preference_direction') or '').strip() or 'active'}"
        f" converged toward {preferred} for {str(preference.get('decision_type') or '').strip() or 'operator guidance'}."
    )
    return {
        "preferred_autonomy_level": preferred,
        "rationale": rationale,
        "confidence": float(preference.get("confidence_score", 0.0) or 0.0),
    }


def learned_preference_stewardship_weight_delta(*, preference: dict | None) -> float:
    if not preference or not learned_preference_is_actionable(preference):
        return 0.0
    effects = _json_dict(preference.get("policy_effects_json", {}))
    return float(effects.get("stewardship_priority_delta", 0.0) or 0.0)


def preference_conflicts(preferences: list[dict]) -> list[dict]:
    by_scope: dict[str, list[dict]] = {}
    for item in preferences:
        by_scope.setdefault(str(item.get("managed_scope") or "global").strip() or "global", []).append(item)

    conflicts: list[dict] = []
    for managed_scope, items in by_scope.items():
        internal_conflicts = [
            item for item in items if str(item.get("conflict_state") or "").strip() == "active_conflict"
        ]
        scope_candidates = [
            item
            for item in items
            if str(item.get("arbitration_state") or "").strip() in {
                "won_scope",
                "lost_scope_conflict",
                "deferred_to_operator_commitment",
            }
        ]
        if not internal_conflicts and len(scope_candidates) <= 1:
            continue
        winner = next(
            (
                item
                for item in items
                if str(item.get("arbitration_state") or "").strip() in {"won_scope", "standalone", "deferred_to_operator_commitment"}
            ),
            items[0] if items else {},
        )
        conflicts.append(
            {
                "managed_scope": managed_scope,
                "conflict_kind": "scope_arbitration" if len(scope_candidates) > 1 else "internal_evidence_conflict",
                "winning_preference_key": str(winner.get("preference_key") or "").strip(),
                "winning_rule": str(winner.get("precedence_rule") or winner.get("winning_rule") or "").strip(),
                "winning_reason": _arbitration_reason(winner),
                "candidates": [
                    {
                        "preference_key": str(item.get("preference_key") or "").strip(),
                        "decision_type": str(item.get("decision_type") or "").strip(),
                        "preference_direction": str(item.get("preference_direction") or "").strip(),
                        "effective_strength_score": round(float(item.get("effective_strength_score", 0.0) or 0.0), 6),
                        "arbitration_state": str(item.get("arbitration_state") or "").strip(),
                        "precedence_rule": str(item.get("precedence_rule") or "").strip(),
                    }
                    for item in items
                ],
            }
        )
    return conflicts