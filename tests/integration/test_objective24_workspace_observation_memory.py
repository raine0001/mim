import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


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


class Objective24WorkspaceObservationMemoryTest(unittest.TestCase):
    def test_workspace_observation_memory_and_freshness(self) -> None:
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

        stale_observed_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

        def run_scan(observations: list[dict]) -> int:
            status, event = post_json(
                "/gateway/intake/text",
                {
                    "text": "scan workspace table",
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.95,
                    "metadata_json": {
                        "scan_mode": "full",
                        "scan_area": "table",
                        "confidence_threshold": 0.65,
                    },
                },
            )
            self.assertEqual(status, 200, event)
            execution_id = event["execution"]["execution_id"]

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
            self.assertIn("workspace_observation_ids", succeeded.get("feedback_json", {}))
            return execution_id

        run_scan(
            [
                {"label": "cup", "zone": "table", "confidence": 0.91},
                {"label": "cup", "zone": "table", "confidence": 0.87},
                {
                    "label": "notebook",
                    "zone": "desk",
                    "confidence": 0.8,
                    "observed_at": stale_observed_at,
                },
            ]
        )
        run_scan(
            [
                {"label": "cup", "zone": "table", "confidence": 0.89},
            ]
        )

        status, table_obs = get_json("/workspace/observations?zone=table")
        self.assertEqual(status, 200, table_obs)
        table_items = table_obs.get("observations", [])
        self.assertGreaterEqual(len(table_items), 1)

        cup = next((item for item in table_items if item.get("detected_object") == "cup"), None)
        self.assertIsNotNone(cup)
        assert cup is not None
        self.assertGreaterEqual(cup.get("observation_count", 0), 3)
        self.assertEqual(cup.get("freshness_state"), "recent")
        self.assertEqual(cup.get("lifecycle_status"), "active")

        observation_id = int(cup["observation_id"])
        status, observation_detail = get_json(f"/workspace/observations/{observation_id}")
        self.assertEqual(status, 200, observation_detail)
        self.assertEqual(observation_detail.get("detected_object"), "cup")
        self.assertEqual(observation_detail.get("zone"), "table")

        status, all_obs = get_json("/workspace/observations")
        self.assertEqual(status, 200, all_obs)
        notebook = next((item for item in all_obs.get("observations", []) if item.get("detected_object") == "notebook"), None)
        self.assertIsNotNone(notebook)
        assert notebook is not None
        self.assertEqual(notebook.get("freshness_state"), "stale")
        self.assertEqual(notebook.get("lifecycle_status"), "outdated")
        self.assertLess(float(notebook.get("effective_confidence", 1.0)), float(notebook.get("confidence", 0.0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
