from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceProposal, WorkspaceProposalArbitrationOutcome


SUCCESS_SCORES = {
    "won": 1.0,
    "merged": 0.75,
    "isolated": 0.55,
    "lost": 0.0,
    "suppressed": 0.0,
    "superseded": 0.2,
}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _normalize_zone(value: str) -> str:
    return str(value or "").strip() or "global"


def _normalized_decision(value: str) -> str:
    decision = str(value or "won").strip().lower()
    if decision in SUCCESS_SCORES:
        return decision
    return "won"


def _outcome_score(*, decision: str, execution_outcome: str) -> float:
    score = float(SUCCESS_SCORES.get(_normalized_decision(decision), 0.5))
    outcome = str(execution_outcome or "").strip().lower()
    if outcome in {"succeeded", "accepted", "completed"}:
        score += 0.05
    elif outcome in {"failed", "rejected", "abandoned"}:
        score -= 0.05
    return _bounded(score)


def _confidence_from_samples(sample_count: int, weighted_success_rate: float) -> float:
    volume = _bounded(float(sample_count) / 8.0)
    directional = _bounded(abs(float(weighted_success_rate) - 0.5) * 2.0)
    return _bounded((volume * 0.7) + (directional * 0.3))


def _priority_bias(weighted_success_rate: float, confidence: float) -> float:
    centered = float(weighted_success_rate) - 0.5
    return max(-0.12, min(0.12, centered * 0.3 * float(confidence)))


