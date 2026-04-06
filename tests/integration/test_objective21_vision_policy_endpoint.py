import json
import os
import unittest
import urllib.error
import urllib.request


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)


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


class Objective21VisionPolicyEndpointTest(unittest.TestCase):
    def test_vision_policy_endpoint_returns_active_profile(self) -> None:
        status, payload = get_json("/gateway/vision-policy")
        self.assertEqual(status, 200, payload)

        self.assertEqual(payload["policy_version"], "vision-policy-v1")
        self.assertIn("policy_path", payload)
        self.assertIn("thresholds", payload)
        self.assertGreaterEqual(payload["thresholds"]["high"], payload["thresholds"]["medium"])

        self.assertIn("allow_auto_propose", payload)
        self.assertIn("auto_execute_safe_intents", payload)
        self.assertIn("blocked_capability_implications", payload)
        self.assertIn("label_overrides", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
