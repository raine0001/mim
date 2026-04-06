import json
import os
import unittest
import urllib.error
import urllib.request


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
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective217ExecutionFeedbackTest(unittest.TestCase):
    def test_guarded_feedback_transitions(self) -> None:
        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {"capability": "workspace_check"},
            },
        )
        self.assertEqual(status, 200, event)
        execution = event.get("execution", {})
        execution_id = execution.get("execution_id")
        self.assertIsInstance(execution_id, int)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "executor accepted request",
                "feedback_json": {"queue": "executor-a"},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "accepted")

        status, running = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "running",
                "reason": "execution started",
                "feedback_json": {"progress": 50},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, running)
        self.assertEqual(running["status"], "running")

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "execution complete",
                "feedback_json": {"progress": 100},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, succeeded)
        self.assertEqual(succeeded["status"], "succeeded")

        status, invalid = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "running",
                "reason": "invalid rollback",
                "feedback_json": {},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 422, invalid)

        status, feedback = get_json(f"/gateway/capabilities/executions/{execution_id}/feedback")
        self.assertEqual(status, 200, feedback)
        self.assertEqual(feedback["status"], "succeeded")
        history = feedback["feedback_json"].get("history", [])
        self.assertGreaterEqual(len(history), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
