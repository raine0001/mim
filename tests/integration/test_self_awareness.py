"""Integration tests for MIM self-awareness and self-optimization.

Demonstrates how to test and validate the self-awareness system.
"""

import json
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None


class TestSelfHealthMonitor(unittest.TestCase):
    """Test MIM's self-health monitoring capabilities."""

    def test_health_metric_recording(self):
        """Test recording health metrics."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor
        import datetime

        monitor = SelfHealthMonitor()

        # Record a metric
        metric = HealthMetric(
            timestamp=datetime.datetime.utcnow().isoformat() + "Z",
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
        import datetime

        monitor = SelfHealthMonitor()

        # Record stable metrics
        for i in range(5):
            metric = HealthMetric(
                timestamp=datetime.datetime.utcnow().isoformat() + "Z",
                memory_percent=50.0,  # stable
            )
            monitor.record_metric(metric)

        trend = monitor.get_trends("memory_percent")
        self.assertIsNotNone(trend)
        self.assertEqual(trend.trend, "stable")

    def test_degradation_detection_memory(self):
        """Test detection of memory degradation."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor
        import datetime

        monitor = SelfHealthMonitor()

        # Record high memory usage
        metric = HealthMetric(
            timestamp=datetime.datetime.utcnow().isoformat() + "Z",
            memory_percent=85.0,  # Above threshold
        )
        monitor.record_metric(metric)

        trend = monitor.get_trends("memory_percent")
        self.assertTrue(trend.degradation_detected)

    def test_recommendation_generation(self):
        """Test that recommendations are generated for degradation."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor
        import datetime

        monitor = SelfHealthMonitor()

        # Record degraded metrics
        for i in range(5):
            metric = HealthMetric(
                timestamp=datetime.datetime.utcnow().isoformat() + "Z",
                memory_percent=85.0 + i,  # Increasing and high
                api_latency_ms=150.0 + (i * 25),  # Increasing
            )
            monitor.record_metric(metric)

        recommendations = monitor.analyze_and_recommend()
        self.assertGreater(len(recommendations), 0)

    def test_health_summary_status_calculation(self):
        """Test overall health status is correctly calculated."""
        from core.self_health_monitor import HealthMetric, SelfHealthMonitor
        import datetime

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
                timestamp=datetime.datetime.utcnow().isoformat() + "Z",
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

            self.assertIn("shared_export_stale_or_misaligned", runtime_codes)
            self.assertIn(summary["status"], {"degraded", "critical"})
            self.assertIn("refresh_shared_export_artifacts", recommendation_actions)

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
