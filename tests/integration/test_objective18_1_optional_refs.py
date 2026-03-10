import json
import unittest
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:8001"


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


class Objective181OptionalRefsTest(unittest.TestCase):
    def test_semantically_invalid_optional_refs_return_422(self) -> None:
        status, payload = post_json(
            "/goals",
            {
                "objective_id": 0,
                "task_id": -1,
                "goal_type": "objective18_1_regression",
                "goal_description": "invalid refs should not 500",
                "requested_by": "test",
                "priority": "normal",
                "status": "new",
            },
        )
        self.assertEqual(status, 422, payload)

    def test_nonexistent_optional_refs_return_404(self) -> None:
        status, payload = post_json(
            "/goals",
            {
                "objective_id": 999999,
                "goal_type": "objective18_1_regression",
                "goal_description": "missing objective should not 500",
                "requested_by": "test",
                "priority": "normal",
                "status": "new",
            },
        )
        self.assertEqual(status, 404, payload)
        self.assertIn("detail", payload)

    def test_valid_optional_refs_return_200(self) -> None:
        status, objective = post_json(
            "/objectives",
            {
                "title": "Objective18.1 Regression Objective",
                "description": "used by integration test",
                "priority": "normal",
                "constraints": [],
                "success_criteria": "goal can be created",
                "status": "new",
            },
        )
        self.assertEqual(status, 200, objective)

        objective_id = objective["objective_id"]

        status, task = post_json(
            "/tasks",
            {
                "objective_id": objective_id,
                "title": "Objective18.1 Regression Task",
                "scope": "integration check",
                "dependencies": [],
                "acceptance_criteria": "goal create passes",
                "assigned_to": "test",
                "status": "queued",
            },
        )
        self.assertEqual(status, 200, task)

        status, goal = post_json(
            "/goals",
            {
                "objective_id": objective_id,
                "task_id": task["task_id"],
                "goal_type": "objective18_1_regression",
                "goal_description": "valid refs should pass",
                "requested_by": "test",
                "priority": "normal",
                "status": "new",
            },
        )
        self.assertEqual(status, 200, goal)
        self.assertGreater(goal.get("goal_id", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
