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
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective50EnvironmentMaintenanceAutonomyTest(unittest.TestCase):
    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
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
        self.assertEqual(status, 200, payload)

    def _create_stale_observation(self, *, zone: str, run_id: str) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective50 stale scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {},
                },
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {
                            "label": f"obj50-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.95,
                            "observed_at": stale_time,
                        }
                    ]
                },
            },
        )
        self.assertEqual(status, 200, done)

    def test_objective50_maintenance_cycle_detects_and_stabilizes(self) -> None:
        run_id = uuid4().hex[:8]
        stale_zone = f"front-left-obj50-{run_id}"

        self._register_workspace_scan()
        self._create_stale_observation(zone=stale_zone, run_id=run_id)

        status, cycle = post_json(
            "/maintenance/cycle",
            {
                "actor": "objective50-test",
                "source": "objective50-focused",
                "stale_after_seconds": 300,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, cycle)

        run = cycle.get("run", {}) if isinstance(cycle, dict) else {}
        self.assertGreaterEqual(int(run.get("run_id", 0)), 1)

        signals = run.get("detected_signals", []) if isinstance(run.get("detected_signals", []), list) else []
        self.assertTrue(any(str(item.get("signal_type", "")) == "stale_zone_detected" for item in signals))
        self.assertTrue(any(str(item.get("target_scope", "")) == stale_zone for item in signals))

        outcomes = run.get("maintenance_outcomes", {}) if isinstance(run.get("maintenance_outcomes", {}), dict) else {}
        self.assertGreaterEqual(int(outcomes.get("degraded_signal_count", 0)), 1)
        self.assertGreaterEqual(int(outcomes.get("strategies_created", 0)), 1)
        self.assertGreaterEqual(int(outcomes.get("actions_executed", 0)), 1)
        self.assertGreaterEqual(int(outcomes.get("memory_entries_created", 0)), 1)
        self.assertTrue(bool(run.get("stabilized", False)))

        run_id_value = int(run.get("run_id", 0))
        status, detail = get_json(f"/maintenance/runs/{run_id_value}")
        self.assertEqual(status, 200, detail)
        detailed = detail.get("run", {}) if isinstance(detail, dict) else {}
        self.assertGreaterEqual(len(detailed.get("actions", [])), 1)
        self.assertGreaterEqual(len(detailed.get("strategies", [])), 1)

        status, decisions = get_json("/planning/decisions?decision_type=maintenance_action&limit=10")
        self.assertEqual(status, 200, decisions)
        decision_rows = decisions.get("decisions", []) if isinstance(decisions, dict) else []
        self.assertGreaterEqual(len(decision_rows), 1)

        status, memories = get_json("/memory")
        self.assertEqual(status, 200, memories)
        memory_rows = memories if isinstance(memories, list) else []
        self.assertTrue(any(str(item.get("memory_class", "")) == "maintenance_outcome" for item in memory_rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
