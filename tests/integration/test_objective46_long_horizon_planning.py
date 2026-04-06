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


class Objective46LongHorizonPlanningTest(unittest.TestCase):
    def test_objective46_multi_goal_ranked_checkpointed_and_replans_on_drift(self) -> None:
        run_id = uuid4().hex[:8]

        status, created = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective46-test",
                "source": "objective46-focused",
                "planning_horizon_minutes": 180,
                "goal_candidates": [
                    {
                        "goal_key": f"refresh-{run_id}",
                        "title": "Refresh stale zone",
                        "priority": "high",
                        "goal_type": "workspace_refresh",
                        "dependencies": [],
                        "estimated_steps": 1,
                        "expected_value": 0.8,
                        "urgency": 0.85,
                        "requires_fresh_map": True,
                        "requires_high_confidence": False,
                        "is_physical": False,
                    },
                    {
                        "goal_key": f"reach-{run_id}",
                        "title": "Directed reach",
                        "priority": "normal",
                        "goal_type": "directed_reach",
                        "dependencies": [f"refresh-{run_id}"],
                        "estimated_steps": 3,
                        "expected_value": 0.9,
                        "urgency": 0.7,
                        "requires_fresh_map": True,
                        "requires_high_confidence": True,
                        "is_physical": True,
                    },
                    {
                        "goal_key": f"low-physical-{run_id}",
                        "title": "Low-value physical tidy",
                        "priority": "low",
                        "goal_type": "tidy_zone",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.3,
                        "urgency": 0.2,
                        "requires_fresh_map": False,
                        "requires_high_confidence": False,
                        "is_physical": True,
                    },
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.75,
                },
                "map_freshness_seconds": 1400,
                "object_confidence": 0.62,
                "human_aware_state": {
                    "human_in_workspace": True,
                    "shared_workspace_active": True,
                },
                "operator_preferences": {
                    "workspace_refresh": 0.8,
                    "directed_reach": 0.4,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)

        plan_id = int(created.get("plan_id", 0))
        self.assertGreater(plan_id, 0)

        ranked = created.get("ranked_goals", [])
        self.assertGreaterEqual(len(ranked), 3)
        self.assertEqual(ranked[0].get("goal_key"), f"refresh-{run_id}")

        deferred_low = next((item for item in ranked if item.get("goal_key") == f"low-physical-{run_id}"), None)
        self.assertIsNotNone(deferred_low, ranked)
        self.assertTrue(bool(deferred_low.get("deferred", False)), deferred_low)

        checkpoints = created.get("checkpoints", [])
        self.assertGreaterEqual(len(checkpoints), 3)
        self.assertEqual(created.get("next_checkpoint", {}).get("status"), "active")

        explanation = created.get("explanation", {})
        self.assertIn("selected_plan_reason", explanation)
        self.assertIn("replan_triggers", explanation)

        status, current = get_json("/planning/horizon/plans/current")
        self.assertEqual(status, 200, current)
        current_plan = current.get("plan", {}) if isinstance(current, dict) else {}
        self.assertEqual(int(current_plan.get("plan_id", 0)), plan_id)

        first_checkpoint_id = int(checkpoints[0].get("checkpoint_id", 0))
        status, advanced = post_json(
            f"/planning/horizon/plans/{plan_id}/checkpoints/advance",
            {
                "actor": "objective46-test",
                "reason": "first checkpoint completed",
                "outcome": "checkpoint_reached",
                "checkpoint_id": first_checkpoint_id,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, advanced)
        self.assertTrue(bool(advanced.get("advanced", False)))
        next_checkpoint = advanced.get("plan", {}).get("next_checkpoint", {})
        self.assertTrue(isinstance(next_checkpoint, dict))
        self.assertEqual(next_checkpoint.get("status"), "active")

        status, drift = post_json(
            f"/planning/horizon/plans/{plan_id}/future-drift",
            {
                "actor": "objective46-test",
                "reason": "future assumption broke",
                "drift_type": "object_confidence",
                "observed_value": "0.31",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, drift)
        self.assertTrue(bool(drift.get("replanned", False)), drift)
        self.assertEqual(drift.get("plan", {}).get("status"), "replanned")

        replanned_explanation = drift.get("plan", {}).get("explanation", {})
        self.assertIn("last_replan", replanned_explanation)
        self.assertEqual(replanned_explanation.get("last_replan", {}).get("drift_type"), "object_confidence")


if __name__ == "__main__":
    unittest.main(verbosity=2)
