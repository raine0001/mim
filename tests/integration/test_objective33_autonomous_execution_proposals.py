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


class Objective33AutonomousExecutionProposalsTest(unittest.TestCase):
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
                "text": f"scan workspace objective33 {run_id}",
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

    def test_autonomous_execution_proposal_accept_reject_flow(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        status, policy = get_json("/workspace/execution-proposals/policy")
        self.assertEqual(status, 200, policy)
        self.assertIn("allowed_capabilities", policy)

        safe_zone = f"front-center-obj33-safe-{run_id}"
        safe_label = f"obj33 safe target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=safe_zone,
            observations=[{"label": safe_label, "zone": safe_zone, "confidence": 0.97}],
        )

        status, resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": safe_label,
                "preferred_zone": safe_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolve)

        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": resolve["target_resolution_id"],
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "objective33 plan",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        plan_id = plan["plan_id"]

        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {"actor": "operator", "reason": "approve objective33", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)

        status, simulated = post_json(
            f"/workspace/action-plans/{plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate objective33",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated)
        self.assertEqual(simulated.get("simulation_outcome"), "plan_safe")

        status, proposed = post_json(
            f"/workspace/action-plans/{plan_id}/propose-execution",
            {"actor": "workspace", "reason": "objective33 autonomous suggestion", "metadata_json": {}},
        )
        self.assertEqual(status, 200, proposed)
        proposal_id = proposed["proposal_id"]

        status, listed = get_json("/workspace/execution-proposals")
        self.assertEqual(status, 200, listed)
        self.assertTrue(any(item.get("proposal_id") == proposal_id for item in listed.get("proposals", [])))

        status, accepted = post_json(
            f"/workspace/execution-proposals/{proposal_id}/accept",
            {
                "actor": "operator",
                "reason": "accept autonomous proposal",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("proposal_status"), "accepted")
        self.assertEqual(accepted.get("plan_execution", {}).get("status"), "executing")

        second_zone = f"rear-center-obj33-second-{run_id}"
        second_label = f"obj33 second target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=second_zone,
            observations=[{"label": second_label, "zone": second_zone, "confidence": 0.96}],
        )

        status, second_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": second_label,
                "preferred_zone": second_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, second_resolve)

        status, second_plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": second_resolve["target_resolution_id"],
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "objective33 second plan",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, second_plan)
        second_plan_id = second_plan["plan_id"]

        post_json(
            f"/workspace/action-plans/{second_plan_id}/approve",
            {"actor": "operator", "reason": "approve second", "metadata_json": {}},
        )
        post_json(
            f"/workspace/action-plans/{second_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate second",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )

        status, second_proposed = post_json(
            f"/workspace/action-plans/{second_plan_id}/propose-execution",
            {"actor": "workspace", "reason": "objective33 reject path", "metadata_json": {}},
        )
        self.assertEqual(status, 200, second_proposed)

        second_proposal_id = second_proposed["proposal_id"]
        status, rejected = post_json(
            f"/workspace/execution-proposals/{second_proposal_id}/reject",
            {
                "actor": "operator",
                "reason": "reject autonomous proposal",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected.get("status"), "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
