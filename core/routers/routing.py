from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import RoutingEngineSummary, RoutingExecutionMetric
from core.schemas import RoutingMetricCreate

router = APIRouter()


def _serialize_metric(row: RoutingExecutionMetric) -> dict:
    return {
        "metric_id": row.id,
        "task_id": row.task_id,
        "objective_id": row.objective_id,
        "timestamp": row.created_at,
        "selected_engine": row.selected_engine,
        "fallback_engine": row.fallback_engine,
        "fallback_used": row.fallback_used,
        "routing_source": row.routing_source,
        "routing_confidence": row.routing_confidence,
        "policy_version": row.policy_version,
        "engine_version": row.engine_version,
        "routing_selection_reason": row.routing_selection_reason,
        "routing_final_outcome": row.routing_final_outcome,
        "latency_ms": row.latency_ms,
        "result_category": row.result_category,
        "failure_category": row.failure_category,
        "review_outcome": row.review_outcome,
        "blocked_pre_invocation": row.blocked_pre_invocation,
        "metadata_json": row.metadata_json,
    }


@router.post("/history")
async def create_routing_metric(payload: RoutingMetricCreate, db: AsyncSession = Depends(get_db)) -> dict:
    row = RoutingExecutionMetric(
        task_id=payload.task_id,
        objective_id=payload.objective_id,
        selected_engine=payload.selected_engine,
        fallback_engine=payload.fallback_engine,
        fallback_used=payload.fallback_used,
        routing_source=payload.routing_source,
        routing_confidence=payload.routing_confidence,
        policy_version=payload.policy_version,
        engine_version=payload.engine_version,
        routing_selection_reason=payload.routing_selection_reason,
        routing_final_outcome=payload.routing_final_outcome,
        latency_ms=payload.latency_ms,
        result_category=payload.result_category,
        failure_category=payload.failure_category,
        review_outcome=payload.review_outcome,
        blocked_pre_invocation=payload.blocked_pre_invocation,
        metadata_json=payload.metadata_json,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _rebuild_engine_summaries(db, window=200)
    return {
        "metric_id": row.id,
        "timestamp": row.created_at,
    }


@router.get("/history")
async def list_routing_history(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    task_id: int | None = None,
    objective_id: int | None = None,
    policy_version: str | None = None,
    engine_version: str | None = None,
) -> list[dict]:
    stmt = select(RoutingExecutionMetric).order_by(RoutingExecutionMetric.id.desc()).limit(limit)

    if task_id is not None:
        stmt = stmt.where(RoutingExecutionMetric.task_id == task_id)
    if objective_id is not None:
        stmt = stmt.where(RoutingExecutionMetric.objective_id == objective_id)
    if policy_version is not None:
        stmt = stmt.where(RoutingExecutionMetric.policy_version == policy_version)
    if engine_version is not None:
        stmt = stmt.where(RoutingExecutionMetric.engine_version == engine_version)

    rows = (await db.execute(stmt)).scalars().all()
    return [_serialize_metric(row) for row in rows]


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


async def _compute_engine_metrics(db: AsyncSession, window: int) -> dict[str, dict]:
    rows = (await db.execute(select(RoutingExecutionMetric).order_by(RoutingExecutionMetric.id.desc()).limit(window))).scalars().all()

    return _compute_engine_metrics_from_rows(rows)


def _compute_engine_metrics_from_rows(rows: list[RoutingExecutionMetric]) -> dict[str, dict]:

    by_engine: dict[str, list[RoutingExecutionMetric]] = {}
    for row in rows:
        by_engine.setdefault(row.selected_engine, []).append(row)

    engine_metrics: dict[str, dict] = {}
    for engine_name, metrics in by_engine.items():
        runs = len(metrics)
        passes = sum(1 for row in metrics if row.routing_final_outcome == "success")
        review_fails = sum(1 for row in metrics if row.review_outcome == "fail")
        blocked = sum(1 for row in metrics if row.blocked_pre_invocation)
        fallback = sum(1 for row in metrics if row.fallback_used)
        avg_latency = round(sum(row.latency_ms for row in metrics) / runs, 2) if runs else 0.0

        recent_weighted_score = 0.0
        for idx, row in enumerate(metrics):
            weight = max(0.2, 1.0 - (idx * 0.03))
            outcome_val = 1.0 if row.routing_final_outcome == "success" else -1.0
            recent_weighted_score += outcome_val * weight
        recent_weighted_score = round(recent_weighted_score, 4)

        engine_metrics[engine_name] = {
            "runs": runs,
            "pass_rate": _rate(passes, runs),
            "review_correction_rate": _rate(review_fails, runs),
            "blocked_rate": _rate(blocked, runs),
            "avg_latency_ms": avg_latency,
            "fallback_rate": _rate(fallback, runs),
            "weighted_recent_score": recent_weighted_score,
        }

    return engine_metrics


async def _rebuild_engine_summaries(db: AsyncSession, window: int) -> None:
    engine_metrics = await _compute_engine_metrics(db, window)

    existing = (await db.execute(select(RoutingEngineSummary))).scalars().all()
    existing_by_name = {row.engine_name: row for row in existing}

    for engine_name, metrics in engine_metrics.items():
        row = existing_by_name.get(engine_name)
        if row is None:
            row = RoutingEngineSummary(engine_name=engine_name)
            db.add(row)

        row.runs = metrics["runs"]
        row.pass_rate = metrics["pass_rate"]
        row.review_correction_rate = metrics["review_correction_rate"]
        row.blocked_rate = metrics["blocked_rate"]
        row.avg_latency_ms = metrics["avg_latency_ms"]
        row.fallback_rate = metrics["fallback_rate"]
        row.weighted_recent_score = metrics["weighted_recent_score"]
        row.sample_window = window

    stale_engines = [name for name in existing_by_name.keys() if name not in engine_metrics]
    for name in stale_engines:
        await db.delete(existing_by_name[name])

    await db.commit()


@router.get("/engines")
async def get_engine_metrics(
    db: AsyncSession = Depends(get_db),
    window: int = Query(default=200, ge=10, le=5000),
    policy_version: str | None = None,
    engine_version: str | None = None,
) -> dict:
    if policy_version is not None or engine_version is not None:
        stmt = select(RoutingExecutionMetric).order_by(RoutingExecutionMetric.id.desc()).limit(window)
        if policy_version is not None:
            stmt = stmt.where(RoutingExecutionMetric.policy_version == policy_version)
        if engine_version is not None:
            stmt = stmt.where(RoutingExecutionMetric.engine_version == engine_version)
        rows = (await db.execute(stmt)).scalars().all()
        return {
            "window": window,
            "computed_from": "routing_execution_metrics_filtered",
            "filters": {
                "policy_version": policy_version,
                "engine_version": engine_version,
            },
            "engine_metrics": _compute_engine_metrics_from_rows(rows),
        }

    summaries = (await db.execute(select(RoutingEngineSummary).order_by(RoutingEngineSummary.engine_name.asc()))).scalars().all()

    if not summaries:
        await _rebuild_engine_summaries(db, window=window)
        summaries = (await db.execute(select(RoutingEngineSummary).order_by(RoutingEngineSummary.engine_name.asc()))).scalars().all()

    engine_metrics: dict[str, dict] = {}
    for row in summaries:
        engine_metrics[row.engine_name] = {
            "runs": row.runs,
            "pass_rate": row.pass_rate,
            "review_correction_rate": row.review_correction_rate,
            "blocked_rate": row.blocked_rate,
            "avg_latency_ms": row.avg_latency_ms,
            "fallback_rate": row.fallback_rate,
            "weighted_recent_score": row.weighted_recent_score,
        }

    return {
        "window": window,
        "computed_from": "routing_engine_summaries",
        "engine_metrics": engine_metrics,
    }


@router.get("/engines/{engine_name}")
async def get_engine_detail(
    engine_name: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    policy_version: str | None = None,
    engine_version: str | None = None,
) -> dict:
    summary = (
        await db.execute(select(RoutingEngineSummary).where(RoutingEngineSummary.engine_name == engine_name))
    ).scalar_one_or_none()

    stmt = (
        select(RoutingExecutionMetric)
        .where(RoutingExecutionMetric.selected_engine == engine_name)
        .order_by(RoutingExecutionMetric.id.desc())
        .limit(limit)
    )
    if policy_version is not None:
        stmt = stmt.where(RoutingExecutionMetric.policy_version == policy_version)
    if engine_version is not None:
        stmt = stmt.where(RoutingExecutionMetric.engine_version == engine_version)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "engine_name": engine_name,
        "filters": {
            "policy_version": policy_version,
            "engine_version": engine_version,
        },
        "summary": (
            {
                "runs": summary.runs,
                "pass_rate": summary.pass_rate,
                "review_correction_rate": summary.review_correction_rate,
                "blocked_rate": summary.blocked_rate,
                "avg_latency_ms": summary.avg_latency_ms,
                "fallback_rate": summary.fallback_rate,
                "weighted_recent_score": summary.weighted_recent_score,
                "sample_window": summary.sample_window,
            }
            if summary
            else None
        ),
        "recent_history": [_serialize_metric(row) for row in rows],
    }


