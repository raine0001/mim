"""Integration tests for MIM self-awareness and self-optimization.

Demonstrates how to test and validate the self-awareness system.
"""

import json
import signal
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None


def _utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class TestSelfHealthMonitor(unittest.TestCase):
    """Test MIM's self-health monitoring capabilities."""

    def test_health_metric_recording(self):
        """Test recording health metrics."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor

        monitor = SelfHealthMonitor()

        # Record a metric
        metric = HealthMetric(
            timestamp=_utc_now_iso(),
            memory_percent=45.2,
            cpu_percent=28.5,
            api_latency_ms=125.3,
            cache_hit_rate=0.72,
        )

        monitor.record_metric(metric)
        self.assertEqual(len(monitor.metrics_history), 1)

    def test_trend_analysis_stable(self):
        """Test trend analysis for stable metrics."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor

        monitor = SelfHealthMonitor()

        # Record stable metrics
        for i in range(5):
            metric = HealthMetric(
                timestamp=_utc_now_iso(),
                memory_percent=50.0,  # stable
            )
            monitor.record_metric(metric)

        trend = monitor.get_trends("memory_percent")
        self.assertIsNotNone(trend)
        self.assertEqual(trend.trend, "stable")

    def test_degradation_detection_memory(self):
        """Test detection of memory degradation."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor

        monitor = SelfHealthMonitor()

        # Record high memory usage
        metric = HealthMetric(
            timestamp=_utc_now_iso(),
            memory_percent=85.0,  # Above threshold
        )
        monitor.record_metric(metric)

        trend = monitor.get_trends("memory_percent")
        self.assertTrue(trend.degradation_detected)

    def test_recommendation_generation(self):
        """Test that recommendations are generated for degradation."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor

        monitor = SelfHealthMonitor()

        # Record degraded metrics
        for i in range(5):
            metric = HealthMetric(
                timestamp=_utc_now_iso(),
                memory_percent=85.0 + i,  # Increasing and high
                api_latency_ms=150.0 + (i * 25),  # Increasing
            )
            monitor.record_metric(metric)

        recommendations = monitor.analyze_and_recommend()
        self.assertGreater(len(recommendations), 0)

    def test_health_summary_status_calculation(self):
        """Test overall health status is correctly calculated."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor

        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = SelfHealthMonitor(state_dir=Path(tmpdir))
            monitor.runtime_process_patterns = {}

            (Path(tmpdir) / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {
                        "exported_at": "2026-03-30T23:19:33Z",
                        "objective_active": "97",
                        "current_next_objective": "97",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (Path(tmpdir) / "MIM_TOD_HANDSHAKE_PACKET.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-30T23:19:33Z",
                        "truth": {"objective_active": "97", "current_next_objective": "97"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            # Record healthy metrics
            metric = HealthMetric(
                timestamp=_utc_now_iso(),
                memory_percent=45.0,
                api_latency_ms=100.0,
                api_error_rate=0.005,
            )
            monitor.record_metric(metric)

            summary = monitor.get_health_summary()
            self.assertEqual(summary["status"], "healthy")

    def test_runtime_diagnostics_detect_stale_export_mismatch(self):
        """Test stale shared export detection against newer live task objective."""
        from core.self_health_monitor import SelfHealthMonitor

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            monitor = SelfHealthMonitor(state_dir=state_dir)

            (state_dir / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {
                        "exported_at": "2026-03-15T18:27:10Z",
                        "objective_active": "74",
                        "current_next_objective": "75",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-15T18:27:10Z",
                        "truth": {"objective_active": "75"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-30T23:19:33Z",
                        "objective_id": "objective-97",
                        "task_id": "objective-97-task-3422",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            summary = monitor.get_health_summary()
            runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
            recommendation_actions = {item["proposed_action"] for item in summary["recommendations"]}
            recovery_recommendation = next(
                item for item in summary["recommendations"] if item["proposed_action"] == "recover_bridge_coordination"
            )

            self.assertIn("shared_export_stale_or_misaligned", runtime_codes)
            self.assertIn(summary["status"], {"degraded", "critical"})
            self.assertIn("refresh_shared_export_artifacts", recommendation_actions)
            self.assertIn("recover_bridge_coordination", recommendation_actions)
            self.assertFalse(recovery_recommendation["requires_approval"])

    def test_runtime_diagnostics_recommend_bridge_coordination_recovery_for_stale_ack(self):
        """Test stale coordination ACKs trigger no-approval bridge recovery."""
        from core.self_health_monitor import SelfHealthMonitor

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            monitor = SelfHealthMonitor(state_dir=state_dir)
            monitor.runtime_process_patterns = {}

            (state_dir / "TOD_MIM_COORDINATION_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T14:49:00Z",
                        "request_id": "coordination-objective-2900-task-008-publication_surface_divergence",
                        "task_id": "coordination-objective-2900-task-008-publication_surface_divergence",
                        "status": "pending",
                        "issue_code": "publication_surface_divergence",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "MIM_TOD_COORDINATION_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-02T14:48:00Z",
                        "request_id": "coordination-objective-2899-task-008-publication_surface_divergence",
                        "ack_status": "pending",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            summary = monitor.get_health_summary()

        runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
        recovery_recommendation = next(
            item for item in summary["recommendations"] if item["proposed_action"] == "recover_bridge_coordination"
        )

        self.assertIn("coordination_ack_stale_or_misaligned", runtime_codes)
        self.assertFalse(recovery_recommendation["requires_approval"])

    def test_runtime_diagnostics_recommend_direct_execution_takeover_after_tod_silence(self):
        """Test task-status direct execution readiness becomes a no-approval takeover recommendation."""
        from core.self_health_monitor import SelfHealthMonitor

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            monitor = SelfHealthMonitor(state_dir=state_dir)
            monitor.runtime_process_patterns = {}

            (state_dir / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "task": {
                            "active_task_id": "objective-2900-task-7117",
                            "objective_id": "objective-2900",
                        },
                        "idle": {
                            "direct_execution_ready": True,
                            "latest_progress_age_seconds": 181,
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (state_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "selected_action": {
                            "code": "fallback_to_codex_direct_execution",
                            "detail": "Stop waiting on TOD and continue locally.",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            summary = monitor.get_health_summary()

        runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
        takeover_recommendation = next(
            item for item in summary["recommendations"] if item["proposed_action"] == "fallback_to_codex_direct_execution"
        )

        self.assertIn("tod_direct_execution_takeover_ready", runtime_codes)
        self.assertFalse(takeover_recommendation["requires_approval"])

    def test_runtime_diagnostics_detect_duplicate_watchers(self):
        """Test duplicate bridge watcher detection from process scan."""
        from core.self_health_monitor import SelfHealthMonitor

        monitor = SelfHealthMonitor()

        def fake_run(cmd, capture_output, text, check):
            pattern = cmd[-1]
            count = "2\n" if pattern == "watch_tod_liveness.sh" else "1\n"

            class Result:
                stdout = count

            return Result()

        with patch("core.self_health_monitor.subprocess.run", side_effect=fake_run):
            summary = monitor.get_health_summary()

        runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
        recommendation_actions = {item["proposed_action"] for item in summary["recommendations"]}

        self.assertIn("duplicate_bridge_watchers", runtime_codes)
        self.assertIn("deduplicate_bridge_watchers", recommendation_actions)
        self.assertIn(summary["status"], {"suboptimal", "degraded", "critical"})

    def test_runtime_diagnostics_detect_unstable_lane_recovery(self):
        """Test runtime recovery instability diagnostics and recommendations."""
        from core.self_health_monitor import SelfHealthMonitor
        from core.runtime_recovery_service import RuntimeRecoveryService

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            monitor = SelfHealthMonitor(state_dir=state_dir)
            monitor.runtime_process_patterns = {}
            recovery = RuntimeRecoveryService(state_dir=state_dir)

            for index in range(3):
                recovery.record_event(
                    lane="microphone",
                    event_type="stale_detected",
                    detail=f"Microphone stale #{index}",
                )
                recovery.record_event(
                    lane="microphone",
                    event_type="recovery_attempted",
                    detail=f"Recovery attempt #{index}",
                )
            recovery.record_event(
                lane="microphone",
                event_type="recovery_failed",
                detail="Browser recognition failed to restart.",
            )
            recovery.record_event(
                lane="microphone",
                event_type="recovery_failed",
                detail="Short-run flap guard engaged.",
            )

            summary = monitor.get_health_summary()
            runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
            recommendation_actions = {item["proposed_action"] for item in summary["recommendations"]}

            self.assertIn("microphone_lane_recovery_instability", runtime_codes)
            self.assertIn("inspect_runtime_devices_and_browser", recommendation_actions)
            self.assertIn(summary["status"], {"degraded", "critical"})

    def test_runtime_recovery_summary_tracks_camera_retry_evidence(self):
        """Test camera runtime recovery summaries preserve retry reasons and healthy frame timestamps."""
        from core.runtime_recovery_service import RuntimeRecoveryService

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            recovery = RuntimeRecoveryService(state_dir=state_dir)

            recovery.record_event(
                lane="camera",
                event_type="recovery_attempted",
                detail="Client restarted camera watcher after backend stale signal.",
                metadata={
                    "retry_reason": "stale_frames",
                    "retry_reason_detail": "Frames continued locally but none kept the backend lane healthy.",
                    "watcher_running": True,
                    "last_frame_seen_at": "2026-03-31T00:38:44Z",
                    "last_healthy_frame_at": "2026-03-31T00:38:40Z",
                },
            )
            recovery.record_event(
                lane="camera",
                event_type="healthy_observed",
                detail="Camera lane returned healthy after backend stale signal.",
                metadata={
                    "first_healthy_at": "2026-03-31T00:38:53Z",
                    "last_healthy_frame_at": "2026-03-31T00:38:53Z",
                    "watcher_running": True,
                },
            )
            recovery.record_event(
                lane="camera",
                event_type="recovery_attempted",
                detail="Client restarted camera watcher after backend stale signal.",
                metadata={
                    "retry_reason": "stale_frames",
                    "retry_reason_detail": "Frames continued locally but none kept the backend lane healthy.",
                    "watcher_running": True,
                    "last_frame_seen_at": "2026-03-31T00:39:00Z",
                    "last_healthy_frame_at": "2026-03-31T00:38:53Z",
                },
            )

            summary = recovery.get_summary()
            camera = summary["lanes"]["camera"]

        self.assertEqual(camera["retry_reason"], "stale_frames")
        self.assertEqual(camera["retry_reason_after_cooldown"], "stale_frames")
        self.assertEqual(camera["first_healthy_at"], "2026-03-31T00:38:53Z")
        self.assertEqual(camera["last_healthy_frame_at"], "2026-03-31T00:38:53Z")
        self.assertTrue(camera["bounded_retry_evidence"])
        self.assertTrue(camera["watcher_running"])

    def test_runtime_diagnostics_surface_bounded_camera_retry_and_recovered_microphone(self):
        """Test self-awareness emits bounded camera retry evidence and recovered microphone evidence."""
        from core.self_health_monitor import SelfHealthMonitor
        from core.runtime_recovery_service import RuntimeRecoveryService

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            monitor = SelfHealthMonitor(state_dir=state_dir)
            monitor.runtime_process_patterns = {}
            recovery = RuntimeRecoveryService(state_dir=state_dir)

            recovery.record_event(
                lane="camera",
                event_type="stale_detected",
                detail="Camera stale #1",
            )
            recovery.record_event(
                lane="camera",
                event_type="recovery_attempted",
                detail="Camera attempt #1",
                metadata={
                    "retry_reason": "stale_frames",
                    "last_healthy_frame_at": "2026-03-31T00:38:40Z",
                    "watcher_running": True,
                },
            )
            recovery.record_event(
                lane="camera",
                event_type="recovery_succeeded",
                detail="Camera watcher restarted successfully.",
            )
            recovery.record_event(
                lane="camera",
                event_type="healthy_observed",
                detail="Camera healthy #1",
                metadata={
                    "first_healthy_at": "2026-03-31T00:38:53Z",
                    "last_healthy_frame_at": "2026-03-31T00:38:53Z",
                    "watcher_running": True,
                },
            )
            recovery.record_event(
                lane="camera",
                event_type="stale_detected",
                detail="Camera stale #2",
            )
            recovery.record_event(
                lane="camera",
                event_type="recovery_attempted",
                detail="Camera attempt #2",
                metadata={
                    "retry_reason": "stale_frames",
                    "last_healthy_frame_at": "2026-03-31T00:38:53Z",
                    "watcher_running": True,
                },
            )
            recovery.record_event(
                lane="microphone",
                event_type="stale_detected",
                detail="Microphone stale #1",
            )
            recovery.record_event(
                lane="microphone",
                event_type="recovery_attempted",
                detail="Microphone attempt #1",
            )
            recovery.record_event(
                lane="microphone",
                event_type="recovery_succeeded",
                detail="Microphone healthy again.",
                metadata={"first_healthy_at": "2026-03-31T00:38:53Z"},
            )

            summary = monitor.get_health_summary()
            runtime_codes = {item["code"] for item in summary["runtime_diagnostics"]}
            recommendation_actions = {item["proposed_action"] for item in summary["recommendations"]}

        self.assertIn("camera_lane_recovery_instability", runtime_codes)
        self.assertIn("camera_lane_bounded_retry_evidence", runtime_codes)
        self.assertIn("microphone_lane_recently_recovered", runtime_codes)
        self.assertIn("inspect_runtime_devices_and_browser", recommendation_actions)
        self.assertIn("monitor_runtime_recovery_evidence", recommendation_actions)


class TestSelfOptimizerService(unittest.TestCase):
    """Test MIM's self-optimization proposal lifecycle."""

    def test_proposal_creation(self):
        """Test creating an optimization proposal."""
        from core.self_optimizer_service import SelfOptimizerService
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            proposal = service.propose_optimization(
                recommendation_id="opt-test",
                title="Test Optimization",
                description="A test optimization for unit testing",
                proposed_action="test_action",
                requires_approval=True,
                severity="medium",
                estimated_impact_percent=25,
            )

            self.assertEqual(proposal.title, "Test Optimization")
            self.assertEqual(proposal.status.value, "proposed")

    def test_proposal_approval_workflow(self):
        """Test approval workflow."""
        from core.self_optimizer_service import SelfOptimizerService, OptimizationStatus
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            # Create proposal
            proposal = service.propose_optimization(
                recommendation_id="opt-test",
                title="Test Optimization",
                description="Test",
                proposed_action="test_action",
            )

            # Approve it
            proposal = service.approve_proposal(proposal.proposal_id, "Test approval")
            self.assertEqual(proposal.status, OptimizationStatus.APPROVED)
            self.assertIsNotNone(proposal.approved_at)

    def test_proposal_execution(self):
        """Test executing an approved optimization."""
        from core.self_optimizer_service import SelfOptimizerService, OptimizationStatus
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            # Create and approve proposal
            proposal = service.propose_optimization(
                recommendation_id="opt-test",
                title="GC Test",
                description="Test GC",
                proposed_action="trigger_garbage_collection",
                requires_approval=False,
            )

            # Execute (no approval needed for this action)
            result = service.execute_proposal(proposal.proposal_id)
            self.assertEqual(result.status, OptimizationStatus.COMPLETED)
            self.assertIsNotNone(result.execution_result)

    def test_proposal_rejection(self):
        """Test rejecting a proposal."""
        from core.self_optimizer_service import SelfOptimizerService, OptimizationStatus
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            proposal = service.propose_optimization(
                recommendation_id="opt-test",
                title="Test",
                description="Test",
                proposed_action="test",
            )

            # Reject it
            rejected = service.reject_proposal(proposal.proposal_id, "Not needed")
            self.assertEqual(rejected.status, OptimizationStatus.REJECTED)

    def test_proposal_rollback(self):
        """Test rolling back a completed optimization."""
        from core.self_optimizer_service import SelfOptimizerService, OptimizationStatus
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            # Create and execute
            proposal = service.propose_optimization(
                recommendation_id="opt-test",
                title="Scale Workers",
                description="Test scaling",
                proposed_action="increase_worker_pool_size",
                rollback_action="decrease_worker_pool_size",
                requires_approval=False,
            )
            service.execute_proposal(proposal.proposal_id)

            # Rollback
            result = service.rollback_proposal(proposal.proposal_id)
            self.assertEqual(result.status, OptimizationStatus.ROLLED_BACK)

    def test_proposal_listing_and_filtering(self):
        """Test listing proposals with status filtering."""
        from core.self_optimizer_service import SelfOptimizerService, OptimizationStatus
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            # Create multiple proposals
            p1 = service.propose_optimization(
                recommendation_id="opt-1",
                title="Test 1",
                description="Test",
                proposed_action="test",
            )
            service.propose_optimization(
                recommendation_id="opt-2",
                title="Test 2",
                description="Test",
                proposed_action="test",
            )

            # Approve one
            service.approve_proposal(p1.proposal_id)

            # List by status
            proposed = service.list_proposals(status=OptimizationStatus.PROPOSED)
            approved = service.list_proposals(status=OptimizationStatus.APPROVED)

            self.assertEqual(len(approved), 1)
            self.assertEqual(len(proposed), 1)

    def test_refresh_shared_export_action_executes_exporter(self):
        """Test exporter refresh action dispatches the export script."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir) / "runtime" / "shared")

            class Result:
                returncode = 0
                stdout = json.dumps(
                    {
                        "objective_active": "97",
                        "schema_version": "2026-03-30-97",
                        "release_tag": "objective-97",
                    }
                )
                stderr = ""

            with patch("core.self_optimizer_service.Path.exists", return_value=True), patch(
                "core.self_optimizer_service.subprocess.run",
                return_value=Result(),
            ):
                result = service._execute_action("refresh_shared_export_artifacts")

        self.assertEqual(result["action"], "refresh_shared_export_artifacts")
        self.assertEqual(result["objective_active"], "97")

    def test_runtime_device_inspection_action_returns_manual_followup(self):
        """Test manual runtime/device inspection action dispatch."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))
            result = service._execute_action("inspect_runtime_devices_and_browser")

        self.assertEqual(result["action"], "inspect_runtime_devices_and_browser")
        self.assertEqual(result["status"], "manual_check_recommended")

    def test_runtime_recovery_monitor_action_returns_observation_followup(self):
        """Test informational runtime recovery evidence action dispatch."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))
            result = service._execute_action("monitor_runtime_recovery_evidence")

        self.assertEqual(result["action"], "monitor_runtime_recovery_evidence")
        self.assertEqual(result["status"], "observation_recommended")

    def test_deduplicate_bridge_watchers_disables_overlapping_system_unit_when_enabled(self):
        """Test duplicate TOD watcher cleanup can disable the overlapping system unit through the bounded bridge."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir))

            def _pgrep_side_effect(args, **kwargs):
                pattern = args[-1]

                class Result:
                    returncode = 0
                    stderr = ""

                result = Result()
                if pattern == "watch_tod_liveness.sh":
                    result.stdout = "101 bash /home/testpilot/mim/scripts/watch_tod_liveness.sh\n202 bash /home/testpilot/mim/scripts/watch_tod_liveness.sh\n"
                else:
                    result.stdout = ""
                return result

            with patch("core.self_optimizer_service.subprocess.run", side_effect=_pgrep_side_effect), patch(
                "core.self_optimizer_service.os.kill"
            ) as kill_mock, patch(
                "core.self_optimizer_service.run_privileged_action",
                return_value={"action": "disable-system-tod-liveness-watcher", "status": "completed"},
            ) as privileged_mock:
                result = service._execute_action("deduplicate_bridge_watchers")

        privileged_mock.assert_called_once_with("disable-system-tod-liveness-watcher")
        kill_mock.assert_called_once_with(202, signal.SIGTERM)
        self.assertEqual(result["kept"]["watch_tod_liveness.sh"], 101)
        self.assertEqual(result["privileged_coordination"]["status"], "completed")

    def test_restart_bridge_watchers_prefers_user_managed_units(self):
        """Test bridge watcher restart uses user systemd units instead of manual duplicate spawns when available."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_dir = Path(tmpdir) / "runtime" / "shared"
            scripts_dir = Path(tmpdir) / "scripts"
            shared_dir.mkdir(parents=True, exist_ok=True)
            scripts_dir.mkdir(parents=True, exist_ok=True)
            for name in ("watch_shared_triggers.sh", "watch_mim_coordination_responder.sh"):
                (scripts_dir / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            service = SelfOptimizerService(shared_dir)

            def _run_side_effect(args, **kwargs):
                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            with patch("core.self_optimizer_service.shutil.which", return_value="/bin/systemctl"), patch(
                "core.self_optimizer_service.subprocess.run",
                side_effect=_run_side_effect,
            ) as run_mock, patch("core.self_optimizer_service.subprocess.Popen") as popen_mock:
                result = service._restart_bridge_watchers()

        self.assertIn("mim-watch-shared-triggers.service", result["restarted"])
        self.assertIn("mim-watch-mim-coordination-responder.service", result["restarted"])
        popen_mock.assert_not_called()
        restart_calls = [call.args[0] for call in run_mock.call_args_list if call.args and call.args[0][:3] == ["/bin/systemctl", "--user", "restart"]]
        self.assertIn(["/bin/systemctl", "--user", "restart", "mim-watch-shared-triggers.service"], restart_calls)
        self.assertIn(["/bin/systemctl", "--user", "restart", "mim-watch-mim-coordination-responder.service"], restart_calls)


class TestSelfAwarenessRouter(unittest.IsolatedAsyncioTestCase):
    """Focused async router regressions for self-optimization execution."""

    async def test_execute_optimization_awaits_async_service(self):
        """Test execute endpoint uses the optimizer's async execution path."""
        from core.routers import self_awareness_router
        from core.self_optimizer_service import OptimizationProposal, OptimizationStatus

        proposal = OptimizationProposal(
            proposal_id="opt-1",
            recommendation_id="rec-1",
            title="Recover bridge",
            description="Recover bridge coordination",
            proposed_action="recover_bridge_coordination",
            rollback_action=None,
            requires_approval=False,
            severity="high",
            estimated_impact_percent=35,
            status=OptimizationStatus.COMPLETED,
        )

        with patch.object(
            self_awareness_router.optimizer_service,
            "execute_proposal_async",
            AsyncMock(return_value=proposal),
        ) as execute_mock:
            response = await self_awareness_router.execute_optimization("opt-1")

        self.assertEqual(response.status, "completed")
        execute_mock.assert_awaited_once_with("opt-1")


