import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import datetime
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


class Objective39PolicyBasedPrioritySelectionTest(unittest.TestCase):
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

    def test_priority_policy_and_next_scheduler(self) -> None:
        run_id = uuid4().hex[:8]
        zone_a = f"front-center-obj39-{run_id}"
        zone_b = f"rear-center-obj39-{run_id}"
        label = f"obj39-target-{run_id}"

        self._register_workspace_scan()

        status, policy_before = get_json("/workspace/proposals/priority-policy")
        self.assertEqual(status, 200, policy_before)
        self.assertIn("weights", policy_before)

        status, policy_update = post_json(
            "/workspace/proposals/priority-policy",
            {
                "actor": "operator",
                "reason": "objective39 test policy tuning",
                "weights": {
                    "urgency": 0.2,
                    "confidence": 0.1,
                    "safety": 0.05,
                    "operator_preference": 0.6,
                    "zone_importance": 0.05,
                    "age": 0.0,
                },
                "operator_preference": {
                    "verify_moved_object": 1.0,
                    "confirm_target_ready": 0.0,
                },
                "age_saturation_minutes": 60,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, policy_update)
        self.assertTrue(policy_update.get("updated"))

        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "keep proposals pending for scheduler",
                "force_manual_approval": True,
                "auto_execution_enabled": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        self._run_scan(
            text=f"objective39 baseline {run_id}",
            scan_area=zone_a,
            observations=[{"label": label, "zone": zone_a, "confidence": 0.95}],
        )
        self._run_scan(
            text=f"objective39 moved {run_id}",
            scan_area=zone_b,
            observations=[{"label": label, "zone": zone_b, "confidence": 0.94}],
        )

        status, pending = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending)
        proposals = pending.get("proposals", [])
        self.assertGreaterEqual(len(proposals), 1)

        for row in proposals:
            self.assertIn("priority_score", row)
            self.assertIn("priority_reason", row)

        def parse_dt(raw: str) -> datetime:
            candidate = raw.replace("Z", "+00:00")
            return datetime.fromisoformat(candidate)

        expected = sorted(
            proposals,
            key=lambda item: (
                float(item.get("priority_score", 0.0)),
                float(item.get("confidence", 0.0)),
                parse_dt(str(item.get("created_at"))),
                int(item.get("proposal_id", 0)),
            ),
            reverse=True,
        )[0]

        status, nxt = get_json("/workspace/proposals/next?actor=objective39-test&reason=scheduler-check&status=pending")
        self.assertEqual(status, 200, nxt)
        self.assertTrue(nxt.get("selected"))
        proposal = nxt.get("proposal") or {}
        self.assertEqual(int(proposal.get("proposal_id", 0)), int(expected.get("proposal_id", 0)))
        self.assertGreater(float(proposal.get("priority_score", 0.0)), 0.0)
        self.assertTrue(str(proposal.get("priority_reason", "")).strip())
        self.assertIn("priority_breakdown", nxt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