@router.get("/stats")
async def get_routing_stats(db: AsyncSession = Depends(get_db), window: int = Query(default=200, ge=10, le=5000)) -> dict:
    rows = (await db.execute(select(RoutingExecutionMetric).order_by(RoutingExecutionMetric.id.desc()).limit(window))).scalars().all()
    total = len(rows)
    success = sum(1 for row in rows if row.routing_final_outcome == "success")
    fail = total - success
    avg_latency = round(sum(row.latency_ms for row in rows) / total, 2) if total else 0.0
    fallback = sum(1 for row in rows if row.fallback_used)
    blocked = sum(1 for row in rows if row.blocked_pre_invocation)

    return {
        "window": window,
        "total_runs": total,
        "success_rate": _rate(success, total),
        "failure_rate": _rate(fail, total),
        "avg_latency_ms": avg_latency,
        "fallback_rate": _rate(fallback, total),
        "blocked_rate": _rate(blocked, total),
    }


@router.get("/tasks/{task_id}")
async def get_routing_task_history(task_id: int, db: AsyncSession = Depends(get_db), limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    stmt = (
        select(RoutingExecutionMetric)
        .where(RoutingExecutionMetric.task_id == task_id)
        .order_by(RoutingExecutionMetric.id.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    return [_serialize_metric(row) for row in rows]


@router.get("/tasks/{task_id}/stats")
async def get_routing_task_stats(task_id: int, db: AsyncSession = Depends(get_db), window: int = Query(default=200, ge=10, le=5000)) -> dict:
    rows = (
        await db.execute(
            select(RoutingExecutionMetric)
            .where(RoutingExecutionMetric.task_id == task_id)
            .order_by(RoutingExecutionMetric.id.desc())
            .limit(window)
        )
    ).scalars().all()

    total = len(rows)
    success = sum(1 for row in rows if row.routing_final_outcome == "success")
    fail = total - success
    fallback = sum(1 for row in rows if row.fallback_used)
    blocked = sum(1 for row in rows if row.blocked_pre_invocation)
    avg_latency = round(sum(row.latency_ms for row in rows) / total, 2) if total else 0.0

    return {
        "task_id": task_id,
        "window": window,
        "total_runs": total,
        "success_rate": _rate(success, total),
        "failure_rate": _rate(fail, total),
        "fallback_rate": _rate(fallback, total),
        "blocked_rate": _rate(blocked, total),
        "avg_latency_ms": avg_latency,
    }
