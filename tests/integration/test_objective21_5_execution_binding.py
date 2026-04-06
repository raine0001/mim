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


class Objective215ExecutionBindingTest(unittest.TestCase):
    def test_execution_binding_and_dispatch_lifecycle(self) -> None:
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

        status, auto_event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "requested_goal": "run workspace check",
                "metadata_json": {"capability": "workspace_check"},
            },
        )
        self.assertEqual(status, 200, auto_event)
        auto_exec = auto_event.get("execution")
        self.assertIsNotNone(auto_exec)
        self.assertEqual(auto_exec["dispatch_decision"], "auto_dispatch")
        self.assertEqual(auto_exec["status"], "dispatched")

        status, ev_exec = get_json(f"/gateway/events/{auto_event['input_id']}/execution")
        self.assertEqual(status, 200, ev_exec)
        self.assertEqual(ev_exec["execution_id"], auto_exec["execution_id"])

        status, exec_detail = get_json(f"/gateway/capabilities/executions/{auto_exec['execution_id']}")
        self.assertEqual(status, 200, exec_detail)
        self.assertEqual(exec_detail["capability_name"], "workspace_check")

        status, pending_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": "do something around there",
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
                "metadata_json": {"capability": "workspace_check"},
            },
        )
        self.assertEqual(status, 200, pending_event)
        pending_exec = pending_event.get("execution")
        self.assertIsNotNone(pending_exec)
        self.assertEqual(pending_exec["dispatch_decision"], "requires_confirmation")
        self.assertEqual(pending_exec["status"], "pending_confirmation")

        status, blocked_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": "move it there now",
                "parsed_intent": "execute_capability",
                "confidence": 0.95,
                "metadata_json": {"capability": "arm_movement"},
            },
        )
        self.assertEqual(status, 200, blocked_event)
        blocked_exec = blocked_event.get("execution")
        self.assertIsNotNone(blocked_exec)
        self.assertEqual(blocked_exec["dispatch_decision"], "blocked")
        self.assertEqual(blocked_exec["status"], "blocked")

        status, manual_dispatch = post_json(
            f"/gateway/events/{pending_event['input_id']}/execution/dispatch",
            {
                "arguments_json": {"scope": "full"},
                "safety_mode": "supervised",
                "requested_executor": "tod",
                "force": True,
            },
        )
        self.assertEqual(status, 200, manual_dispatch)
        self.assertEqual(manual_dispatch["dispatch_decision"], "auto_dispatch")
        self.assertEqual(manual_dispatch["status"], "dispatched")
        self.assertEqual(manual_dispatch["safety_mode"], "supervised")


if __name__ == "__main__":
    unittest.main(verbosity=2)
