import json
import os
import unittest
import urllib.error
import urllib.request


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


def post_json(path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, headers: dict | None = None) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective22TodFeedbackIntegrationTest(unittest.TestCase):
    def test_handoff_contract_and_tod_feedback_flow(self) -> None:
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
                "confidence": 0.97,
                "requested_goal": "run workspace check",
                "metadata_json": {"capability": "workspace_check", "trace_id": "obj22-flow"},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event.get("execution", {}).get("execution_id")
        self.assertIsInstance(execution_id, int)

        status, handoff = get_json(f"/gateway/capabilities/executions/{execution_id}/handoff")
        self.assertEqual(status, 200, handoff)
        self.assertEqual(handoff["execution_id"], execution_id)
        self.assertEqual(handoff["capability_name"], "workspace_check")
        self.assertIn("goal_ref", handoff)
        self.assertIn("action_ref", handoff)
        self.assertIn("correlation_metadata", handoff)

        status, denied = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "unauthorized writer",
                "feedback_json": {"phase": "accepted"},
                "actor": "random-junk",
            },
        )
        self.assertEqual(status, 403, denied)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "tod accepted execution",
                "feedback_json": {"phase": "accepted"},
                "correlation_json": {"trace_id": "obj22-flow"},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "accepted")

        status, running = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "runtime_outcome": "retry_in_progress",
                "feedback_json": {"retry": 1},
                "correlation_json": {"trace_id": "obj22-flow"},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, running)
        self.assertEqual(running["status"], "running")

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "runtime_outcome": "recovered",
                "feedback_json": {"final": "ok"},
                "recovery_state": "fallback_recovered",
                "correlation_json": {"trace_id": "obj22-flow"},
                "actor": "tod",
            },
        )
        self.assertEqual(status, 200, succeeded)
        self.assertEqual(succeeded["status"], "succeeded")

        status, feedback = get_json(f"/gateway/capabilities/executions/{execution_id}/feedback")
        self.assertEqual(status, 200, feedback)
        self.assertEqual(feedback["status"], "succeeded")
        self.assertEqual(feedback["feedback_json"].get("runtime_outcome"), "recovered")
        self.assertEqual(feedback["feedback_json"].get("recovery_state"), "fallback_recovered")
        self.assertEqual(feedback["feedback_json"].get("correlation_json", {}).get("trace_id"), "obj22-flow")
        history = feedback["feedback_json"].get("history", [])
        self.assertGreaterEqual(len(history), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
