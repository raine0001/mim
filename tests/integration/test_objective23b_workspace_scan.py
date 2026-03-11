import json
import os
import unittest
import urllib.error
import urllib.request
from uuid import uuid4


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


class Objective23BSafeCapabilityExpansionTest(unittest.TestCase):
    def test_workspace_scan_safe_capability_flow(self) -> None:
        run_id = uuid4().hex[:8]
        base_zone = f"workspace_obj23b_{run_id}"

        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200)

        payloads = [
            (
                "/gateway/intake/text",
                {
                    "text": "scan workspace",
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.95,
                    "metadata_json": {
                        "scan_mode": "full",
                        "scan_area": f"{base_zone}_a",
                        "confidence_threshold": 0.65,
                    },
                },
            ),
            (
                "/gateway/voice/input",
                {
                    "transcript": "scan workspace now",
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.93,
                    "metadata_json": {
                        "scan_mode": "quick",
                        "scan_area": f"{base_zone}_b",
                        "confidence_threshold": 0.6,
                    },
                },
            ),
            (
                "/gateway/intake/api",
                {
                    "payload": {"command": "scan workspace"},
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.97,
                    "metadata_json": {
                        "scan_mode": "targeted",
                        "scan_area": f"{base_zone}_c",
                        "confidence_threshold": 0.7,
                    },
                },
            ),
        ]

        execution_ids: list[int] = []
        for path, body in payloads:
            status, event = post_json(path, body)
            self.assertEqual(status, 200, event)
            execution = event.get("execution", {})
            self.assertEqual(execution.get("capability_name"), "workspace_scan")
            self.assertEqual(execution.get("status"), "dispatched")
            self.assertIn("scan_mode", execution.get("arguments_json", {}))
            execution_ids.append(execution["execution_id"])

        for execution_id in execution_ids:
            status, accepted = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": "accepted", "reason": "accepted", "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, accepted)

            status, running = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": "running", "reason": "running", "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, running)

            observations = [
                {"label": f"workspace_obj23b_{run_id}", "confidence": 0.94},
                {"label": f"table_obj23b_{run_id}", "confidence": 0.88},
            ]
            status, succeeded = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": "succeeded",
                    "reason": "scan complete",
                    "actor": "tod",
                    "feedback_json": {
                        "observations": observations,
                        "observation_confidence": 0.9,
                    },
                },
            )
            self.assertEqual(status, 200, succeeded)
            feedback_json = succeeded.get("feedback_json", {})
            self.assertIn("observation_event_id", feedback_json)
            self.assertEqual(feedback_json.get("observations", []), observations)

            status, obs_view = get_json(f"/operator/executions/{execution_id}/observations")
            self.assertEqual(status, 200, obs_view)
            self.assertGreaterEqual(len(obs_view.get("observations", [])), 1)

            status, promoted = post_json(
                f"/operator/executions/{execution_id}/promote-to-goal",
                {"actor": "operator", "reason": "promote scan findings", "metadata_json": {"ticket": "OBJ23B-PROMOTE"}},
            )
            self.assertEqual(status, 200, promoted)
            self.assertIsNotNone(promoted.get("goal_id"))

            status, ignored = post_json(
                f"/operator/executions/{execution_id}/ignore",
                {"actor": "operator", "reason": "ignore findings", "metadata_json": {"ticket": "OBJ23B-IGNORE"}},
            )
            self.assertEqual(status, 200, ignored)
            self.assertEqual(ignored.get("status"), "succeeded")

            status, rescan = post_json(
                f"/operator/executions/{execution_id}/request-rescan",
                {"actor": "operator", "reason": "rescan requested", "metadata_json": {"ticket": "OBJ23B-RESCAN"}},
            )
            self.assertEqual(status, 200, rescan)
            self.assertEqual(rescan.get("status"), "dispatched")


if __name__ == "__main__":
    unittest.main(verbosity=2)
