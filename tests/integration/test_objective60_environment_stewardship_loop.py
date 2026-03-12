import json
import os
import urllib.error
import urllib.parse
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
                "metadata_json": {"run_id": run_id, "phase": "degraded"},
            },
        )
        self.assertEqual(status, 200, cycled)

        stewardship = cycled.get("stewardship", {}) if isinstance(cycled, dict) else {}
        cycle = cycled.get("cycle", {}) if isinstance(cycled, dict) else {}
        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        stewardship_id = int(stewardship.get("stewardship_id", 0) or 0)
        self.assertGreater(stewardship_id, 0)
        self.assertGreaterEqual(float(cycle.get("post_health", 0.0) or 0.0), float(cycle.get("pre_health", 0.0) or 0.0))
        self.assertGreaterEqual(int(summary.get("degraded_signal_count", 0) or 0), 1)
        self.assertGreaterEqual(int(summary.get("actions_executed", 0) or 0), 1)
        integration = cycle.get("integration_evidence", {}) if isinstance(cycle.get("integration_evidence", {}), dict) else {}
        strategy_goal_ids = integration.get("strategy_goal_ids", []) if isinstance(integration.get("strategy_goal_ids", []), list) else []
        self.assertGreaterEqual(len(strategy_goal_ids), 1)
        self.assertIn("autonomy_boundary_id", integration)
        self.assertIn("operator_preference_weight", integration)

        status, listed = get_json("/stewardship", {"managed_scope": scope, "limit": 20})
        self.assertEqual(status, 200, listed)
        rows = listed.get("stewardship", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("stewardship_id", 0) or 0) == stewardship_id for item in rows if isinstance(item, dict)))

        status, detail = get_json(f"/stewardship/{stewardship_id}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(int(detail.get("stewardship", {}).get("stewardship_id", 0) or 0), stewardship_id)

        status, history = get_json("/stewardship/history", {"stewardship_id": stewardship_id, "limit": 20})
        self.assertEqual(status, 200, history)
        history_rows = history.get("history", []) if isinstance(history, dict) else []
        self.assertTrue(any(int(item.get("stewardship_id", 0) or 0) == stewardship_id for item in history_rows if isinstance(item, dict)))

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
                "metadata_json": {"run_id": run_id, "phase": "stable"},
            },
        )
        self.assertEqual(status, 200, stable)
        stable_summary = stable.get("summary", {}) if isinstance(stable, dict) else {}
        self.assertEqual(int(stable_summary.get("degraded_signal_count", -1) or 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
