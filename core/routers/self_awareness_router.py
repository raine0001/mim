"""Router for MIM self-health status, diagnostics, and self-optimization.

Exposes MIM's awareness of its own operational state and optimization proposals.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.runtime_recovery_service import RuntimeRecoveryService
from core.self_health_monitor import HealthMetric, SelfHealthMonitor
from core.self_optimizer_service import OptimizationStatus, SelfOptimizerService
from core.ui_health_service import build_mim_ui_health_snapshot, merge_health_status

# Initialize services (shared instances)
health_monitor = SelfHealthMonitor(memory_window=300, sample_interval=5, state_dir=Path("runtime/shared"))
optimizer_service = SelfOptimizerService(Path("runtime/shared"))
runtime_recovery_service = RuntimeRecoveryService(Path("runtime/shared"))

router = APIRouter(prefix="/mim/self", tags=["self-awareness"])


# ============================================================================
# Pydantic models for API contracts
# ============================================================================


class HealthTrendResponse(BaseModel):
    """Health trend summary."""
    metric_name: str
    current_value: float | None = None
    average: float | None = None
    trend: str  # "stable", "increasing", "decreasing"
    degradation_detected: bool = False


class HealthSummaryResponse(BaseModel):
    """MIM's comprehensive health status."""
    generated_at: str
    uptime_seconds: int
    status: str  # "healthy", "suboptimal", "degraded", "critical"
    metrics_sampled: int
    health_window_seconds: int
    trends: dict[str, dict] = Field(default_factory=dict)
    recommendations_count: int = 0
    high_severity_issues: int = 0


class OptimizationRecommendationResponse(BaseModel):
    """Optimization recommendation from self-diagnostics."""
    recommendation_id: str
    category: str
    severity: str  # "low", "medium", "high"
    title: str
    description: str
    proposed_action: str
    expected_benefit: str
    estimated_impact_percent: int | None = None


class OptimizationProposalResponse(BaseModel):
    """Tracked optimization proposal with governance state."""
    proposal_id: str
    recommendation_id: str
    title: str
    description: str
    status: str
    requires_approval: bool
    severity: str
    estimated_impact_percent: int | None = None
    created_at: str | None = None
    approved_at: str | None = None
    executed_at: str | None = None
    execution_result: dict | None = None
    error_message: str | None = None


class RecordMetricRequest(BaseModel):
    """Request to record a health metric."""
    memory_mb: int | None = None
    memory_percent: float | None = None
    cpu_percent: float | None = None
    api_latency_ms: float | None = None
    api_error_rate: float | None = None
    db_conn_pool_used: int | None = None
    db_conn_pool_size: int | None = None
    state_bus_lag_ms: int | None = None
    cache_hit_rate: float | None = None
    worker_queue_depth: int | None = None


class ApproveOptimizationRequest(BaseModel):
    """Request to approve an optimization proposal."""
    reason: str | None = None


class ExecuteOptimizationRequest(BaseModel):
    """Request to execute an approved optimization."""
    pass


# ============================================================================
# Health & Diagnostics Endpoints
# ============================================================================


@router.get("/health", response_model=HealthSummaryResponse, tags=["diagnostics"])
async def get_mim_health_status(db: AsyncSession = Depends(get_db)) -> HealthSummaryResponse:
    """Get MIM's comprehensive health and self-diagnostics status.

    Includes trends, detected issues, and optimization recommendations.
    Called by MIM to become aware of its own operational state.
    """
    summary = health_monitor.get_health_summary()
    ui_runtime_health = await build_mim_ui_health_snapshot(db=db)
    ui_runtime_recovery = runtime_recovery_service.get_summary()
    summary["status"] = merge_health_status(summary.get("status", "healthy"), ui_runtime_health.get("status", "healthy"))
    if str(ui_runtime_recovery.get("status") or "") == "degraded":
        summary["status"] = merge_health_status(summary.get("status", "healthy"), "degraded")

    # Count high severity issues in recommendations
    high_severity = sum(1 for r in summary.get("recommendations", []) if r.get("severity") == "high")
    recommendations_count = len(summary.get("recommendations", []))

    return HealthSummaryResponse(
        generated_at=summary["generated_at"],
        uptime_seconds=summary["uptime_seconds"],
        status=summary["status"],
        metrics_sampled=summary["metrics_sampled"],
        health_window_seconds=summary["health_window_seconds"],
        recommendations_count=recommendations_count,
        high_severity_issues=high_severity,
    )


@router.get("/health/detailed", tags=["diagnostics"])
async def get_detailed_health_report(db: AsyncSession = Depends(get_db)) -> dict:
    """Get full health report with all trends and metrics.

    Detailed diagnostics for self-checks. Returns raw metric history and trend analysis.
    """
    summary = health_monitor.get_health_summary()
    ui_runtime_health = await build_mim_ui_health_snapshot(db=db)
    ui_runtime_recovery = runtime_recovery_service.get_summary()
    summary["ui_runtime_health"] = ui_runtime_health
    summary["ui_runtime_recovery"] = ui_runtime_recovery
    summary["status"] = merge_health_status(summary.get("status", "healthy"), ui_runtime_health.get("status", "healthy"))
    if str(ui_runtime_recovery.get("status") or "") == "degraded":
        summary["status"] = merge_health_status(summary.get("status", "healthy"), "degraded")
    runtime_diagnostics = summary.get("runtime_diagnostics", [])
    if isinstance(runtime_diagnostics, list):
        summary["runtime_diagnostics"] = [*runtime_diagnostics, *(ui_runtime_health.get("diagnostics", []) or [])]
    return summary


