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


class Objective44ConstraintEvaluationEngineTest(unittest.TestCase):
    def test_objective44_constraint_engine_decisions_and_explainability(self) -> None:
        run_id = uuid4().hex[:8]

        status, allowed = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective44-test",
                "source": "objective44-focused",
                "goal": {"goal_id": f"obj44-allow-{run_id}", "desired_state": "safe_execution"},
                "action_plan": {"action_type": "observe_workspace", "is_physical": False},
                "workspace_state": {
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.95,
                    "map_freshness_seconds": 10,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {"min_target_confidence": 0.7, "map_freshness_limit_seconds": 900, "unlawful_action": False},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, allowed)
        self.assertEqual(allowed.get("decision"), "allowed")
        self.assertIn("explanation", allowed)

        status, blocked = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective44-test",
                "source": "objective44-focused",
                "goal": {"goal_id": f"obj44-block-{run_id}", "desired_state": "physical_execution"},
                "action_plan": {"action_type": "execute_action_plan", "is_physical": True},
                "workspace_state": {
                    "human_in_workspace": True,
                    "human_near_target_zone": True,
                    "human_near_motion_path": True,
                    "shared_workspace_active": True,
                    "target_confidence": 0.91,
                    "map_freshness_seconds": 20,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {"min_target_confidence": 0.7, "map_freshness_limit_seconds": 900, "unlawful_action": False},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, blocked)
        self.assertEqual(blocked.get("decision"), "blocked")
        self.assertGreaterEqual(len(blocked.get("violations", [])), 1)

        status, replan = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective44-test",
                "source": "objective44-focused",
                "goal": {"goal_id": f"obj44-replan-{run_id}", "desired_state": "stable_target"},
                "action_plan": {"action_type": "execute_action_plan", "is_physical": True},
                "workspace_state": {
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.41,
                    "map_freshness_seconds": 1800,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {"min_target_confidence": 0.7, "map_freshness_limit_seconds": 900, "unlawful_action": False},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, replan)
        self.assertEqual(replan.get("decision"), "requires_replan")
        self.assertGreaterEqual(len(replan.get("warnings", [])), 1)

        status, confirm = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective44-test",
                "source": "objective44-focused",
                "goal": {"goal_id": f"obj44-confirm-{run_id}", "desired_state": "physical_execution"},
                "action_plan": {"action_type": "execute_action_plan", "is_physical": True},
                "workspace_state": {
                    "human_in_workspace": True,
                    "human_near_target_zone": True,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.92,
                    "map_freshness_seconds": 100,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {"min_target_confidence": 0.7, "map_freshness_limit_seconds": 900, "unlawful_action": False},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, confirm)
        self.assertEqual(confirm.get("decision"), "requires_confirmation")
        self.assertEqual(confirm.get("recommended_next_step"), "request_operator_confirmation")

        status, last = get_json("/constraints/last-evaluation")
        self.assertEqual(status, 200, last)
        evaluation = last.get("evaluation") if isinstance(last, dict) else None
        self.assertTrue(isinstance(evaluation, dict), last)
        self.assertIn("decision", evaluation)
        self.assertIn("explanation", evaluation)

        status, history = get_json("/constraints/history?limit=10")
        self.assertEqual(status, 200, history)
        evaluations = history.get("evaluations", []) if isinstance(history, dict) else []
        self.assertGreaterEqual(len(evaluations), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
