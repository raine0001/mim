import json
import os
import urllib.error
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


class Objective80ExecutionTruthAdaptationSurfacesTest(unittest.TestCase):
    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200, payload)

    def _create_stale_observation(self, *, scope: str, run_id: str) -> None:
        self._register_workspace_scan()
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective80 adaptation stale scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scope,
                    "confidence_threshold": 0.6,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        for state in ["accepted", "running"]:
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, payload)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": f"obj80-adaptation-stale-{run_id}",
                            "zone": scope,
                            "confidence": 0.9,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_execution_truth(self, *, scope: str, run_id: str) -> int:
        capability_name = f"execution_truth_adaptation_probe_{run_id}"
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80 adaptation surfaces probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run adaptation execution truth probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for adaptation surfaces",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "runtime mismatch recorded",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
                "execution_truth": {
                    "contract": "execution_truth_v1",
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": 1710,
                    "duration_delta_ratio": 0.9,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.94,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, payload)
        return execution_id

    def test_execution_truth_surfaces_across_autonomy_maintenance_and_stewardship(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective80-adaptation-{run_id}"

        self._create_stale_observation(scope=scope, run_id=run_id)
        execution_id = self._seed_execution_truth(scope=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": "objective80-adaptation-surfaces",
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective80-test",
                "source": "objective80-adaptation-surfaces",
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, goals)

        status, boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective80-test",
                "source": "objective80-adaptation-surfaces",
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {"human_safety": True, "legality": True, "system_integrity": True},
                "evidence_inputs_override": {
                    "success_rate": 0.88,
                    "escalation_rate": 0.08,
                    "retry_rate": 0.1,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.72,
                    "sample_count": 18,
                    "override_rate": 0.02,
                    "replan_rate": 0.08,
                    "environment_stability": 0.76,
                    "development_confidence": 0.78,
                    "constraint_reliability": 0.84,
                    "experiment_confidence": 0.74,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, boundary_payload)
        boundary = boundary_payload.get("boundary", {}) if isinstance(boundary_payload, dict) else {}
        truth_influence = (
            boundary.get("execution_truth_influence", {})
            if isinstance(boundary.get("execution_truth_influence", {}), dict)
            else {}
        )
        self.assertTrue(bool(truth_influence.get("review_only", False)), truth_influence)
        self.assertGreaterEqual(int(truth_influence.get("deviation_signal_count", 0) or 0), 5, truth_influence)
        self.assertEqual(str(truth_influence.get("managed_scope", "")), scope, truth_influence)
        self.assertIn("simulation_reality_mismatch", truth_influence.get("signal_types", []), truth_influence)
        self.assertIn(
            execution_id,
            [
                int(item.get("execution_id", 0) or 0)
                for item in (truth_influence.get("recent_executions", []) if isinstance(truth_influence.get("recent_executions", []), list) else [])
                if isinstance(item, dict)
            ],
            truth_influence,
        )

        status, maintenance_payload = post_json(
            "/maintenance/cycle",
            {
                "actor": "objective80-test",
                "source": "objective80-adaptation-surfaces",
                "stale_after_seconds": 300,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, maintenance_payload)
        maintenance_run = maintenance_payload.get("run", {}) if isinstance(maintenance_payload, dict) else {}
        maintenance_signals = (
            maintenance_run.get("detected_signals", [])
            if isinstance(maintenance_run.get("detected_signals", []), list)
            else []
        )
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(item.get("signal_type", "")) == "environment_shift_during_execution"
                and str(item.get("target_scope", "")) == scope
                for item in maintenance_signals
            ),
            maintenance_signals,
        )
        maintenance_outcomes = (
            maintenance_run.get("maintenance_outcomes", {})
            if isinstance(maintenance_run.get("maintenance_outcomes", {}), dict)
            else {}
        )
        self.assertGreaterEqual(int(maintenance_outcomes.get("execution_truth_signal_count", 0) or 0), 5, maintenance_outcomes)
        self.assertIn("environment_shift_during_execution", maintenance_outcomes.get("execution_truth_signal_types", []), maintenance_outcomes)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective80-test",
                "source": "objective80-adaptation-surfaces",
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"obj80-adaptation-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "adaptation_surfaces"},
            },
        )
        self.assertEqual(status, 200, cycled)
        cycle = cycled.get("cycle", {}) if isinstance(cycled, dict) else {}
        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        selected_actions = cycle.get("selected_actions", []) if isinstance(cycle.get("selected_actions", []), list) else []
        verification = cycle.get("verification", {}) if isinstance(cycle.get("verification", {}), dict) else {}

        self.assertTrue(bool(summary.get("execution_truth_followup_recommended", False)), summary)
        self.assertTrue(bool(verification.get("execution_truth_followup_recommended", False)), verification)
        self.assertGreaterEqual(int(summary.get("execution_truth_signal_count", 0) or 0), 5, summary)
        self.assertIn("simulation_reality_mismatch", summary.get("execution_truth_signal_types", []), summary)
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(item.get("action_type", "")) == "execution_truth_review_recommended"
                and str(item.get("managed_scope", "")) == scope
                for item in selected_actions
            ),
            selected_actions,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)