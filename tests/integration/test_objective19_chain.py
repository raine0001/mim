import json
import unittest
import urllib.error
import urllib.request


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = DEFAULT_BASE_URL


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective19ChainTest(unittest.TestCase):
    def test_goal_plan_timeline_status(self) -> None:
        status, goal = post_json(
            "/goals",
            {
                "goal_type": "objective19_chain_test",
                "goal_description": "Validate multi-step goal chain",
                "requested_by": "test",
                "priority": "high",
                "status": "new",
            },
        )
        self.assertEqual(status, 200, goal)
        goal_id = goal["goal_id"]

        status, action1 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "local",
                "action_type": "step-1",
                "input_ref": "chain://step1",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 1,
                "status": "completed",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 0}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 1}},
            },
        )
        self.assertEqual(status, 200, action1)

        status, action2 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "local",
                "action_type": "step-2",
                "input_ref": "chain://step2",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 2,
                "depends_on_action_id": action1["action_id"],
                "status": "retried",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 1}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 2}},
            },
        )
        self.assertEqual(status, 200, action2)

        status, action3 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "local",
                "action_type": "step-3",
                "input_ref": "chain://step3",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 3,
                "depends_on_action_id": action2["action_id"],
                "status": "skipped",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 2}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 3}},
            },
        )
        self.assertEqual(status, 200, action3)

        status, plan = post_json(
            f"/goals/{goal_id}/plan",
            {
                "ordered_action_ids": [action1["action_id"], action2["action_id"], action3["action_id"]],
                "current_step_index": 1,
            },
        )
        self.assertEqual(status, 200, plan)
        self.assertEqual(len(plan["ordered_action_ids"]), 3)

        status, plan_get = get_json(f"/goals/{goal_id}/plan")
        self.assertEqual(status, 200, plan_get)
        self.assertEqual(len(plan_get["actions"]), 3)

        status, timeline = get_json(f"/goals/{goal_id}/timeline")
        self.assertEqual(status, 200, timeline)
        self.assertEqual(len(timeline["timeline"]), 3)
        self.assertEqual(timeline["timeline"][0]["action"]["sequence_index"], 1)
        self.assertEqual(timeline["timeline"][1]["action"]["depends_on_action_id"], action1["action_id"])

        status, goal_status = get_json(f"/goals/{goal_id}/status")
        self.assertEqual(status, 200, goal_status)
        self.assertEqual(goal_status["goal_id"], goal_id)
        self.assertIn(goal_status["derived_status"], {"achieved", "partial", "failed", "blocked", "unknown"})
        self.assertEqual(goal_status["total_steps"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
