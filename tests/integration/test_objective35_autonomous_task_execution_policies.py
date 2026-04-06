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


class Objective35AutonomousTaskExecutionPoliciesTest(unittest.TestCase):
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

    def _run_scan(self, *, text: str, scan_area: str, observations: list[dict]) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": text,
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

    def _wait_for(self, predicate, timeout_seconds: float = 8.0, step_seconds: float = 0.25) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(step_seconds)
        return False

    def test_objective35_policy_autonomy_throttle_override_and_monitoring(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center-obj35-{run_id}"
        moved_zone = f"rear-center-obj35-{run_id}"

        self._register_workspace_scan()

        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "objective35 baseline",
                "auto_execution_enabled": True,
                "force_manual_approval": False,
                "max_auto_actions_per_minute": 20,
                "cooldown_between_actions_seconds": 0,
                "zone_action_limits": {},
                "auto_safe_confidence_threshold": 0.7,
                "auto_preferred_confidence_threshold": 0.7,
                "low_risk_score_max": 0.3,
                "pause_monitoring_loop": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        # Safe proposal auto-executes: high-confidence target-ready proposal
        label_a = f"obj35-a-{run_id}"
        self._run_scan(
            text=f"objective35 baseline {run_id}",
            scan_area=zone,
            observations=[{"label": label_a, "zone": zone, "confidence": 0.95}],
        )

        status, proposals = get_json("/workspace/proposals")
        self.assertEqual(status, 200, proposals)
        accepted_auto = [
            item
            for item in proposals.get("proposals", [])
            if item.get("proposal_type") in {"confirm_target_ready", "rescan_zone", "monitor_recheck_workspace", "monitor_search_adjacent_zone"}
            and item.get("status") == "accepted"
            and isinstance(item.get("metadata_json"), dict)
            and bool(item.get("metadata_json", {}).get("auto_execution"))
        ]
        self.assertGreaterEqual(len(accepted_auto), 1)

        # Unsafe policy remains pending: moved-object proposals are operator_required
        self._run_scan(
            text=f"objective35 moved source {run_id}",
            scan_area=zone,
            observations=[{"label": f"obj35-move-{run_id}", "zone": zone, "confidence": 0.94}],
        )
        self._run_scan(
            text=f"objective35 moved destination {run_id}",
            scan_area=moved_zone,
            observations=[{"label": f"obj35-move-{run_id}", "zone": moved_zone, "confidence": 0.94}],
        )

        status, pending = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending)
        self.assertTrue(any(item.get("proposal_type") == "verify_moved_object" for item in pending.get("proposals", [])))

        # Policy override works: force manual approval blocks otherwise safe auto-exec
        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "force manual",
                "force_manual_approval": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        label_force = f"obj35-force-manual-{run_id}"
        self._run_scan(
            text=f"objective35 force manual source {run_id}",
            scan_area=zone,
            observations=[{"label": label_force, "zone": zone, "confidence": 0.95}],
        )

        status, pending_after_override = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending_after_override)
        self.assertTrue(any(item.get("proposal_type") == "confirm_target_ready" for item in pending_after_override.get("proposals", [])))

        # Cooldown enforcement works: only first auto-safe proposal auto-executes
        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "cooldown check",
                "force_manual_approval": False,
                "auto_execution_enabled": True,
                "cooldown_between_actions_seconds": 60,
                "max_auto_actions_per_minute": 20,
                "reset_auto_history": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        label_c1 = f"obj35-cooldown-1-{run_id}"
        label_c2 = f"obj35-cooldown-2-{run_id}"
        self._run_scan(
            text=f"objective35 cooldown baseline {run_id}",
            scan_area=zone,
            observations=[
                {"label": label_c1, "zone": zone, "confidence": 0.95},
                {"label": label_c2, "zone": zone, "confidence": 0.95},
            ],
        )

        status, proposals_after_cooldown = get_json("/workspace/proposals")
        self.assertEqual(status, 200, proposals_after_cooldown)
        cooldown_auto = [
            item
            for item in proposals_after_cooldown.get("proposals", [])
            if item.get("proposal_type") == "confirm_target_ready"
            and item.get("status") == "accepted"
            and isinstance(item.get("metadata_json"), dict)
            and bool(item.get("metadata_json", {}).get("auto_execution"))
        ]
        cooldown_pending = [
            item
            for item in proposals_after_cooldown.get("proposals", [])
            if item.get("proposal_type") == "confirm_target_ready" and item.get("status") == "pending"
        ]
        self.assertGreaterEqual(len(cooldown_auto), 1)
        self.assertGreaterEqual(len(cooldown_pending), 1)

        # Monitoring loop still functions
        status, started = post_json(
            "/workspace/monitoring/start",
            {
                "actor": "operator",
                "reason": "objective35 monitor health",
                "trigger_mode": "interval",
                "interval_seconds": 1,
                "freshness_threshold_seconds": 900,
                "cooldown_seconds": 0,
                "max_scan_rate": 30,
                "priority_zones": [zone],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, started)

        def has_scan_progress() -> bool:
            st, body = get_json("/workspace/monitoring")
            return st == 200 and int(body.get("scan_count", 0)) >= 1

        self.assertTrue(self._wait_for(has_scan_progress, timeout_seconds=6), "monitoring loop did not progress")

        post_json(
            "/workspace/monitoring/stop",
            {
                "actor": "operator",
                "reason": "objective35 cleanup",
                "preserve_desired_running": False,
                "metadata_json": {"run_id": run_id},
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