class TestSelfOptimizerServiceContinuation(unittest.TestCase):
    """Additional optimizer action regressions."""

    def test_authoritative_stale_guard_request_is_never_promoted(self):
        """Stale-guard metadata is diagnostic only and cannot become authoritative request lineage."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_dir = Path(tmpdir) / "runtime" / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            service = SelfOptimizerService(shared_dir)

            (shared_dir / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {
                        "source_of_truth": {
                            "terminal_request_review": {
                                "reason": "stale_guard_higher_authoritative_request"
                            }
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "stale_guard": {
                            "detected": True,
                            "reason": "higher_authoritative_task_ordinal_active",
                            "high_watermark": {
                                "request_id": "objective-2912-task-7141-implement-bounded-work"
                            },
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = service._authoritative_stale_guard_request()

        self.assertIsNone(result)

    def test_authoritative_stale_guard_request_is_none_without_terminal_review(self):
        """Stale-guard remains non-authoritative even when export review metadata is absent."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_dir = Path(tmpdir) / "runtime" / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            service = SelfOptimizerService(shared_dir)

            (shared_dir / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "stale_guard": {
                            "detected": True,
                            "reason": "higher_authoritative_task_ordinal_active",
                            "current_request": {
                                "request_id": "objective-2912-task-008",
                            },
                            "high_watermark": {
                                "request_id": "objective-2912-task-7141-implement-bounded-work"
                            },
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            result = service._authoritative_stale_guard_request()

        self.assertIsNone(result)

    def test_recover_bridge_coordination_action_repairs_and_verifies(self):
        """Test bridge coordination recovery composes republish, refresh, verification, and watcher restart."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = SelfOptimizerService(Path(tmpdir) / "runtime" / "shared")

            with patch.object(
                service,
                "_republish_active_task_request_surface_async",
                AsyncMock(return_value={"objective_id": 2900, "task_id": 7117, "request_id": "objective-2900-task-7117"}),
            ), patch.object(
                service,
                "_refresh_publication_boundary",
                return_value={"action": "refresh_publication_boundary", "status": "completed", "request_request_id": "objective-2900-task-7117"},
            ), patch.object(
                service,
                "_action_refresh_shared_export_artifacts",
                return_value={"action": "refresh_shared_export_artifacts", "status": "completed", "objective_active": "2900"},
            ), patch.object(
                service,
                "_run_coordination_responder_once",
                return_value={"action": "run_coordination_responder_once", "status": "completed"},
            ), patch.object(
                service,
                "_restart_bridge_watchers",
                return_value={"action": "restart_bridge_watchers", "status": "completed", "restarted": ["watch_shared_triggers.sh"]},
            ), patch("core.self_health_monitor.SelfHealthMonitor") as monitor_cls:
                monitor_cls.return_value.get_runtime_diagnostics.return_value = []
                result = service._execute_action("recover_bridge_coordination")

        self.assertEqual(result["action"], "recover_bridge_coordination")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["republish"]["objective_id"], 2900)

    def test_direct_execution_takeover_action_claims_fallback_and_dispatches(self):
        """Test TOD silence takeover writes fallback activation and dispatches the active task."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_dir = Path(tmpdir) / "runtime" / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            service = SelfOptimizerService(shared_dir)

            (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "task": {
                            "active_task_id": "objective-2900-task-7117",
                            "objective_id": "objective-2900",
                        },
                        "idle": {"direct_execution_ready": True},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "selected_action": {
                            "code": "fallback_to_codex_direct_execution",
                            "detail": "Stop waiting on TOD and continue locally.",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                service,
                "_dispatch_active_task_to_codex_direct_execution_async",
                AsyncMock(return_value={
                    "objective_id": "2900",
                    "task_id": "7117",
                    "request_id": "handoff-2900-7117",
                    "dispatch_status": "running",
                    "submission": {"status": "running"},
                }),
            ):
                result = service._execute_action("fallback_to_codex_direct_execution")

            fallback_activation = json.loads(
                (shared_dir / "MIM_TOD_FALLBACK_ACTIVATION.latest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["action"], "fallback_to_codex_direct_execution")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(fallback_activation["decision_outcome"], "mim_direct_execution_takeover")
        self.assertEqual(fallback_activation["execution_state"], "running")

    def test_direct_execution_takeover_refuses_same_task_bridge_evidence(self):
        """Same-task TOD bridge evidence should block direct-execution takeover."""
        from core.self_optimizer_service import SelfOptimizerService

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_dir = Path(tmpdir) / "runtime" / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            service = SelfOptimizerService(shared_dir)

            (shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                json.dumps(
                    {
                        "task": {
                            "active_task_id": "objective-2900-task-7117",
                            "objective_id": "objective-2900",
                        },
                        "idle": {"direct_execution_ready": True},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json").write_text(
                json.dumps(
                    {
                        "selected_action": {
                            "code": "fallback_to_codex_direct_execution",
                            "detail": "Stop waiting on TOD and continue locally.",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_COMMAND_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "bridge_runtime": {
                            "current_processing": {
                                "task_id": "objective-2900-task-7117",
                            }
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "same-task bridge evidence"):
                service._execute_action("fallback_to_codex_direct_execution")


class TestSelfAwarenessAPI(unittest.IsolatedAsyncioTestCase):
    """Test self-awareness API endpoints."""

    async def test_health_endpoint_returns_summary(self):
        """Test GET /mim/self/health returns health summary."""
        # This would be run against a test server
        # Example using httpx:
        # async with httpx.AsyncClient() as client:
        #     response = await client.get("http://127.0.0.1:18001/mim/self/health")
        #     self.assertEqual(response.status_code, 200)
        #     data = response.json()
        #     self.assertIn("status", data)
        #     self.assertIn("uptime_seconds", data)
        pass

    async def test_record_metric_endpoint(self):
        """Test POST /mim/self/health/record-metric works."""
        # async with httpx.AsyncClient() as client:
        #     response = await client.post(
        #         "http://127.0.0.1:18001/mim/self/health/record-metric",
        #         json={
        #             "memory_percent": 45.2,
        #             "cpu_percent": 28.5,
        #         }
        #     )
        #     self.assertEqual(response.status_code, 200)
        pass

    async def test_propose_optimization_endpoint(self):
        """Test POST /mim/self/optimize/propose creates proposal."""
        # async with httpx.AsyncClient() as client:
        #     response = await client.post(
        #         "http://127.0.0.1:18001/mim/self/optimize/propose",
        #         json={
        #             "recommendation_id": "test-1",
        #             "title": "Test Optimization",
        #             "description": "Test",
        #             "proposed_action": "trigger_garbage_collection",
        #             "severity": "medium",
        #         }
        #     )
        #     self.assertEqual(response.status_code, 200)
        #     data = response.json()
        #     self.assertIn("proposal_id", data)
        pass


if __name__ == "__main__":
    unittest.main()
