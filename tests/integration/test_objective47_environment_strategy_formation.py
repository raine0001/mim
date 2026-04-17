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


class Objective47EnvironmentStrategyFormationTest(unittest.TestCase):
    def test_objective47_strategy_generation_influence_and_lifecycle(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"front-left-obj47-{run_id}"

        status, generated = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective47-test",
                "source": "objective47-focused",
                "observed_conditions": [
                    {
                        "condition_type": "stale_scans",
                        "target_scope": scope,
                        "severity": 0.88,
                        "occurrence_count": 6,
                        "metadata_json": {"run_id": run_id},
                    }
                ],
                "min_severity": 0.4,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        self.assertGreaterEqual(int(generated.get("generated", 0)), 1)

        rows = generated.get("strategies", [])
        strategy = next((item for item in rows if item.get("target_scope") == scope), None)
        self.assertIsNotNone(strategy, rows)
        strategy_id = int(strategy.get("strategy_id", 0))
        self.assertGreater(strategy_id, 0)
        self.assertEqual(strategy.get("current_status"), "active")

        status, listed = get_json("/planning/strategies?status=active&limit=50")
        self.assertEqual(status, 200, listed)
        listed_rows = listed.get("strategies", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("strategy_id", 0)) == strategy_id for item in listed_rows), listed_rows)

        status, plan = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective47-test",
                "source": "objective47-focused",
                "planning_horizon_minutes": 120,
                "goal_candidates": [
                    {
                        "goal_key": f"refresh:{scope}",
                        "title": "Refresh target scope",
                        "priority": "normal",
                        "goal_type": "workspace_refresh",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.5,
                        "urgency": 0.5,
                        "is_physical": False,
                        "metadata_json": {"scope": scope, "run_id": run_id},
                    },
                    {
                        "goal_key": f"other-goal-{run_id}",
                        "title": "Other neutral goal",
                        "priority": "normal",
                        "goal_type": "directed_reach",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.5,
                        "urgency": 0.5,
                        "is_physical": False,
                        "metadata_json": {"scope": f"rear-right-obj47-{run_id}", "run_id": run_id},
                    },
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.75,
                },
                "map_freshness_seconds": 300,
                "object_confidence": 0.9,
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
        self.assertEqual(ranked[0].get("goal_key"), f"refresh:{scope}")

        explanation = plan.get("explanation", {})
        influenced_ids = explanation.get("influenced_strategy_ids", []) if isinstance(explanation, dict) else []
        self.assertIn(strategy_id, influenced_ids)

        status, strategy_after_plan = get_json(f"/planning/strategies/{strategy_id}")
        self.assertEqual(status, 200, strategy_after_plan)
        influenced_plan_ids = (strategy_after_plan.get("strategy", {}) or {}).get("influenced_plan_ids", [])
        self.assertIn(int(plan.get("plan_id", 0)), influenced_plan_ids)

        status, blocked = post_json(
            f"/planning/strategies/{strategy_id}/resolve",
            {
                "actor": "objective47-test",
                "reason": "missing scan capability in target scope",
                "status": "blocked",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, blocked)
        self.assertEqual((blocked.get("strategy", {}) or {}).get("current_status"), "blocked")
        self.assertIn("missing scan capability", (blocked.get("strategy", {}) or {}).get("status_reason", ""))

        status, stable = post_json(
            f"/planning/strategies/{strategy_id}/resolve",
            {
                "actor": "objective47-test",
                "reason": "freshness recovered",
                "status": "stable",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, stable)
        self.assertEqual((stable.get("strategy", {}) or {}).get("current_status"), "stable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
