import json
import os
import unittest
import urllib.error
import urllib.request


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


class Objective212BridgeTest(unittest.TestCase):
    def test_event_to_goal_bridge_and_safety_gate(self) -> None:
        status, capability = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace observation and check",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"mode": "safe"},
            },
        )
        self.assertEqual(status, 200, capability)

        status, capability2 = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "observation_capability",
                "category": "perception",
                "description": "Object/scene observation",
                "requires_confirmation": True,
                "enabled": True,
                "safety_policy": {"requires_human": True},
            },
        )
        self.assertEqual(status, 200, capability2)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check",
                "parsed_intent": "execute_capability",
                "confidence": 0.95,
                "requested_goal": "run workspace check",
                "metadata_json": {"capability": "workspace_check"},
            },
        )
        self.assertEqual(status, 200, event)
        self.assertIn("resolution", event)
        event_id = event["input_id"]
        resolution = event["resolution"]
        self.assertEqual(resolution["resolution_status"], "auto_execute")
        self.assertEqual(resolution["internal_intent"], "execute_capability")
        self.assertTrue(resolution["capability_registered"])
        self.assertTrue(resolution["goal_id"] is not None)

        status, resolution_get = get_json(f"/gateway/events/{event_id}/resolution")
        self.assertEqual(status, 200, resolution_get)
        self.assertEqual(resolution_get["goal_id"], resolution["goal_id"])

        status, goal = get_json(f"/goals/{resolution['goal_id']}")
        self.assertEqual(status, 200, goal)
        self.assertEqual(goal["status"], "new")

        status, voice_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": "look for the blue block",
                "parsed_intent": "identify_object",
                "confidence": 0.61,
                "requested_goal": "identify blue block",
            },
        )
        self.assertEqual(status, 200, voice_event)
        voice_event_id = voice_event["input_id"]
        voice_resolution = voice_event["resolution"]
        self.assertEqual(voice_resolution["resolution_status"], "requires_confirmation")
        self.assertTrue(voice_resolution["goal_id"] is not None)

        status, voice_goal = get_json(f"/goals/{voice_resolution['goal_id']}")
        self.assertEqual(status, 200, voice_goal)
        self.assertEqual(voice_goal["status"], "proposed")

        status, promoted = post_json(
            f"/gateway/events/{voice_event_id}/promote-to-goal",
            {"requested_by": "operator", "force": False},
        )
        self.assertEqual(status, 200, promoted)
        self.assertEqual(promoted["resolution_status"], "auto_execute")

        status, promoted_goal = get_json(f"/goals/{promoted['goal_id']}")
        self.assertEqual(status, 200, promoted_goal)
        self.assertEqual(promoted_goal["status"], "new")

        status, blocked_event = post_json(
            "/gateway/intake/api",
            {
                "payload": {"command": "move arm fast"},
                "parsed_intent": "execute_capability",
                "confidence": 0.98,
                "requested_goal": "move arm to target",
                "safety_flags": ["deny_execution"],
                "metadata_json": {"capability": "arm_movement"},
            },
        )
        self.assertEqual(status, 200, blocked_event)
        blocked_resolution = blocked_event["resolution"]
        self.assertEqual(blocked_resolution["resolution_status"], "blocked")
        self.assertEqual(blocked_resolution["goal_id"], None)

        status, blocked_promote = post_json(
            f"/gateway/events/{blocked_event['input_id']}/promote-to-goal",
            {"requested_by": "operator", "force": False},
        )
        self.assertEqual(status, 422, blocked_promote)


if __name__ == "__main__":
    unittest.main(verbosity=2)
