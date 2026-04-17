import json
import os
import unittest
import urllib.error
import urllib.request
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


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective48HumanPreferenceStrategyIntegrationTest(unittest.TestCase):
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

    def _run_scan(self, *, text: str, scan_area: str, observations: list[dict]) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scan_area,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {"observations": observations},
            },
        )
        self.assertEqual(status, 200, done)

    def _upsert_pref(self, pref_type: str, value) -> None:
        status, upserted = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": pref_type,
                "value": value,
                "confidence": 0.9,
                "source": "objective48-test",
            },
        )
        self.assertEqual(status, 200, upserted)

    def test_objective48_preference_aware_strategy_and_routine_integration(self) -> None:
        run_id = uuid4().hex[:8]
        scope_a = f"front-left-obj48-a-{run_id}"
        scope_b = f"front-left-obj48-b-{run_id}"
        routine_scope = f"front-left-obj48-r-{run_id}"

        self._upsert_pref("prefer_auto_refresh_scans", False)
        self._upsert_pref("prefer_minimal_interruption", False)
        self._upsert_pref("preferred_scan_zones", [])

        status, generated_a = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective48-test",
                "source": "objective48-focused",
                "observed_conditions": [
                    {
                        "condition_type": "stale_scans",
                        "target_scope": scope_a,
                        "severity": 0.62,
                        "occurrence_count": 4,
                    }
                ],
                "min_severity": 0.6,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id, "phase": "baseline"},
            },
        )
        self.assertEqual(status, 200, generated_a)
        baseline_strategy = next((item for item in generated_a.get("strategies", []) if item.get("target_scope") == scope_a), None)
        self.assertIsNotNone(baseline_strategy, generated_a)

        self._upsert_pref("prefer_auto_refresh_scans", True)
        self._upsert_pref("prefer_minimal_interruption", True)
        self._upsert_pref("preferred_scan_zones", [scope_b])

        status, generated_b = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective48-test",
                "source": "objective48-focused",
                "observed_conditions": [
                    {
                        "condition_type": "stale_scans",
                        "target_scope": scope_b,
                        "severity": 0.62,
                        "occurrence_count": 4,
                    }
                ],
                "min_severity": 0.6,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id, "phase": "preference-aware"},
            },
        )
        self.assertEqual(status, 200, generated_b)
        preferred_strategy = next((item for item in generated_b.get("strategies", []) if item.get("target_scope") == scope_b), None)
        self.assertIsNotNone(preferred_strategy, generated_b)

        self.assertGreaterEqual(float(preferred_strategy.get("priority_weight", 0.0)), float(baseline_strategy.get("priority_weight", 0.0)))
        self.assertTrue(bool(preferred_strategy.get("preference_adjustments", {}).get("prefer_auto_refresh_scans", False)))

        self._register_workspace_scan()
        for index in range(3):
            self._run_scan(
                text=f"objective48 routine scan {run_id}-{index}",
                scan_area=routine_scope,
                observations=[{"label": f"obj48-routine-{run_id}-{index}", "zone": routine_scope, "confidence": 0.95}],
            )

        status, routine_generated = post_json(
            "/planning/strategies/routines/generate",
            {
                "actor": "objective48-test",
                "source": "objective48-focused",
                "lookback_hours": 24,
                "min_occurrence_count": 3,
                "max_strategies": 5,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, routine_generated)
        self.assertGreaterEqual(int(routine_generated.get("generated", 0)), 1)
        self.assertTrue(any(bool((item.get("metadata_json", {}) or {}).get("routine_generated", False)) for item in routine_generated.get("strategies", [])))

        status, plan = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective48-test",
                "source": "objective48-focused",
                "planning_horizon_minutes": 120,
                "goal_candidates": [
                    {
                        "goal_key": f"other-{run_id}",
                        "title": "Other goal first in input order",
                        "priority": "normal",
                        "goal_type": "directed_reach",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.5,
                        "urgency": 0.5,
                        "is_physical": False,
                        "metadata_json": {"scope": f"rear-right-obj48-{run_id}"},
                    },
                    {
                        "goal_key": f"refresh:{scope_b}",
                        "title": "Refresh preferred scope",
                        "priority": "normal",
                        "goal_type": "workspace_refresh",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.5,
                        "urgency": 0.5,
                        "is_physical": False,
                        "metadata_json": {"scope": scope_b},
                    },
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.75,
                },
                "map_freshness_seconds": 200,
                "object_confidence": 0.95,
                "human_aware_state": {
                    "human_in_workspace": False,
                    "shared_workspace_active": False,
                },
                "operator_preferences": {},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, plan)
        ranked = plan.get("ranked_goals", [])
        self.assertGreaterEqual(len(ranked), 2)
        self.assertEqual(ranked[0].get("goal_key"), f"refresh:{scope_b}")

        explanation = plan.get("explanation", {})
        self.assertIn("strategy_context", explanation)
        self.assertTrue(len(explanation.get("influenced_strategy_ids", [])) >= 1)

        strategy_id = int(preferred_strategy.get("strategy_id", 0))
        status, detail = get_json(f"/planning/strategies/{strategy_id}")
        self.assertEqual(status, 200, detail)
        strategy_detail = detail.get("strategy", {}) if isinstance(detail, dict) else {}
        self.assertIn("strategy_reason", strategy_detail)
        self.assertIn("environment_signals", strategy_detail)
        self.assertIn("preference_adjustments", strategy_detail)
        self.assertIn("priority_weight", strategy_detail)
        self.assertTrue(bool(strategy_detail.get("preference_adjustments", {}).get("prefer_auto_refresh_scans", False)))

        status, decisions = get_json("/planning/decisions?decision_type=plan_selection&limit=10")
        self.assertEqual(status, 200, decisions)
        decision_rows = decisions.get("decisions", []) if isinstance(decisions, dict) else []
        self.assertGreaterEqual(len(decision_rows), 1)
        self.assertIn("preferences_applied", decision_rows[0])
        self.assertIn("strategies_applied", decision_rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
