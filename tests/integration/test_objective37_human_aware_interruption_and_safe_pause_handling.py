import json
import os
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


class Objective37HumanAwareInterruptionTest(unittest.TestCase):
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

    def _run_scan(self, *, run_id: str, observations: list[dict], scan_area: str) -> int:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace objective37 {run_id}",
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
        return execution_id

    def test_human_aware_interruption_pause_resume_stop_flow(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        target_zone = f"front-center-obj37-{run_id}"
        target_label = f"obj37 target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=target_zone,
            observations=[{"label": target_label, "zone": target_zone, "confidence": 0.97}],
        )

        status, resolved = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": target_label,
                "preferred_zone": target_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolved)

        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": resolved["target_resolution_id"],
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "objective37 plan",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        plan_id = plan["plan_id"]

        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {"actor": "operator", "reason": "approve objective37", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)

        status, simulated = post_json(
            f"/workspace/action-plans/{plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate objective37",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated)
        self.assertEqual(simulated.get("simulation_outcome"), "plan_safe")

        status, executed = post_json(
            f"/workspace/action-plans/{plan_id}/execute",
            {
                "actor": "operator",
                "reason": "execute objective37",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, executed)
        execution_id = int(executed["execution_id"])

        status, proposals = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, proposals)
        proposal_id = int(proposals["proposals"][0]["proposal_id"])

        status, chain = post_json(
            "/workspace/chains",
            {
                "actor": "workspace",
                "reason": "objective37 linked chain",
                "chain_type": "proposal_sequence",
                "proposal_ids": [proposal_id],
                "source": "objective37",
                "step_policy_json": {},
                "stop_on_failure": True,
                "cooldown_seconds": 0,
                "requires_approval": False,
                "metadata_json": {"active_execution_id": execution_id},
            },
        )
        self.assertEqual(status, 200, chain)
        chain_id = int(chain["chain_id"])

        status, paused = post_json(
            f"/workspace/executions/{execution_id}/pause",
            {
                "actor": "operator",
                "source": "safety_sensor",
                "interruption_type": "human_detected_in_workspace",
                "reason": "human entered workspace",
                "metadata_json": {"sensor": "camera-a"},
            },
        )
        self.assertEqual(status, 200, paused)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["interruption"]["interruption_type"], "human_detected_in_workspace")

        status, chain_after_pause = get_json(f"/workspace/chains/{chain_id}")
        self.assertEqual(status, 200, chain_after_pause)
        self.assertEqual(chain_after_pause["status"], "paused")

        status, operator_inbox = get_json("/operator/inbox")
        self.assertEqual(status, 200, operator_inbox)
        self.assertGreaterEqual(operator_inbox["counts"].get("paused", 0), 1)

        status, interruptions = get_json(f"/workspace/interruptions?execution_id={execution_id}")
        self.assertEqual(status, 200, interruptions)
        self.assertGreaterEqual(len(interruptions.get("interruptions", [])), 1)

        status, invalid_resume = post_json(
            f"/workspace/executions/{execution_id}/resume",
            {
                "actor": "operator",
                "source": "operator",
                "reason": "try resume without restore",
                "safety_ack": True,
                "conditions_restored": False,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, invalid_resume)

        status, resumed = post_json(
            f"/workspace/executions/{execution_id}/resume",
            {
                "actor": "operator",
                "source": "operator",
                "reason": "workspace cleared",
                "safety_ack": True,
                "conditions_restored": True,
                "metadata_json": {"clearance_check": "passed"},
            },
        )
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed["status"], "running")

        status, chain_after_resume = get_json(f"/workspace/chains/{chain_id}")
        self.assertEqual(status, 200, chain_after_resume)
        self.assertEqual(chain_after_resume["status"], "active")

        status, stopped = post_json(
            f"/workspace/executions/{execution_id}/stop",
            {
                "actor": "operator",
                "source": "safety_sensor",
                "interruption_type": "new_obstacle_detected",
                "reason": "obstacle appeared",
                "metadata_json": {"sensor": "lidar"},
            },
        )
        self.assertEqual(status, 200, stopped)
        self.assertEqual(stopped["status"], "blocked")

        status, plan_after_stop = get_json(f"/workspace/action-plans/{plan_id}")
        self.assertEqual(status, 200, plan_after_stop)
        self.assertEqual(plan_after_stop["status"], "aborted")

        status, chain_after_stop = get_json(f"/workspace/chains/{chain_id}")
        self.assertEqual(status, 200, chain_after_stop)
        self.assertEqual(chain_after_stop["status"], "canceled")

        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        actions = {entry.get("action") for entry in journal}
        self.assertIn("workspace_execution_pause", actions)
        self.assertIn("workspace_execution_resume", actions)
        self.assertIn("workspace_execution_stop", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