@router.get("/recommendations", response_model=list[OptimizationRecommendationResponse], tags=["diagnostics"])
async def get_optimization_recommendations() -> list[OptimizationRecommendationResponse]:
    """Get current optimization recommendations from health diagnostics.

    These are proposals for self-improvement that MIM should consider.
    """
    summary = health_monitor.get_health_summary()
    recommendations = summary.get("recommendations", [])
    return [OptimizationRecommendationResponse(**r) for r in recommendations]


@router.post("/health/record-metric", tags=["diagnostics"])
async def record_health_metric(request: RecordMetricRequest) -> dict:
    """Record a health metric sample.

    Called periodically (or on-demand) by MIM's monitoring thread to sample current state.
    """
    import datetime
    metric = HealthMetric(
        timestamp=datetime.datetime.utcnow().isoformat() + "Z",
        memory_mb=request.memory_mb,
        memory_percent=request.memory_percent,
        cpu_percent=request.cpu_percent,
        api_latency_ms=request.api_latency_ms,
        api_error_rate=request.api_error_rate,
        db_conn_pool_used=request.db_conn_pool_used,
        db_conn_pool_size=request.db_conn_pool_size,
        state_bus_lag_ms=request.state_bus_lag_ms,
        cache_hit_rate=request.cache_hit_rate,
        worker_queue_depth=request.worker_queue_depth,
    )
    health_monitor.record_metric(metric)
    return {"status": "recorded", "timestamp": metric.timestamp}


# ============================================================================
# Optimization Governance Endpoints
# ============================================================================


@router.post("/optimize/propose", response_model=OptimizationProposalResponse, tags=["optimization"])
async def propose_optimization(
    recommendation_id: str,
    title: str,
    description: str,
    proposed_action: str,
    severity: str = "medium",
    requires_approval: bool = True,
) -> OptimizationProposalResponse:
    """Create an optimization proposal from a recommendation.

    Bridges health diagnostics to governance. MIM proposes optimizations,
    which are tracked and approved by operators.
    """
    proposal = optimizer_service.propose_optimization(
        recommendation_id=recommendation_id,
        title=title,
        description=description,
        proposed_action=proposed_action,
        requires_approval=requires_approval,
        severity=severity,
    )
    return OptimizationProposalResponse(**dataclass_to_dict(proposal))


@router.get("/optimize/proposals", response_model=list[OptimizationProposalResponse], tags=["optimization"])
async def list_proposals(status: str | None = None, severity: str | None = None) -> list[OptimizationProposalResponse]:
    """List all optimization proposals, optionally filtered by status or severity.

    Useful for understanding MIM's pending self-improvements and their approval state.
    """
    status_enum = OptimizationStatus(status) if status else None
    proposals = optimizer_service.list_proposals(status=status_enum, severity=severity)
    return [OptimizationProposalResponse(**dataclass_to_dict(p)) for p in proposals]


@router.get("/optimize/proposals/{proposal_id}", response_model=OptimizationProposalResponse, tags=["optimization"])
async def get_proposal(proposal_id: str) -> OptimizationProposalResponse:
    """Get details of a specific optimization proposal."""
    proposal = optimizer_service.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    return OptimizationProposalResponse(**dataclass_to_dict(proposal))


@router.post("/optimize/proposals/{proposal_id}/approve", response_model=OptimizationProposalResponse, tags=["optimization"])
async def approve_optimization(
    proposal_id: str, request: ApproveOptimizationRequest
) -> OptimizationProposalResponse:
    """Approve an optimization proposal for execution.

    Operator gates self-optimizations for higher-impact changes.
    """
    try:
        proposal = optimizer_service.approve_proposal(proposal_id, request.reason or "")
        return OptimizationProposalResponse(**dataclass_to_dict(proposal))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/optimize/proposals/{proposal_id}/reject", response_model=OptimizationProposalResponse, tags=["optimization"])
async def reject_optimization(proposal_id: str) -> OptimizationProposalResponse:
    """Reject an optimization proposal.

    Stops a proposal without approving it (remains in history for audit).
    """
    try:
        proposal = optimizer_service.reject_proposal(proposal_id)
        return OptimizationProposalResponse(**dataclass_to_dict(proposal))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/optimize/proposals/{proposal_id}/execute", response_model=OptimizationProposalResponse, tags=["optimization"])
async def execute_optimization(proposal_id: str) -> OptimizationProposalResponse:
    """Execute an approved optimization proposal.

    Actually applies the self-optimization. Requires approval for high-impact changes.
    """
    try:
        proposal = optimizer_service.execute_proposal(proposal_id)
        return OptimizationProposalResponse(**dataclass_to_dict(proposal))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/optimize/proposals/{proposal_id}/rollback", response_model=OptimizationProposalResponse, tags=["optimization"])
async def rollback_optimization(proposal_id: str) -> OptimizationProposalResponse:
    """Rollback a completed optimization to previous state.

    Reverses optimizations if they prove ineffective or cause issues.
    """
    try:
        proposal = optimizer_service.rollback_proposal(proposal_id)
        return OptimizationProposalResponse(**dataclass_to_dict(proposal))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Utilities
# ============================================================================


def dataclass_to_dict(obj) -> dict:
    """Convert dataclass to dict, handling enum conversion."""
    import dataclasses
    if not dataclasses.is_dataclass(obj):
        return obj
    result = {}
    for field in dataclasses.fields(obj):
        value = getattr(obj, field.name)
        if hasattr(value, "value"):  # Enum
            result[field.name] = value.value
        else:
            result[field.name] = value
    return result
