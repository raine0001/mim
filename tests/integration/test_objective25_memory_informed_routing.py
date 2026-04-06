import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4


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


class Objective25MemoryInformedRoutingTest(unittest.TestCase):
    def test_memory_signal_changes_observe_workspace_decision(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"table_obj25_{run_id}"
        label = f"cup_obj25_{run_id}"

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

        stale_observed_at = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()

        status, stale_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace table",
                "parsed_intent": "observe_workspace",
                "confidence": 0.9,
                    "metadata_json": {"scan_mode": "full", "scan_area": zone, "confidence_threshold": 0.6},
            },
        )
        self.assertEqual(status, 200, stale_event)
        stale_execution_id = stale_event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, resp = post_json(
                f"/gateway/capabilities/executions/{stale_execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, resp)

        status, stale_feedback = post_json(
            f"/gateway/capabilities/executions/{stale_execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {"label": label, "zone": zone, "confidence": 0.92, "observed_at": stale_observed_at}
                    ]
                },
            },
        )
        self.assertEqual(status, 200, stale_feedback)

        status, stale_resolution_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace table again",
                "parsed_intent": "observe_workspace",
                "confidence": 0.92,
                "metadata_json": {"scan_mode": "full", "scan_area": zone, "confidence_threshold": 0.6},
            },
        )
        self.assertEqual(status, 200, stale_resolution_event)
        stale_resolution = stale_resolution_event["resolution"]
        self.assertEqual(stale_resolution.get("outcome"), "requires_confirmation")
        self.assertIn(
            stale_resolution.get("reason"),
            {"memory_stale_requires_reconfirm", "memory_object_uncertain_requires_reconfirm"},
        )
        memory_signal = stale_resolution.get("metadata_json", {}).get("memory_signal", {})
        self.assertGreaterEqual(memory_signal.get("stale_count", 0), 1)

        status, recent_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace table now",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {"scan_mode": "quick", "scan_area": zone, "confidence_threshold": 0.6},
            },
        )
        self.assertEqual(status, 200, recent_event)
        recent_execution_id = recent_event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, resp = post_json(
                f"/gateway/capabilities/executions/{recent_execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, resp)

        status, recent_feedback = post_json(
            f"/gateway/capabilities/executions/{recent_execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {"label": label, "zone": zone, "confidence": 0.97}
                    ]
                },
            },
        )
        self.assertEqual(status, 200, recent_feedback)

        status, memory_confident_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace table with memory",
                "parsed_intent": "observe_workspace",
                "confidence": 0.9,
                "metadata_json": {"scan_mode": "quick", "scan_area": zone, "confidence_threshold": 0.6},
            },
        )
        self.assertEqual(status, 200, memory_confident_event)
        resolution = memory_confident_event["resolution"]
        self.assertEqual(resolution.get("outcome"), "auto_execute")
        self.assertEqual(resolution.get("reason"), "memory_confident_recent_identity")
        signal = resolution.get("metadata_json", {}).get("memory_signal", {})
        self.assertGreaterEqual(signal.get("recent_count", 0), 1)
        self.assertGreaterEqual(float(signal.get("best_effective_confidence", 0.0)), 0.75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