def _normalized_proposal_types(proposal_types: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in proposal_types:
        proposal_type = str(item or "").strip()
        if not proposal_type or proposal_type in seen:
            continue
        seen.add(proposal_type)
        cleaned.append(proposal_type)
    return cleaned


def _learning_summary(*, proposal_type: str, related_zone: str, rows: list[WorkspaceProposalArbitrationOutcome]) -> dict:
    sample_count = len(rows)
    if sample_count <= 0:
        return {
            "proposal_type": proposal_type,
            "related_zone": related_zone,
            "sample_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "merged_count": 0,
            "isolated_count": 0,
            "weighted_success_rate": 0.5,
            "confidence": 0.0,
            "priority_bias": 0.0,
            "suppression_recommended": False,
            "learned_posture": "neutral",
            "reasoning": "No arbitration outcomes recorded yet for this proposal type.",
            "recent_outcomes": [],
            "applied": False,
        }

    win_count = sum(1 for row in rows if str(row.arbitration_decision or "") == "won")
    loss_count = sum(1 for row in rows if str(row.arbitration_decision or "") in {"lost", "suppressed"})
    merged_count = sum(1 for row in rows if str(row.arbitration_decision or "") == "merged")
    isolated_count = sum(1 for row in rows if str(row.arbitration_decision or "") == "isolated")
    weighted_success_rate = sum(float(row.outcome_score or 0.0) for row in rows) / float(sample_count)
    confidence = _confidence_from_samples(sample_count, weighted_success_rate)
    priority_bias = _priority_bias(weighted_success_rate, confidence)
    applied = sample_count >= 2 and abs(priority_bias) >= 0.01
    learned_posture = "neutral"
    if priority_bias > 0.01:
        learned_posture = "favored"
    elif priority_bias < -0.01:
        learned_posture = "suppressed"
    reasoning = (
        f"Observed arbitration weighted_success_rate={weighted_success_rate:.3f} across {sample_count} outcome(s) "
        f"for proposal_type={proposal_type or 'unknown'} in zone={related_zone or 'global'}."
    )
    return {
        "proposal_type": proposal_type,
        "related_zone": related_zone,
        "sample_count": sample_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "merged_count": merged_count,
        "isolated_count": isolated_count,
        "weighted_success_rate": round(weighted_success_rate, 6),
        "confidence": round(confidence, 6),
        "priority_bias": round(priority_bias, 6),
        "suppression_recommended": priority_bias <= -0.04 and sample_count >= 3,
        "learned_posture": learned_posture,
        "reasoning": reasoning,
        "recent_outcomes": [
            {
                "outcome_id": int(row.id),
                "proposal_id": int(row.proposal_id) if row.proposal_id is not None else None,
                "arbitration_decision": str(row.arbitration_decision or ""),
                "arbitration_posture": str(row.arbitration_posture or ""),
                "downstream_execution_outcome": str(row.downstream_execution_outcome or ""),
                "outcome_score": round(float(row.outcome_score or 0.0), 6),
                "created_at": row.created_at.isoformat() if row.created_at is not None else None,
            }
            for row in rows[:5]
        ],
        "applied": applied,
    }


async def record_workspace_proposal_arbitration_outcome(
    *,
    actor: str,
    source: str,
    proposal_id: int | None,
    proposal_type: str,
    related_zone: str,
    arbitration_decision: str,
    arbitration_posture: str,
    trust_chain_status: str,
    downstream_execution_outcome: str,
    confidence: float,
    arbitration_reason: str,
    conflict_context_json: dict,
    commitment_state_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceProposalArbitrationOutcome:
    proposal: WorkspaceProposal | None = None
    resolved_proposal_id: int | None = int(proposal_id) if proposal_id and int(proposal_id) > 0 else None
    if resolved_proposal_id is not None:
        proposal = await db.get(WorkspaceProposal, resolved_proposal_id)
        if proposal is None:
            raise ValueError("workspace_proposal_not_found")

    resolved_type = str(proposal_type or getattr(proposal, "proposal_type", "") or "").strip()
    if not resolved_type:
        raise ValueError("proposal_type_required")
    resolved_zone = _normalize_zone(str(related_zone or getattr(proposal, "related_zone", "") or "global"))
    decision = _normalized_decision(arbitration_decision)
    outcome = WorkspaceProposalArbitrationOutcome(
        actor=str(actor or "tod"),
        source=str(source or "objective88_2"),
        proposal_id=resolved_proposal_id,
        proposal_type=resolved_type,
        related_zone=resolved_zone,
        arbitration_decision=decision,
        arbitration_posture=str(arbitration_posture or "isolate").strip() or "isolate",
        trust_chain_status=str(trust_chain_status or "verified").strip() or "verified",
        downstream_execution_outcome=str(downstream_execution_outcome or "").strip(),
        outcome_score=_outcome_score(decision=decision, execution_outcome=downstream_execution_outcome),
        confidence=_bounded(confidence),
        arbitration_reason=str(arbitration_reason or "").strip(),
        conflict_context_json=conflict_context_json if isinstance(conflict_context_json, dict) else {},
        commitment_state_json=commitment_state_json if isinstance(commitment_state_json, dict) else {},
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(outcome)
    await db.flush()
    return outcome


async def list_workspace_proposal_arbitration_outcomes(
    *,
    db: AsyncSession,
    proposal_type: str = "",
    related_zone: str = "",
    limit: int = 50,
) -> list[WorkspaceProposalArbitrationOutcome]:
    stmt = select(WorkspaceProposalArbitrationOutcome).order_by(WorkspaceProposalArbitrationOutcome.id.desc())
    if str(proposal_type or "").strip():
        stmt = stmt.where(WorkspaceProposalArbitrationOutcome.proposal_type == str(proposal_type).strip())
    if str(related_zone or "").strip():
        stmt = stmt.where(WorkspaceProposalArbitrationOutcome.related_zone == _normalize_zone(related_zone))
    stmt = stmt.limit(max(1, min(int(limit), 200)))
    return list((await db.execute(stmt)).scalars().all())


async def workspace_proposal_arbitration_learning_bias(
    *,
    proposal_type: str,
    related_zone: str,
    db: AsyncSession,
) -> dict:
    normalized_type = str(proposal_type or "").strip()
    if not normalized_type:
        return _learning_summary(proposal_type="", related_zone=_normalize_zone(related_zone), rows=[])

    zone = _normalize_zone(related_zone)
    zone_rows = (
        await db.execute(
            select(WorkspaceProposalArbitrationOutcome)
            .where(WorkspaceProposalArbitrationOutcome.proposal_type == normalized_type)
            .where(WorkspaceProposalArbitrationOutcome.related_zone == zone)
            .order_by(WorkspaceProposalArbitrationOutcome.id.desc())
            .limit(50)
        )
    ).scalars().all()
    if zone_rows:
        return _learning_summary(proposal_type=normalized_type, related_zone=zone, rows=list(zone_rows))

    rows = (
        await db.execute(
            select(WorkspaceProposalArbitrationOutcome)
            .where(WorkspaceProposalArbitrationOutcome.proposal_type == normalized_type)
            .order_by(WorkspaceProposalArbitrationOutcome.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return _learning_summary(proposal_type=normalized_type, related_zone=zone, rows=list(rows))


async def workspace_proposal_arbitration_family_influence(
    *,
    proposal_types: list[str],
    related_zone: str,
    db: AsyncSession,
    max_abs_bias: float = 0.12,
) -> dict:
    normalized_types = _normalized_proposal_types(proposal_types)
    zone = _normalize_zone(related_zone)
    if not normalized_types:
        return {
            "related_zone": zone,
            "proposal_types": [],
            "sample_count": 0,
            "learning": [],
            "aggregate_priority_bias": 0.0,
            "applied": False,
        }

    learning_rows: list[dict] = []
    weighted_bias_total = 0.0
    weight_total = 0.0
    sample_count = 0
    for proposal_type in normalized_types:
        learning = await workspace_proposal_arbitration_learning_bias(
            proposal_type=proposal_type,
            related_zone=zone,
            db=db,
        )
        if not isinstance(learning, dict):
            continue
        current_samples = int(learning.get("sample_count", 0) or 0)
        if current_samples <= 0:
            continue
        confidence = _bounded(float(learning.get("confidence", 0.0) or 0.0))
        effective_weight = max(0.25, confidence)
        priority_bias = float(learning.get("priority_bias", 0.0) or 0.0)
        weighted_bias_total += priority_bias * effective_weight
        weight_total += effective_weight
        sample_count += current_samples
        learning_rows.append(
            {
                "proposal_type": str(learning.get("proposal_type", proposal_type) or proposal_type),
                "sample_count": current_samples,
                "confidence": round(confidence, 6),
                "priority_bias": round(priority_bias, 6),
                "learned_posture": str(learning.get("learned_posture", "neutral") or "neutral"),
                "suppression_recommended": bool(learning.get("suppression_recommended", False)),
                "applied": bool(learning.get("applied", False)),
            }
        )

    if not learning_rows or weight_total <= 0.0:
        return {
            "related_zone": zone,
            "proposal_types": normalized_types,
            "sample_count": 0,
            "learning": [],
            "aggregate_priority_bias": 0.0,
            "applied": False,
        }

    aggregate_priority_bias = max(
        -abs(float(max_abs_bias)),
        min(abs(float(max_abs_bias)), weighted_bias_total / weight_total),
    )
    applied = sample_count >= 2 and abs(aggregate_priority_bias) >= 0.01
    if abs(aggregate_priority_bias) < 1e-9:
        aggregate_priority_bias = 0.0
        applied = False
    return {
        "related_zone": zone,
        "proposal_types": normalized_types,
        "sample_count": sample_count,
        "learning": learning_rows,
        "aggregate_priority_bias": round(aggregate_priority_bias, 6),
        "applied": applied,
    }


async def list_workspace_proposal_arbitration_learning(
    *,
    db: AsyncSession,
    related_zone: str = "",
    limit: int = 50,
) -> list[dict]:
    stmt = select(WorkspaceProposalArbitrationOutcome).order_by(WorkspaceProposalArbitrationOutcome.id.desc()).limit(500)
    if str(related_zone or "").strip():
        stmt = stmt.where(WorkspaceProposalArbitrationOutcome.related_zone == _normalize_zone(related_zone))
    rows = list((await db.execute(stmt)).scalars().all())
    grouped: dict[tuple[str, str], list[WorkspaceProposalArbitrationOutcome]] = defaultdict(list)
    for row in rows:
        key = (str(row.proposal_type or "").strip(), _normalize_zone(str(row.related_zone or "global")))
        if key[0]:
            grouped[key].append(row)
    payload = [
        _learning_summary(proposal_type=proposal_type, related_zone=zone, rows=grouped_rows)
        for (proposal_type, zone), grouped_rows in grouped.items()
    ]
    payload.sort(key=lambda item: (abs(float(item.get("priority_bias", 0.0) or 0.0)), int(item.get("sample_count", 0) or 0)), reverse=True)
    return payload[: max(1, min(int(limit), 200))]


def to_workspace_proposal_arbitration_out(row: WorkspaceProposalArbitrationOutcome) -> dict:
    return {
        "outcome_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "proposal_id": int(row.proposal_id) if row.proposal_id is not None else None,
        "proposal_type": row.proposal_type,
        "related_zone": row.related_zone,
        "arbitration_decision": row.arbitration_decision,
        "arbitration_posture": row.arbitration_posture,
        "trust_chain_status": row.trust_chain_status,
        "downstream_execution_outcome": row.downstream_execution_outcome,
        "outcome_score": round(float(row.outcome_score or 0.0), 6),
        "confidence": round(float(row.confidence or 0.0), 6),
        "arbitration_reason": row.arbitration_reason,
        "conflict_context_json": row.conflict_context_json if isinstance(row.conflict_context_json, dict) else {},
        "commitment_state_json": row.commitment_state_json if isinstance(row.commitment_state_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }