import json
import os
import time
import unittest
import urllib.error
import urllib.request
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


class Objective34ContinuousWorkspaceMonitoringLoopTest(unittest.TestCase):
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

    def _run_scan(self, *, run_id: str, observations: list[dict], scan_area: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace objective34 {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scan_area,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {"observations": observations},
            },
        )
        self.assertEqual(status, 200, done)

    def _wait_for(self, predicate, timeout_seconds: float = 10.0, step_seconds: float = 0.3) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(step_seconds)
        return False

    def test_continuous_monitoring_scheduler_deltas_proposals_throttle_and_restart(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        baseline_zone = f"front-center-obj34-{run_id}"
        moved_zone = f"rear-center-obj34-{run_id}"
        object_label = f"obj34-target-{run_id}"

        self._run_scan(
            run_id=run_id,
            scan_area=baseline_zone,
            observations=[{"label": object_label, "zone": baseline_zone, "confidence": 0.95}],
        )

        status, started = post_json(
            "/workspace/monitoring/start",
            {
                "actor": "operator",
                "reason": "objective34 integration test",
                "trigger_mode": "interval",
                "interval_seconds": 1,
                "freshness_threshold_seconds": 900,
                "cooldown_seconds": 0,
                "max_scan_rate": 30,
                "priority_zones": [baseline_zone, moved_zone],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, started)
        self.assertTrue(started.get("is_running"))

        def has_any_scan() -> bool:
            st, body = get_json("/workspace/monitoring")
            return st == 200 and int(body.get("scan_count", 0)) >= 1

        self.assertTrue(self._wait_for(has_any_scan, timeout_seconds=8), "scheduled scan did not execute")

        self._run_scan(
            run_id=run_id,
            scan_area=moved_zone,
            observations=[{"label": object_label, "zone": moved_zone, "confidence": 0.92}],
        )
        self._run_scan(
            run_id=run_id,
            scan_area=moved_zone,
            observations=[{"label": f"other-{run_id}", "zone": moved_zone, "confidence": 0.91}],
        )

        seen_events: set[str] = set()

        def has_required_deltas() -> bool:
            st, body = get_json("/workspace/monitoring")
            if st != 200:
                return False
            events = {item.get("event") for item in body.get("last_deltas", []) if isinstance(item, dict)}
            seen_events.update(str(item) for item in events if str(item).strip())
            return "object_moved" in seen_events and "object_missing" in seen_events and "confidence_changed" in seen_events

        self.assertTrue(self._wait_for(has_required_deltas, timeout_seconds=20), "delta detection requirements not met")

        status, proposals = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, proposals)
        proposal_types = {item.get("proposal_type") for item in proposals.get("proposals", [])}
        self.assertIn("monitor_recheck_workspace", proposal_types)
        self.assertIn("monitor_search_adjacent_zone", proposal_types)

        status, throttled = post_json(
            "/workspace/monitoring/start",
            {
                "actor": "operator",
                "reason": "objective34 throttle check",
                "trigger_mode": "interval",
                "interval_seconds": 1,
                "freshness_threshold_seconds": 900,
                "cooldown_seconds": 0,
                "max_scan_rate": 1,
                "priority_zones": [baseline_zone, moved_zone],
                "metadata_json": {"run_id": run_id, "phase": "throttle"},
            },
        )
        self.assertEqual(status, 200, throttled)

        status, before = get_json("/workspace/monitoring")
        self.assertEqual(status, 200, before)
        before_count = int(before.get("scan_count", 0))
        time.sleep(2.5)
        status, after = get_json("/workspace/monitoring")
        self.assertEqual(status, 200, after)
        after_count = int(after.get("scan_count", 0))
        self.assertLessEqual(after_count - before_count, 1, "scan throttling not respected")

        status, stopped = post_json(
            "/workspace/monitoring/stop",
            {
                "actor": "operator",
                "reason": "simulate restart",
                "preserve_desired_running": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, stopped)
        self.assertFalse(stopped.get("is_running"))

        def restarted_by_reconcile() -> bool:
            st, body = get_json("/workspace/monitoring")
            return st == 200 and bool(body.get("desired_running")) and bool(body.get("is_running"))

        self.assertTrue(
            self._wait_for(restarted_by_reconcile, timeout_seconds=6),
            "monitoring did not restart from persisted desired state",
        )

        status, stopped_final = post_json(
            "/workspace/monitoring/stop",
            {
                "actor": "operator",
                "reason": "test cleanup",
                "preserve_desired_running": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, stopped_final)


if __name__ == "__main__":
    unittest.main(verbosity=2)
