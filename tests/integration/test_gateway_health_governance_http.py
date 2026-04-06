import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


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


class GatewayHealthGovernanceHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe_current_source_runtime(
            suite_name="Gateway health governance HTTP",
            base_url=BASE_URL,
            require_self_health=True,
            require_safety=True,
        )

    def _seed_degraded_health(self):
        for _ in range(3):
            status, payload = post_json(
                "/mim/self/health/record-metric",
                {
                    "memory_percent": 96.0,
                    "api_latency_ms": 650.0,
                    "api_error_rate": 0.2,
                    "cpu_percent": 88.0,
                },
            )
            if status == 404:
                self.skipTest("self-awareness health endpoints are not mounted on this runtime")
            self.assertEqual(status, 200, payload)

    def test_gateway_degraded_health_requires_confirmation(self):
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        self._seed_degraded_health()

        status, health_payload = get_json("/mim/self/health")
        if status == 404:
            self.skipTest("self-awareness health endpoints are not mounted on this runtime")
        self.assertEqual(status, 200, health_payload)
        self.assertIn(health_payload.get("status"), {"degraded", "critical"})

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

        resolution = event.get("resolution", {})
        self.assertEqual(resolution.get("outcome"), "requires_confirmation")
        self.assertEqual(resolution.get("reason"), "system_health_degraded")
        self.assertIn(
            "system_health_degraded",
            list(resolution.get("escalation_reasons") or []),
        )
        governance = (
            resolution.get("metadata_json", {}).get("governance", {})
            if isinstance(resolution.get("metadata_json", {}), dict)
            else {}
        )
        self.assertIn("system_health_degraded", governance.get("signal_codes", []))

    def test_combined_signals_and_dispatch_refusal_surface(self):
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        self._seed_degraded_health()

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": "run apt install dangerous-package",
                "parsed_intent": "execute_capability",
                "confidence": 0.97,
                "requested_goal": "install package",
                "metadata_json": {"capability": "workspace_check", "user_id": "operator"},
            },
        )
        self.assertEqual(status, 200, event)

        resolution = event.get("resolution", {})
        self.assertEqual(resolution.get("outcome"), "requires_confirmation")
        self.assertEqual(resolution.get("reason"), "user_action_safety_requires_inquiry")
        reasons = list(resolution.get("escalation_reasons") or [])
        self.assertIn("user_action_safety_risk", reasons)
        self.assertIn("system_health_degraded", reasons)

        status, dispatch = post_json(
            f"/gateway/events/{event['input_id']}/execution/dispatch",
            {},
        )
        self.assertEqual(status, 422, dispatch)
        self.assertIn("Governance context", str(dispatch.get("detail", "")))

        status, assessment = post_json(
            "/mim/safety/assess-action",
            {
                "user_id": "operator",
                "action_type": "execute_capability",
                "description": "apt install dangerous-package",
                "category": "software_installation",
                "command": "apt install dangerous-package",
                "target_path": "/usr/local/bin",
                "parameters": {},
            },
        )
        self.assertEqual(status, 200, assessment)

        query = urllib.parse.urlencode(
            {
                "action_id": assessment["action_id"],
                "user_id": "operator",
                "action_description": "apt install dangerous-package",
            }
        )
        status, inquiry = post_json(f"/mim/safety/inquiries?{query}", {})
        self.assertEqual(status, 200, inquiry)
        self.assertIn(inquiry.get("system_health_status"), {"degraded", "critical"})
        self.assertTrue(str(inquiry.get("governance_summary", "")).strip())
        self.assertTrue(str(inquiry.get("operator_prompt", "")).strip())
