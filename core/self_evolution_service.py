from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.improvement_governance_service import (
    get_improvement_backlog_item,
    list_improvement_backlog,
    refresh_improvement_backlog,
    to_improvement_backlog_out,
)
from core.improvement_recommendation_service import (
    get_improvement_recommendation,
    list_improvement_recommendations,
    to_improvement_recommendation_out_resolved,
)
from core.improvement_service import list_improvement_proposals, to_improvement_proposal_out
from core.improvement_service import get_improvement_proposal


def _count_values(values: list[str]) -> dict[str, int]:
    counts = Counter(item for item in values if item)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _snapshot_status(
    *,
    proposal_counts: dict[str, int],
    recommendation_counts: dict[str, int],
    backlog_counts: dict[str, int],
    risk_counts: dict[str, int],
    governance_counts: dict[str, int],
) -> str:
    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)
    active_backlog = sum(
        int(backlog_counts.get(key, 0) or 0)
        for key in ["queued", "experimenting", "evaluating", "recommended"]
    )
    high_risk_items = int(risk_counts.get("high", 0) or 0)

    if operator_review_count > 0 or (high_risk_items > 0 and active_backlog > 0):
        return "operator_review_required"
    if open_proposals > 0 or open_recommendations > 0 or active_backlog > 0:
        return "active"
    return "quiet"


def _snapshot_summary(
    *,
    status: str,
    proposal_counts: dict[str, int],
    recommendation_counts: dict[str, int],
    backlog_counts: dict[str, int],
    governance_counts: dict[str, int],
    top_priority_type: str,
) -> str:
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    queued_items = int(backlog_counts.get("queued", 0) or 0)
    recommended_items = int(backlog_counts.get("recommended", 0) or 0)
    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)

    if status == "operator_review_required":
        return (
            f"Self-evolution is active with {operator_review_count} backlog item(s) awaiting operator review; "
            f"open proposals={open_proposals}, open recommendations={open_recommendations}, "
            f"top priority type={top_priority_type or 'none'}."
        )
    if status == "active":
        return (
            f"Self-evolution is active with {queued_items + recommended_items} ranked backlog item(s), "
            f"open proposals={open_proposals}, open recommendations={open_recommendations}, "
            f"top priority type={top_priority_type or 'none'}."
        )
    return (
        "Self-evolution is quiet; no active ranked backlog pressure is present and the current loop is "
        f"holding at proposals={open_proposals}, recommendations={open_recommendations}."
    )


