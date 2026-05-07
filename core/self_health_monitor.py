"""MIM self-health monitoring and diagnostics service.

Continuously tracks MIM's own performance, resource usage, and operational health.
Identifies degradation patterns and triggers optimization recommendations.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from core.runtime_recovery_service import RuntimeRecoveryService

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclasses.dataclass
class HealthMetric:
    """Single point-in-time health measurement."""

    timestamp: str
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
    uptime_seconds: int | None = None


@dataclasses.dataclass
class HealthTrend:
    """Statistical trend over a window of metrics."""

    metric_name: str
    window_seconds: int
    sample_count: int
    current_value: float | None
    average: float | None
    max_value: float | None
    min_value: float | None
    trend: str
    degradation_detected: bool = False


@dataclasses.dataclass
class OptimizationRecommendation:
    """Proposed self-optimization action."""

    recommendation_id: str
    category: str
    severity: str
    title: str
    description: str
    proposed_action: str
    expected_benefit: str
    requires_approval: bool = True
    estimated_impact_percent: int | None = None
    rollback_action: str | None = None


@dataclasses.dataclass
class RuntimeDiagnostic:
    """Point-in-time runtime diagnostic for freeze and coordination risk."""

    code: str
    severity: str
    summary: str
    detail: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


class SelfHealthMonitor:
    """MIM's health awareness and self-diagnostics engine."""

    def __init__(
        self,
        memory_window: int = 300,
        sample_interval: int = 5,
        state_dir: Path = Path("runtime/shared"),
    ):
        self.memory_window = memory_window
        self.sample_interval = sample_interval
        self.state_dir = state_dir
        self.metrics_history: deque[HealthMetric] = deque()
        self.last_sample_time = 0.0
        self.start_time = time.time()
        self.recommendations_history: list[OptimizationRecommendation] = []
        self.runtime_export_stale_after_seconds = 6 * 60 * 60
        self.runtime_ack_stale_after_seconds = 15 * 60
        self.runtime_duplicate_process_threshold = 1
        self.runtime_process_patterns = {
            "watch_tod_liveness.sh": "watch_tod_liveness.sh",
            "watch_shared_triggers.sh": "watch_shared_triggers.sh",
            "watch_mim_coordination_responder.sh": "watch_mim_coordination_responder.sh",
        }
        self.runtime_recovery_service = RuntimeRecoveryService(state_dir=state_dir)

    def record_metric(self, metric: HealthMetric) -> None:
        """Record a health metric sample."""

        self.metrics_history.append(metric)
        cutoff = time.time() - self.memory_window
        while self.metrics_history:
            parsed = self._parse_timestamp(self.metrics_history[0].timestamp)
            if parsed is None or parsed.timestamp() >= cutoff:
                break
            self.metrics_history.popleft()

    def get_trends(self, metric_name: str) -> HealthTrend | None:
        """Analyze trend for a specific metric."""

        values = []
        for metric in self.metrics_history:
            value = getattr(metric, metric_name, None)
            if value is not None:
                values.append(float(value))

        if not values:
            return None

        current = values[-1]
        average = sum(values) / len(values)
        trend = "stable"
        if len(values) >= 2:
            midpoint = max(1, len(values) // 2)
            recent = sum(values[midpoint:]) / (len(values) - midpoint)
            older = sum(values[:midpoint]) / midpoint
            if recent > older * 1.15:
                trend = "increasing"
            elif recent < older * 0.85:
                trend = "decreasing"

        degradation = False
        if metric_name == "memory_percent" and current > 80:
            degradation = True
        if metric_name == "api_latency_ms" and current > 200:
            degradation = True
        if metric_name == "api_error_rate" and current > 0.05:
            degradation = True
        if metric_name == "cache_hit_rate" and current < 0.5:
            degradation = True
        if metric_name == "state_bus_lag_ms" and current > 5000:
            degradation = True

        return HealthTrend(
            metric_name=metric_name,
            window_seconds=self.memory_window,
            sample_count=len(values),
            current_value=current,
            average=average,
            max_value=max(values),
            min_value=min(values),
            trend=trend,
            degradation_detected=degradation,
        )

    def analyze_and_recommend(self) -> list[OptimizationRecommendation]:
        """Analyze health trends and runtime diagnostics and generate recommendations."""

        recommendations: list[OptimizationRecommendation] = []
        diagnostics = self.get_runtime_diagnostics()

        mem_trend = self.get_trends("memory_percent")
        if mem_trend and mem_trend.degradation_detected and mem_trend.trend == "increasing":
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-mem-gc",
                    category="memory",
                    severity="high",
                    title="Trigger memory cleanup and garbage collection",
                    description=f"Memory usage at {mem_trend.current_value:.1f}% and trending upward. GC not recently triggered.",
                    proposed_action="trigger_garbage_collection",
                    expected_benefit="Recover 10-20% of memory footprint by clearing caches and collected objects.",
                    requires_approval=False,
                    estimated_impact_percent=15,
                    rollback_action="none_required",
                )
            )

        latency_trend = self.get_trends("api_latency_ms")
        if latency_trend and latency_trend.degradation_detected and latency_trend.trend == "increasing":
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-latency-scale",
                    category="latency",
                    severity="medium",
                    title="Increase worker pool size to reduce API latency",
                    description=f"API latency at {latency_trend.current_value:.0f}ms (avg {latency_trend.average:.0f}ms). Queue depth suggests thread pool saturation.",
                    proposed_action="increase_worker_pool_size",
                    expected_benefit="Reduce API latency by 20-30% with additional worker threads.",
                    requires_approval=True,
                    estimated_impact_percent=25,
                    rollback_action="decrease_worker_pool_size",
                )
            )

        cache_trend = self.get_trends("cache_hit_rate")
        if cache_trend and cache_trend.degradation_detected:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-cache-tune",
                    category="cache",
                    severity="medium",
                    title="Expand cache size to improve hit rate",
                    description=f"Cache hit rate at {cache_trend.current_value:.1%}. Expanding cache may reduce DB load and latency.",
                    proposed_action="increase_cache_size",
                    expected_benefit="Improve cache hit rate to 65-75%, reducing database queries by ~20%.",
                    requires_approval=True,
                    estimated_impact_percent=20,
                    rollback_action="decrease_cache_size",
                )
            )

        lag_trend = self.get_trends("state_bus_lag_ms")
        if lag_trend and lag_trend.degradation_detected and lag_trend.trend == "increasing":
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-statebus-lag",
                    category="resource",
                    severity="high",
                    title="Reduce state bus consumer batch timeout",
                    description=f"State bus lag at {lag_trend.current_value:.0f}ms. Consumers may be falling behind.",
                    proposed_action="reduce_state_bus_batch_timeout",
                    expected_benefit="Reduce lag to <1000ms by processing events more frequently.",
                    requires_approval=False,
                    estimated_impact_percent=30,
                    rollback_action="restore_state_bus_batch_timeout",
                )
            )

        stale_export = next(
            (item for item in diagnostics if item.code == "shared_export_stale_or_misaligned"),
            None,
        )
        coordination_ack_issue = next(
            (item for item in diagnostics if item.code == "coordination_ack_stale_or_misaligned"),
            None,
        )
        if stale_export is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-shared-export-refresh",
                    category="resource",
                    severity=stale_export.severity,
                    title="Refresh shared export artifacts",
                    description=stale_export.detail,
                    proposed_action="refresh_shared_export_artifacts",
                    expected_benefit="Restore canonical shared-state freshness so TOD/MIM coordination reflects the live objective and release metadata.",
                    requires_approval=True,
                    estimated_impact_percent=30,
                    rollback_action="none_required",
                )
            )
        if stale_export is not None or coordination_ack_issue is not None:
            primary_issue = stale_export or coordination_ack_issue
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-recover-bridge-coordination",
                    category="runtime",
                    severity=(primary_issue.severity if primary_issue is not None else "high"),
                    title="Recover stalled TOD bridge coordination",
                    description=(
                        primary_issue.detail
                        if primary_issue is not None
                        else "The TOD bridge needs a bounded republish and watcher recovery cycle."
                    ),
                    proposed_action="recover_bridge_coordination",
                    expected_benefit="Republish the active objective request surface, refresh shared artifacts, verify the bridge state, and restart the coordination watchers without waiting for manual approval.",
                    requires_approval=False,
                    estimated_impact_percent=35,
                    rollback_action="none_required",
                )
            )

        direct_execution_takeover = next(
            (item for item in diagnostics if item.code == "tod_direct_execution_takeover_ready"),
            None,
        )
        if direct_execution_takeover is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-direct-execution-takeover",
                    category="runtime",
                    severity=direct_execution_takeover.severity,
                    title="Take over TOD-silent execution locally",
                    description=direct_execution_takeover.detail,
                    proposed_action="fallback_to_codex_direct_execution",
                    expected_benefit="Stops indefinite TOD waiting by letting MIM claim bounded fallback authority and continue the active task through the local Codex/OpenAI handoff path.",
                    requires_approval=False,
                    estimated_impact_percent=40,
                    rollback_action="none_required",
                )
            )

        duplicate_watchers = next(
            (item for item in diagnostics if item.code == "duplicate_bridge_watchers"),
            None,
        )
        if duplicate_watchers is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-deduplicate-bridge-watchers",
                    category="resource",
                    severity=duplicate_watchers.severity,
                    title="Deduplicate bridge watcher processes",
                    description=duplicate_watchers.detail,
                    proposed_action="deduplicate_bridge_watchers",
                    expected_benefit="Reduce duplicate trigger emission and watchdog churn that can masquerade as freezes or stale coordination.",
                    requires_approval=True,
                    estimated_impact_percent=20,
                    rollback_action="none_required",
                )
            )

        runtime_recovery_attention = next(
            (
                item
                for item in diagnostics
                if item.code in {
                    "microphone_lane_recovery_instability",
                    "camera_lane_recovery_instability",
                    "camera_lane_bounded_retry_evidence",
                }
            ),
            None,
        )
        if runtime_recovery_attention is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id=f"opt-{runtime_recovery_attention.code}",
                    category="runtime",
                    severity=runtime_recovery_attention.severity,
                    title="Inspect browser, device, and runtime health for unstable recovery lane",
                    description=runtime_recovery_attention.detail,
                    proposed_action="inspect_runtime_devices_and_browser",
                    expected_benefit="Confirm whether browser media permissions, selected devices, or runtime conditions are causing repeated stale-lane recovery attempts.",
                    requires_approval=True,
                    estimated_impact_percent=15,
                    rollback_action="none_required",
                )
            )

        microphone_recovered = next(
            (item for item in diagnostics if item.code == "microphone_lane_recently_recovered"),
            None,
        )
        camera_bounded_retry = next(
            (item for item in diagnostics if item.code == "camera_lane_bounded_retry_evidence"),
            None,
        )
        if camera_bounded_retry is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-camera-lane-bounded-retry-evidence",
                    category="runtime",
                    severity="low",
                    title="Camera lane shows bounded retry evidence",
                    description=camera_bounded_retry.detail,
                    proposed_action="monitor_runtime_recovery_evidence",
                    expected_benefit="Preserve camera retry evidence so recurring stale-frame behavior can be separated from watcher crashes or backend threshold disagreements.",
                    requires_approval=False,
                    estimated_impact_percent=5,
                    rollback_action="none_required",
                )
            )
        if microphone_recovered is not None:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id="opt-microphone-lane-recently-recovered",
                    category="runtime",
                    severity="low",
                    title="Microphone lane recovered with bounded retry evidence",
                    description=microphone_recovered.detail,
                    proposed_action="monitor_runtime_recovery_evidence",
                    expected_benefit="Preserve evidence that the microphone lane recovered cleanly so future regressions can be compared against a known-good baseline.",
                    requires_approval=False,
                    estimated_impact_percent=5,
                    rollback_action="none_required",
                )
            )

        self.recommendations_history.extend(recommendations)
        return recommendations

    def get_runtime_diagnostics(self) -> list[RuntimeDiagnostic]:
        """Inspect runtime coordination artifacts and watcher processes for freeze precursors."""

        diagnostics: list[RuntimeDiagnostic] = []
        diagnostics.extend(self._shared_artifact_diagnostics())
        if self._watcher_process_scan_enabled():
            diagnostics.extend(self._watcher_process_diagnostics())
        diagnostics.extend(self._runtime_recovery_diagnostics())
        return diagnostics

    def _watcher_process_scan_enabled(self) -> bool:
        """Limit host-process watcher scans to the canonical runtime state directory.

        Temp-directory monitors are used heavily in tests and should not inherit
        live host watcher state from the developer machine.
        """

        try:
            return self.state_dir.resolve() == Path("runtime/shared").resolve()
        except Exception:
            return False

    def _runtime_recovery_diagnostics(self) -> list[RuntimeDiagnostic]:
        diagnostics: list[RuntimeDiagnostic] = []
        summary = self.runtime_recovery_service.get_summary()
        lanes = summary.get("lanes", {}) if isinstance(summary.get("lanes", {}), dict) else {}
        for lane in ("camera", "microphone"):
            lane_summary = lanes.get(lane, {}) if isinstance(lanes.get(lane, {}), dict) else {}
            if not lane_summary:
                continue
            unstable = bool(lane_summary.get("unstable", False))
            failure_count = int(lane_summary.get("failure_count", 0) or 0)
            attempt_count = int(lane_summary.get("recovery_attempt_count", 0) or 0)
            success_count = int(lane_summary.get("success_count", 0) or 0)
            stale_count = int(lane_summary.get("stale_detected_count", 0) or 0)
            if unstable or failure_count >= 2 or (lane == "camera" and stale_count >= 2 and attempt_count >= 2):
                diagnostics.append(
                    RuntimeDiagnostic(
                        code=f"{lane}_lane_recovery_instability",
                        severity="high" if failure_count >= 2 else "medium",
                        summary=f"{lane.capitalize()} lane recovery is showing instability.",
                        detail=str(lane_summary.get("summary") or "").strip(),
                        metadata={
                            "lane": lane,
                            "recovery_attempt_count": attempt_count,
                            "failure_count": failure_count,
                            "success_count": success_count,
                            "stale_detected_count": stale_count,
                            "next_retry_at": lane_summary.get("next_retry_at"),
                            "retry_reason_after_cooldown": lane_summary.get("retry_reason_after_cooldown"),
                        },
                    )
                )

            if lane == "camera" and bool(lane_summary.get("bounded_retry_evidence")) and stale_count >= 2 and success_count >= 1:
                diagnostics.append(
                    RuntimeDiagnostic(
                        code="camera_lane_bounded_retry_evidence",
                        severity="medium",
                        summary="Camera lane recovered but then needed another bounded retry.",
                        detail=(
                            f"{str(lane_summary.get('summary') or '').strip()} "
                            f"retry_reason_after_cooldown={lane_summary.get('retry_reason_after_cooldown') or 'unknown'}; "
                            f"last_healthy_frame_at={lane_summary.get('last_healthy_frame_at') or 'unknown'}."
                        ).strip(),
                        metadata={
                            "lane": lane,
                            "recovery_attempt_count": attempt_count,
                            "success_count": success_count,
                            "stale_detected_count": stale_count,
                            "retry_reason_after_cooldown": lane_summary.get("retry_reason_after_cooldown"),
                            "last_healthy_frame_at": lane_summary.get("last_healthy_frame_at"),
                            "watcher_running": lane_summary.get("watcher_running"),
                            "health_report_disagreement": lane_summary.get("health_report_disagreement"),
                        },
                    )
                )

            if lane == "microphone" and attempt_count >= 1 and success_count >= 1 and failure_count == 0:
                diagnostics.append(
                    RuntimeDiagnostic(
                        code="microphone_lane_recently_recovered",
                        severity="low",
                        summary="Microphone lane recovered cleanly from a stale interval.",
                        detail=(
                            f"{str(lane_summary.get('summary') or '').strip()} "
                            f"first_healthy_at={lane_summary.get('first_healthy_at') or 'unknown'}."
                        ).strip(),
                        metadata={
                            "lane": lane,
                            "recovery_attempt_count": attempt_count,
                            "success_count": success_count,
                            "first_healthy_at": lane_summary.get("first_healthy_at"),
                            "next_retry_at": lane_summary.get("next_retry_at"),
                        },
                    )
                )
        return diagnostics

    def get_health_summary(self) -> dict[str, Any]:
        """Generate comprehensive health summary."""

        uptime = time.time() - self.start_time
        trends: dict[str, dict[str, Any]] = {}
        for metric_name in [
            "memory_percent",
            "cpu_percent",
            "api_latency_ms",
            "api_error_rate",
            "cache_hit_rate",
            "state_bus_lag_ms",
        ]:
            trend = self.get_trends(metric_name)
            if trend:
                trends[metric_name] = dataclasses.asdict(trend)

        runtime_diagnostics = self.get_runtime_diagnostics()
        recommendations = self.analyze_and_recommend()

        return {
            "generated_at": _utc_now_iso(),
            "uptime_seconds": int(uptime),
            "metrics_sampled": len(self.metrics_history),
            "health_window_seconds": self.memory_window,
            "status": self._derive_status(trends, recommendations, runtime_diagnostics),
            "trends": trends,
            "recommendations": [dataclasses.asdict(item) for item in recommendations],
            "runtime_diagnostics": [dataclasses.asdict(item) for item in runtime_diagnostics],
            "latest_metrics": dataclasses.asdict(self.metrics_history[-1]) if self.metrics_history else None,
        }

    def _derive_status(
        self,
        trends: dict[str, dict[str, Any]],
        recommendations: list[OptimizationRecommendation],
        runtime_diagnostics: list[RuntimeDiagnostic],
    ) -> str:
        """Derive overall health status from trends, diagnostics, and recommendations."""

        high_severity_count = sum(1 for item in recommendations if item.severity == "high")
        degraded_count = sum(1 for item in trends.values() if item.get("degradation_detected"))
        runtime_high = sum(1 for item in runtime_diagnostics if item.severity == "high")
        runtime_medium = sum(1 for item in runtime_diagnostics if item.severity == "medium")

        if high_severity_count >= 2 or degraded_count >= 3 or runtime_high >= 2:
            return "critical"
        if high_severity_count >= 1 or degraded_count >= 2 or runtime_high >= 1 or runtime_medium >= 2:
            return "degraded"
        if recommendations or runtime_medium >= 1:
            return "suboptimal"
        return "healthy"

    def _shared_artifact_diagnostics(self) -> list[RuntimeDiagnostic]:
        diagnostics: list[RuntimeDiagnostic] = []

        context_export = self._read_json(self.state_dir / "MIM_CONTEXT_EXPORT.latest.json")
        handshake = self._read_json(self.state_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json")
        task_request = self._read_json(self.state_dir / "MIM_TOD_TASK_REQUEST.latest.json")
        publication_boundary = self._read_json(self.state_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json")
        coordination_request = self._read_json(self.state_dir / "TOD_MIM_COORDINATION_REQUEST.latest.json")
        coordination_ack = self._read_json(self.state_dir / "MIM_TOD_COORDINATION_ACK.latest.json")
        task_review = self._read_json(self.state_dir / "MIM_TASK_STATUS_REVIEW.latest.json")
        next_action = self._read_json(self.state_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json")
        fallback_activation = self._read_json(self.state_dir / "MIM_TOD_FALLBACK_ACTIVATION.latest.json")
        authoritative_request = publication_boundary.get("authoritative_request") if isinstance((publication_boundary or {}).get("authoritative_request"), dict) else None
        effective_task_request = authoritative_request if authoritative_request else task_request

        export_age = self._age_seconds_from_payload(context_export, ["exported_at", "generated_at"])
        task_objective = self._normalized_objective(effective_task_request)
        task_source_service = self._text((effective_task_request or {}).get("source_service")).lower()
        export_objective = self._normalized_objective(context_export)
        handshake_objective = self._normalized_objective(handshake)
        export_stale = export_age is None or export_age > self.runtime_export_stale_after_seconds
        stale_with_live_task = export_stale and bool(task_objective)
        objective_mismatch = bool(task_objective) and (
            task_objective != export_objective or task_objective != handshake_objective
        )
        if stale_with_live_task or objective_mismatch:
            diagnostics.append(
                RuntimeDiagnostic(
                    code="shared_export_stale_or_misaligned",
                    severity="high",
                    summary="Shared export is stale or behind the live task objective.",
                    detail=(
                        "Canonical shared-state artifacts are stale or still point at an older objective than the live task bus. "
                        f"task_request_objective={task_objective or 'unknown'}, "
                        f"context_export_objective={export_objective or 'unknown'}, "
                        f"handshake_objective={handshake_objective or 'unknown'}, "
                        f"export_age_seconds={export_age if export_age is not None else 'unknown'}."
                    ),
                    metadata={
                        "task_request_objective": task_objective,
                        "context_export_objective": export_objective,
                        "handshake_objective": handshake_objective,
                        "export_age_seconds": export_age,
                    },
                )
            )

        authoritative_host = self._text((publication_boundary or {}).get("authoritative_host"))
        authoritative_root = self._text((publication_boundary or {}).get("authoritative_root"))
        if publication_boundary and (
            authoritative_host != "192.168.1.120"
            or authoritative_root != "/home/testpilot/mim/runtime/shared"
        ):
            diagnostics.append(
                RuntimeDiagnostic(
                    code="communication_authority_drift",
                    severity="high",
                    summary="Communication authority drifted away from the canonical MIM shared root.",
                    detail=(
                        "The latest publication-boundary artifact does not point at the canonical MIM/TOD communication root "
                        "192.168.1.120:/home/testpilot/mim/runtime/shared. "
                        f"authoritative_host={authoritative_host or 'missing'}, authoritative_root={authoritative_root or 'missing'}, "
                        f"source_service={task_source_service or 'unknown'}, task_objective={task_objective or 'unknown'}."
                    ),
                    metadata={
                        "source_service": task_source_service,
                        "task_objective": task_objective,
                        "authoritative_host": authoritative_host,
                        "authoritative_root": authoritative_root,
                    },
                )
            )

        request_id = self._text((coordination_request or {}).get("request_id") or (coordination_request or {}).get("task_id"))
        request_status = self._text((coordination_request or {}).get("status")).lower()
        ack_request_id = self._text((coordination_ack or {}).get("request_id") or (coordination_ack or {}).get("task_id"))
        ack_status = self._text(
            (coordination_ack or {}).get("ack_status")
            or ((coordination_ack or {}).get("coordination") or {}).get("status")
        ).lower()
        ack_age = self._age_seconds_from_payload(coordination_ack, ["generated_at", "emitted_at"])
        if request_id:
            request_newer_than_ack = self._payload_is_newer(coordination_request, coordination_ack)
            ack_misaligned = ack_request_id != request_id or (request_status and request_status != ack_status)
            ack_stale = ack_age is None or ack_age > self.runtime_ack_stale_after_seconds
            if request_newer_than_ack and (ack_misaligned or ack_stale):
                diagnostics.append(
                    RuntimeDiagnostic(
                        code="coordination_ack_stale_or_misaligned",
                        severity="medium",
                        summary="Coordination ACK is stale relative to the latest coordination request.",
                        detail=(
                            "The coordination request has advanced beyond the currently published ACK. "
                            f"request_id={request_id}, request_status={request_status or 'unknown'}, "
                            f"ack_request_id={ack_request_id or 'missing'}, ack_status={ack_status or 'missing'}, "
                            f"ack_age_seconds={ack_age if ack_age is not None else 'unknown'}."
                        ),
                        metadata={
                            "request_id": request_id,
                            "request_status": request_status,
                            "ack_request_id": ack_request_id,
                            "ack_status": ack_status,
                            "ack_age_seconds": ack_age,
                        },
                    )
                )

        review_idle = task_review.get("idle") if isinstance((task_review or {}).get("idle"), dict) else {}
        review_task = task_review.get("task") if isinstance((task_review or {}).get("task"), dict) else {}
        selected_action = next_action.get("selected_action") if isinstance((next_action or {}).get("selected_action"), dict) else {}
        direct_execution_ready = bool(review_idle.get("direct_execution_ready") is True)
        selected_action_code = self._text(selected_action.get("code")).lower()
        active_task_id = self._text(review_task.get("active_task_id"))
        fallback_task_id = self._text((fallback_activation or {}).get("task_id"))
        fallback_state = self._text((fallback_activation or {}).get("execution_state")).lower()
        fallback_active = bool(
            fallback_task_id
            and active_task_id
            and fallback_task_id == active_task_id
            and fallback_state in {"accepted", "running", "completed"}
        )
        if direct_execution_ready and selected_action_code == "fallback_to_codex_direct_execution" and not fallback_active:
            latest_progress_age = review_idle.get("latest_progress_age_seconds")
            diagnostics.append(
                RuntimeDiagnostic(
                    code="tod_direct_execution_takeover_ready",
                    severity="high",
                    summary="TOD silence exceeded the direct-execution threshold; MIM should take over.",
                    detail=(
                        f"Task-status review marked {active_task_id or 'the active task'} as direct-execution ready after "
                        f"{latest_progress_age if latest_progress_age is not None else 'an extended'} seconds of TOD silence. "
                        "Claim bounded fallback authority and continue the task locally instead of waiting for TOD confirmation."
                    ),
                    metadata={
                        "active_task_id": active_task_id,
                        "selected_action_code": selected_action_code,
                        "latest_progress_age_seconds": latest_progress_age,
                    },
                )
            )

        return diagnostics

    def _watcher_process_diagnostics(self) -> list[RuntimeDiagnostic]:
        duplicates: dict[str, int] = {}
        for label, pattern in self.runtime_process_patterns.items():
            count = self._process_count(pattern)
            if count > self.runtime_duplicate_process_threshold:
                duplicates[label] = count

        if not duplicates:
            return []

        return [
            RuntimeDiagnostic(
                code="duplicate_bridge_watchers",
                severity="medium",
                summary="Duplicate bridge watcher processes detected.",
                detail=(
                    "Multiple bridge watcher processes are active for the same script, which can create duplicate pings, "
                    "stale-state churn, or misleading freeze signals. "
                    + ", ".join(f"{name}={count}" for name, count in sorted(duplicates.items()))
                ),
                metadata={"duplicates": duplicates},
            )
        ]

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _parse_timestamp(raw: Any) -> datetime.datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)

    def _age_seconds_from_payload(self, payload: dict[str, Any] | None, fields: list[str]) -> int | None:
        if not payload:
            return None
        for field in fields:
            parsed = self._parse_timestamp(payload.get(field))
            if parsed is not None:
                return max(
                    0,
                    int((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()),
                )
        return None

    def _payload_is_newer(self, left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        left_ts = None
        right_ts = None
        if left:
            left_ts = self._parse_timestamp(left.get("resolved_at") or left.get("generated_at") or left.get("emitted_at"))
        if right:
            right_ts = self._parse_timestamp(right.get("generated_at") or right.get("emitted_at"))
        if left_ts is None:
            return False
        if right_ts is None:
            return True
        return left_ts > right_ts

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _normalized_objective(self, payload: dict[str, Any] | None) -> str:
        if not payload:
            return ""
        candidates = [
            payload.get("objective_id"),
            payload.get("objective_active"),
            payload.get("objective_in_flight"),
            payload.get("current_next_objective"),
        ]
        truth = payload.get("truth")
        if isinstance(truth, dict):
            candidates.extend(
                [
                    truth.get("objective_active"),
                    truth.get("current_next_objective"),
                ]
            )
        for candidate in candidates:
            text = self._text(candidate)
            if text:
                if text.startswith("objective-"):
                    return text[len("objective-"):]
                return text
        return ""

    @staticmethod
    def _process_count(pattern: str) -> int:
        try:
            completed = subprocess.run(
                ["pgrep", "-fc", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return 0
        raw = (completed.stdout or "").strip()
        return int(raw) if raw.isdigit() else 0
