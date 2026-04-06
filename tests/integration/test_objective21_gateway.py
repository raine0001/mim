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


class Objective21GatewayTest(unittest.TestCase):
    def test_unified_intake_and_capability_registry(self) -> None:
        status, normalized = post_json(
            "/gateway/intake",
            {
                "source": "api",
                "raw_input": "{\"cmd\":\"sync\"}",
                "parsed_intent": "sync_state",
                "confidence": 0.95,
                "target_system": "mim",
                "requested_goal": "sync objective/task state",
                "safety_flags": ["read_only"],
                "metadata_json": {"origin": "integration-test"},
            },
        )
        self.assertEqual(status, 200, normalized)
        self.assertEqual(normalized["source"], "api")
        self.assertEqual(normalized["parsed_intent"], "sync_state")

        status, text_input = post_json(
            "/gateway/intake/text",
            {
                "text": "check workspace blockers",
                "parsed_intent": "workspace_check",
                "requested_goal": "identify blockers",
            },
        )
        self.assertEqual(status, 200, text_input)
        self.assertEqual(text_input["source"], "text")

        status, voice_input = post_json(
            "/gateway/voice/input",
            {
                "transcript": "run step two",
                "parsed_intent": "task_execute",
                "confidence": 0.74,
                "requested_goal": "execute step two",
            },
        )
        self.assertEqual(status, 200, voice_input)
        self.assertEqual(voice_input["source"], "voice")

        status, vision_obs = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "detected unknown object near arm",
                "detected_labels": ["unknown_object", "workspace"],
                "confidence": 0.61,
                "proposed_goal": "perform workspace safety scan",
            },
        )
        self.assertEqual(status, 200, vision_obs)
        self.assertEqual(vision_obs["source"], "vision")
        self.assertIn("requires_confirmation", vision_obs["safety_flags"])

        status, spoken = post_json(
            "/gateway/voice/output",
            {
                "message": "I detected a blocked dependency.",
                "voice_profile": "status",
                "channel": "speaker",
            },
        )
        self.assertEqual(status, 200, spoken)
        self.assertEqual(spoken["status"], "queued")

        status, capability = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Run workspace checks before task execution",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"max_rate_per_min": 5},
            },
        )
        self.assertEqual(status, 200, capability)
        self.assertEqual(capability["capability_name"], "workspace_check")

        status, capabilities = get_json("/gateway/capabilities")
        self.assertEqual(status, 200, capabilities)
        self.assertTrue(any(item["capability_name"] == "workspace_check" for item in capabilities))

        status, intake = get_json("/gateway/intake")
        self.assertEqual(status, 200, intake)
        self.assertGreaterEqual(len(intake), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