async def build_self_evolution_snapshot(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    fetch_limit = max(25, min(500, int(limit) * 5))
    if refresh:
        backlog_rows = await refresh_improvement_backlog(
            actor=actor,
            source=source,
            lookback_hours=lookback_hours,
            min_occurrence_count=min_occurrence_count,
            max_items=fetch_limit,
            auto_experiment_limit=auto_experiment_limit,
            metadata_json={
                "objective164_self_evolution": True,
                "refresh_via_self_evolution": True,
            },
            db=db,
        )
    else:
        backlog_rows = await list_improvement_backlog(
            db=db,
            status="",
            risk_level="",
            limit=fetch_limit,
        )

    proposal_rows = await list_improvement_proposals(
        db=db,
        status="",
        proposal_type="",
        limit=fetch_limit,
    )
    recommendation_rows = await list_improvement_recommendations(
        db=db,
        status="",
        recommendation_type="",
        limit=fetch_limit,
    )

    proposal_by_id = {int(row.id): row for row in proposal_rows}
    proposal_counts = _count_values([str(row.status or "").strip().lower() for row in proposal_rows])
    recommendation_counts = _count_values(
        [str(row.status or "").strip().lower() for row in recommendation_rows]
    )
    backlog_counts = _count_values([str(row.status or "").strip().lower() for row in backlog_rows])
    risk_counts = _count_values([str(row.risk_level or "").strip().lower() for row in backlog_rows])
    governance_counts = _count_values(
        [str(row.governance_decision or "").strip().lower() for row in backlog_rows]
    )

    top_backlog = backlog_rows[0] if backlog_rows else None
    top_priority_score = float(getattr(top_backlog, "priority_score", 0.0) or 0.0)
    top_priority_type = str(getattr(top_backlog, "proposal_type", "") or "").strip()
    top_proposal = proposal_by_id.get(int(getattr(top_backlog, "proposal_id", 0) or 0)) if top_backlog else None
    top_affected_component = str(getattr(top_proposal, "affected_component", "") or "").strip()

    status = _snapshot_status(
        proposal_counts=proposal_counts,
        recommendation_counts=recommendation_counts,
        backlog_counts=backlog_counts,
        risk_counts=risk_counts,
        governance_counts=governance_counts,
    )
    summary = _snapshot_summary(
        status=status,
        proposal_counts=proposal_counts,
        recommendation_counts=recommendation_counts,
        backlog_counts=backlog_counts,
        governance_counts=governance_counts,
        top_priority_type=top_priority_type,
    )

    top_proposals = [to_improvement_proposal_out(row) for row in proposal_rows[: max(1, int(limit))]]
    top_recommendations = [
        await to_improvement_recommendation_out_resolved(row=row, db=db)
        for row in recommendation_rows[: max(1, int(limit))]
    ]
    top_backlog_items = [
        to_improvement_backlog_out(row) for row in backlog_rows[: max(1, int(limit))]
    ]

    return {
        "status": status,
        "summary": summary,
        "proposal_counts": proposal_counts,
        "recommendation_counts": recommendation_counts,
        "backlog_counts": backlog_counts,
        "risk_counts": risk_counts,
        "governance_decision_counts": governance_counts,
        "top_priority_score": top_priority_score,
        "top_priority_type": top_priority_type,
        "top_affected_component": top_affected_component,
        "proposals": top_proposals,
        "recommendations": top_recommendations,
        "backlog": top_backlog_items,
        "metadata_json": {
            "actor": actor,
            "source": source,
            "refresh_requested": refresh,
            "lookback_hours": int(lookback_hours),
            "min_occurrence_count": int(min_occurrence_count),
            "auto_experiment_limit": int(auto_experiment_limit),
            "limit": int(limit),
            "objective164_self_evolution": True,
        },
        "created_at": datetime.now(timezone.utc),
    }


def _decision_payload(
    *,
    decision_type: str,
    priority: str,
    rationale: str,
    target_kind: str,
    target_id: int | None,
    action_method: str,
    action_path: str,
    action_payload: dict,
    summary: str,
    snapshot: dict,
    metadata_json: dict,
) -> dict:
    return {
        "decision_type": decision_type,
        "priority": priority,
        "rationale": rationale,
        "target_kind": target_kind,
        "target_id": target_id,
        "action": {
            "method": action_method,
            "path": action_path,
            "payload": action_payload,
        },
        "summary": summary,
        "snapshot_status": str(snapshot.get("status", "") or ""),
        "snapshot_summary": str(snapshot.get("summary", "") or ""),
        "metadata_json": metadata_json,
        "created_at": datetime.now(timezone.utc),
    }


async def build_self_evolution_next_action(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    snapshot = await build_self_evolution_snapshot(
        actor=actor,
        source=source,
        refresh=refresh,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        auto_experiment_limit=auto_experiment_limit,
        limit=limit,
        db=db,
    )

    backlog = snapshot.get("backlog", []) if isinstance(snapshot.get("backlog", []), list) else []
    recommendations = (
        snapshot.get("recommendations", []) if isinstance(snapshot.get("recommendations", []), list) else []
    )
    proposal_counts = snapshot.get("proposal_counts", {}) if isinstance(snapshot.get("proposal_counts", {}), dict) else {}
    recommendation_counts = (
        snapshot.get("recommendation_counts", {})
        if isinstance(snapshot.get("recommendation_counts", {}), dict)
        else {}
    )
    backlog_counts = snapshot.get("backlog_counts", {}) if isinstance(snapshot.get("backlog_counts", {}), dict) else {}
    governance_counts = (
        snapshot.get("governance_decision_counts", {})
        if isinstance(snapshot.get("governance_decision_counts", {}), dict)
        else {}
    )

    top_backlog = backlog[0] if backlog and isinstance(backlog[0], dict) else {}
    top_recommendation = recommendations[0] if recommendations and isinstance(recommendations[0], dict) else {}

    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    queued_backlog = int(backlog_counts.get("queued", 0) or 0)
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)

    if operator_review_count > 0 and int(top_backlog.get("recommendation_id", 0) or 0) > 0:
        recommendation_id = int(top_backlog.get("recommendation_id", 0) or 0)
        recommendation_row = await get_improvement_recommendation(
            recommendation_id=recommendation_id,
            db=db,
        )
        recommendation = (
            await to_improvement_recommendation_out_resolved(row=recommendation_row, db=db)
            if recommendation_row is not None
            else top_recommendation
        )
        proposal_type = str(top_backlog.get("proposal_type", "") or "")
        return {
            "decision": _decision_payload(
                decision_type="approve_ranked_recommendation",
                priority="high",
                rationale=(
                    "The current self-evolution loop is blocked behind operator review on the highest-ranked "
                    "backlog item, so the next bounded action is to review the linked recommendation."
                ),
                target_kind="recommendation",
                target_id=recommendation_id,
                action_method="POST",
                action_path=f"/improvement/recommendations/{recommendation_id}/approve",
                action_payload={
                    "actor": actor,
                    "reason": "objective165 guided approval for the highest-ranked self-evolution backlog item",
                    "metadata_json": {
                        "objective165_self_evolution_next_action": True,
                        "source": source,
                    },
                },
                summary=(
                    f"Review recommendation {recommendation_id} for the top-ranked {proposal_type or 'improvement'} "
                    "item before continuing the loop."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "operator_review_count": operator_review_count,
                    "recommendation": recommendation,
                    "backlog_item": top_backlog,
                },
            )
        }

    if open_recommendations > 0 and int(top_recommendation.get("recommendation_id", 0) or 0) > 0:
        recommendation_id = int(top_recommendation.get("recommendation_id", 0) or 0)
        recommendation_type = str(top_recommendation.get("recommendation_type", "") or "")
        return {
            "decision": _decision_payload(
                decision_type="review_open_recommendation",
                priority="medium",
                rationale=(
                    "Open recommendations already exist, so the next bounded action is to review the newest "
                    "recommendation before generating additional loop pressure."
                ),
                target_kind="recommendation",
                target_id=recommendation_id,
                action_method="GET",
                action_path=f"/improvement/recommendations/{recommendation_id}",
                action_payload={},
                summary=(
                    f"Inspect open recommendation {recommendation_id} ({recommendation_type or 'pending'}) "
                    "before creating more backlog churn."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "open_recommendations": open_recommendations,
                    "recommendation": top_recommendation,
                },
            )
        }

    if queued_backlog > 0 and int(top_backlog.get("improvement_id", 0) or 0) > 0:
        improvement_id = int(top_backlog.get("improvement_id", 0) or 0)
        return {
            "decision": _decision_payload(
                decision_type="inspect_ranked_backlog_item",
                priority="medium",
                rationale=(
                    "There is ranked backlog pressure but no open operator-review recommendation at the top, so the "
                    "next bounded action is to inspect the highest-priority backlog item."
                ),
                target_kind="backlog_item",
                target_id=improvement_id,
                action_method="GET",
                action_path=f"/improvement/backlog/{improvement_id}",
                action_payload={},
                summary=(
                    f"Inspect backlog item {improvement_id} with priority score "
                    f"{float(top_backlog.get('priority_score', 0.0) or 0.0):.4f}."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "queued_backlog": queued_backlog,
                    "backlog_item": top_backlog,
                },
            )
        }

    if open_proposals > 0:
        return {
            "decision": _decision_payload(
                decision_type="generate_recommendations",
                priority="medium",
                rationale=(
                    "Open proposals exist without enough downstream recommendation pressure, so the next bounded action "
                    "is to generate recommendations from the current proposal set."
                ),
                target_kind="proposal_batch",
                target_id=None,
                action_method="POST",
                action_path="/improvement/recommendations/generate",
                action_payload={
                    "actor": actor,
                    "source": source,
                    "lookback_hours": int(lookback_hours),
                    "min_occurrence_count": int(min_occurrence_count),
                    "max_recommendations": max(1, min(10, int(limit))),
                    "include_existing_open_proposals": True,
                    "metadata_json": {
                        "objective165_self_evolution_next_action": True,
                        "source": source,
                    },
                },
                summary=(
                    f"Generate recommendations for {open_proposals} open proposal(s) to keep the self-evolution "
                    "loop moving into governed review."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "open_proposals": open_proposals,
                },
            )
        }

    return {
        "decision": _decision_payload(
            decision_type="refresh_self_evolution_state",
            priority="low",
            rationale=(
                "No active proposal, recommendation, or backlog pressure is currently visible, so the next bounded "
                "action is to refresh the self-evolution loop state."
            ),
            target_kind="self_evolution",
            target_id=None,
            action_method="GET",
            action_path="/improvement/self-evolution",
            action_payload={
                "refresh": True,
                "actor": actor,
                "source": source,
                "lookback_hours": int(lookback_hours),
                "min_occurrence_count": int(min_occurrence_count),
                "auto_experiment_limit": int(auto_experiment_limit),
                "limit": int(limit),
            },
            summary="Refresh the self-evolution snapshot to look for new governed improvement pressure.",
            snapshot=snapshot,
            metadata_json={
                "objective165_self_evolution_next_action": True,
            },
        )
    }


async def build_self_evolution_briefing(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    decision_result = await build_self_evolution_next_action(
        actor=actor,
        source=source,
        refresh=refresh,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        auto_experiment_limit=auto_experiment_limit,
        limit=limit,
        db=db,
    )
    decision = decision_result.get("decision", {}) if isinstance(decision_result, dict) else {}
    target_kind = str(decision.get("target_kind", "") or "")
    target_id = decision.get("target_id")

    target_payload: dict = {
        "target_kind": target_kind,
        "target_id": target_id,
        "proposal": None,
        "recommendation": None,
        "backlog_item": None,
    }

    if target_kind == "recommendation" and int(target_id or 0) > 0:
        recommendation_row = await get_improvement_recommendation(
            recommendation_id=int(target_id or 0),
            db=db,
        )
        if recommendation_row is not None:
            target_payload["recommendation"] = await to_improvement_recommendation_out_resolved(
                row=recommendation_row,
                db=db,
            )
            proposal_row = await get_improvement_proposal(
                proposal_id=int(recommendation_row.proposal_id),
                db=db,
            )
            if proposal_row is not None:
                target_payload["proposal"] = to_improvement_proposal_out(proposal_row)

    if target_kind == "backlog_item" and int(target_id or 0) > 0:
        backlog_row = await get_improvement_backlog_item(
            backlog_id=int(target_id or 0),
            db=db,
        )
        if backlog_row is not None:
            target_payload["backlog_item"] = to_improvement_backlog_out(backlog_row)
            proposal_row = await get_improvement_proposal(
                proposal_id=int(backlog_row.proposal_id),
                db=db,
            )
            if proposal_row is not None:
                target_payload["proposal"] = to_improvement_proposal_out(proposal_row)
            if backlog_row.recommendation_id is not None:
                recommendation_row = await get_improvement_recommendation(
                    recommendation_id=int(backlog_row.recommendation_id),
                    db=db,
                )
                if recommendation_row is not None:
                    target_payload["recommendation"] = await to_improvement_recommendation_out_resolved(
                        row=recommendation_row,
                        db=db,
                    )

    return {
        "briefing": {
            "snapshot": await build_self_evolution_snapshot(
                actor=actor,
                source=source,
                refresh=False,
                lookback_hours=lookback_hours,
                min_occurrence_count=min_occurrence_count,
                auto_experiment_limit=auto_experiment_limit,
                limit=limit,
                db=db,
            ),
            "decision": decision,
            "target": target_payload,
            "metadata_json": {
                "objective166_self_evolution_briefing": True,
                "actor": actor,
                "source": source,
            },
            "created_at": datetime.now(timezone.utc),
        }
    }