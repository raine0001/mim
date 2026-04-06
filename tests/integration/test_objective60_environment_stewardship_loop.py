import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)


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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective60EnvironmentStewardshipLoopTest(unittest.TestCase):
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

    def _create_stale_observation(self, *, zone: str, run_id: str) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective60 stale scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {},
                },
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {
                            "label": f"obj60-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ]
                },
            },
        )
        self.assertEqual(status, 200, done)

    def test_objective60_environment_stewardship_loop(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"stewardship-obj60-{run_id}"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": "objective60-focused",
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective60-test",
                "source": "objective60-focused",
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, goals)
        self.assertGreater(int(goals.get("generated", 0) or 0), 0)

        status, boundary = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective60-test",
                "source": "objective60-focused",
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {"human_safety": "bounded_auto"},
                "evidence_inputs_override": {
                    "success_rate": 0.9,
                    "escalation_rate": 0.05,
                    "retry_rate": 0.05,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.7,
                    "sample_count": 20,
                    "manual_override_count": 0,
                    "replan_count": 0,
                    "constraint_high_risk_count": 0,
                    "stability_signal": 0.9,
                    "human_present_rate": 0.0,
                    "active_experiment_count": 0,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, boundary)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective60-test",
                "source": "objective60-focused",
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "force_degraded": False,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.3,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"obj60-critical-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "degraded"},
            },
        )
        self.assertEqual(status, 200, cycled)

        stewardship = cycled.get("stewardship", {}) if isinstance(cycled, dict) else {}
        cycle = cycled.get("cycle", {}) if isinstance(cycled, dict) else {}
        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        stewardship_id = int(stewardship.get("stewardship_id", 0) or 0)
        self.assertGreater(stewardship_id, 0)
        self.assertGreater(int(stewardship.get("linked_desired_state_id", 0) or 0), 0)
        desired_state = (
            stewardship.get("desired_state", {})
            if isinstance(stewardship.get("desired_state", {}), dict)
            else {}
        )
        self.assertEqual(
            int(desired_state.get("desired_state_id", 0) or 0),
            int(stewardship.get("linked_desired_state_id", 0) or 0),
        )
        self.assertEqual(str(desired_state.get("scope", "")), "zone")
        self.assertEqual(str(desired_state.get("scope_ref", "")), scope)
        self.assertTrue(
            str(desired_state.get("created_from", "")).startswith("strategy_goal:")
        )
        self.assertGreaterEqual(
            float(cycle.get("post_health", 0.0) or 0.0),
            float(cycle.get("pre_health", 0.0) or 0.0),
        )
        self.assertGreaterEqual(int(summary.get("degraded_signal_count", 0) or 0), 1)
        self.assertGreaterEqual(int(summary.get("actions_executed", 0) or 0), 1)
        self.assertEqual(
            stewardship.get("target_environment_state", {}).get("key_objects", []),
            [f"obj60-critical-missing-{run_id}"],
        )
        self.assertIn("current_metrics", stewardship)
        self.assertIn("stability_score", stewardship.get("current_metrics", {}))
        integration = (
            cycle.get("integration_evidence", {})
            if isinstance(cycle.get("integration_evidence", {}), dict)
            else {}
        )
        strategy_goal_ids = (
            integration.get("strategy_goal_ids", [])
            if isinstance(integration.get("strategy_goal_ids", []), list)
            else []
        )
        self.assertGreaterEqual(len(strategy_goal_ids), 1)
        self.assertEqual(
            int(integration.get("desired_state_id", 0) or 0),
            int(stewardship.get("linked_desired_state_id", 0) or 0),
        )
        self.assertIn("autonomy_boundary_id", integration)
        self.assertIn("operator_preference_weight", integration)
        self.assertIn("governance", integration)
        assessment = (
            cycle.get("assessment", {})
            if isinstance(cycle.get("assessment", {}), dict)
            else {}
        )
        verification = (
            cycle.get("verification", {})
            if isinstance(cycle.get("verification", {}), dict)
            else {}
        )
        decision = (
            cycle.get("decision", {})
            if isinstance(cycle.get("decision", {}), dict)
            else {}
        )
        pre_assessment = (
            assessment.get("pre", {})
            if isinstance(assessment.get("pre", {}), dict)
            else {}
        )
        post_assessment = (
            assessment.get("post", {})
            if isinstance(assessment.get("post", {}), dict)
            else {}
        )
        pre_system = (
            pre_assessment.get("system_metrics", {})
            if isinstance(pre_assessment.get("system_metrics", {}), dict)
            else {}
        )
        post_system = (
            post_assessment.get("system_metrics", {})
            if isinstance(post_assessment.get("system_metrics", {}), dict)
            else {}
        )
        self.assertIn("stability_score", pre_system)
        self.assertIn("uncertainty_score", pre_system)
        self.assertIn("drift_rate", pre_system)
        self.assertIn("stability_score", post_system)
        self.assertIn("remaining_deviation_count", verification)
        self.assertIn("throttle_state", verification)
        self.assertIn("throttle_state", decision)
        self.assertEqual(
            int(decision.get("desired_state_id", 0) or 0),
            int(stewardship.get("linked_desired_state_id", 0) or 0),
        )
        key_objects = (
            post_assessment.get("scope_metrics", {}).get("key_objects", [])
            if isinstance(post_assessment.get("scope_metrics", {}), dict)
            else []
        )
        self.assertTrue(
            any(
                str(item.get("object_name", "")) == f"obj60-critical-missing-{run_id}"
                for item in key_objects
                if isinstance(item, dict)
            )
        )

        status, cycles = get_json(
            "/stewardship/cycle",
            {"stewardship_id": stewardship_id, "managed_scope": scope, "limit": 20},
        )
        self.assertEqual(status, 200, cycles)
        cycle_rows = cycles.get("cycles", []) if isinstance(cycles, dict) else []
        self.assertTrue(
            any(
                int(item.get("cycle_id", 0) or 0) == int(cycle.get("cycle_id", 0) or 0)
                for item in cycle_rows
                if isinstance(item, dict)
            )
        )

        status, listed = get_json("/stewardship", {"managed_scope": scope, "limit": 20})
        self.assertEqual(status, 200, listed)
        rows = listed.get("stewardship", []) if isinstance(listed, dict) else []
        self.assertTrue(
            any(
                int(item.get("stewardship_id", 0) or 0) == stewardship_id
                for item in rows
                if isinstance(item, dict)
            )
        )

        status, detail = get_json(f"/stewardship/{stewardship_id}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(
            int(detail.get("stewardship", {}).get("stewardship_id", 0) or 0),
            stewardship_id,
        )

        status, history = get_json(
            "/stewardship/history", {"stewardship_id": stewardship_id, "limit": 20}
        )
        self.assertEqual(status, 200, history)
        history_rows = history.get("history", []) if isinstance(history, dict) else []
        self.assertTrue(
            any(
                int(item.get("stewardship_id", 0) or 0) == stewardship_id
                for item in history_rows
                if isinstance(item, dict)
            )
        )

        status, throttled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective60-test",
                "source": "objective60-focused",
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.3,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"obj60-critical-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "throttled"},
            },
        )
        self.assertEqual(status, 200, throttled)
        throttled_summary = (
            throttled.get("summary", {}) if isinstance(throttled, dict) else {}
        )
        throttled_cycle = (
            throttled.get("cycle", {})
            if isinstance(throttled.get("cycle", {}), dict)
            else {}
        )
        throttled_decision = (
            throttled_cycle.get("decision", {})
            if isinstance(throttled_cycle.get("decision", {}), dict)
            else {}
        )
        throttled_verification = (
            throttled_cycle.get("verification", {})
            if isinstance(throttled_cycle.get("verification", {}), dict)
            else {}
        )
        throttle_state = (
            throttled_decision.get("throttle_state", {})
            if isinstance(throttled_decision.get("throttle_state", {}), dict)
            else {}
        )
        self.assertTrue(bool(throttled_summary.get("throttle_blocked", False)))
        self.assertEqual(int(throttled_summary.get("actions_executed", -1) or 0), 0)
        self.assertFalse(bool(throttled_decision.get("allow_auto_execution", True)))
        self.assertFalse(bool(throttle_state.get("allowed", True)))
        self.assertTrue(bool(throttle_state.get("scope_cooldown_active", False)))
        self.assertIn("scope_cooldown_active", throttle_state.get("reasons", []))
        self.assertIn("throttle_state", throttled_verification)

        status, stable = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective60-test",
                "source": "objective60-focused",
                "managed_scope": f"stable-{run_id}",
                "stale_after_seconds": 86400,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "force_degraded": False,
                "target_environment_state": {
                    "zone_freshness_seconds": 86400,
                    "max_degraded_zones": 0,
                    "max_missing_key_objects": 0,
                    "key_objects": [],
                },
                "metadata_json": {"run_id": run_id, "phase": "stable"},
            },
        )
        self.assertEqual(status, 200, stable)
        stable_summary = stable.get("summary", {}) if isinstance(stable, dict) else {}
        self.assertEqual(int(stable_summary.get("degraded_signal_count", -1) or 0), 0)
        stable_cycle = stable.get("cycle", {}) if isinstance(stable, dict) else {}
        stable_assessment = (
            stable_cycle.get("assessment", {})
            if isinstance(stable_cycle.get("assessment", {}), dict)
            else {}
        )
        stable_post = (
            stable_assessment.get("post", {})
            if isinstance(stable_assessment.get("post", {}), dict)
            else {}
        )
        stable_post_system = (
            stable_post.get("system_metrics", {})
            if isinstance(stable_post.get("system_metrics", {}), dict)
            else {}
        )
        self.assertIn("stability_score", stable_post_system)


if __name__ == "__main__":
    unittest.main(verbosity=2)
