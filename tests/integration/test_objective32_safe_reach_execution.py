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


class Objective32SafeReachExecutionTest(unittest.TestCase):
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
                "text": f"scan workspace objective32 {run_id}",
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

    def _create_plan(self, *, target_resolution_id: int) -> dict:
        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": target_resolution_id,
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "objective32 execution",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        return plan

    def _resolve_target(self, *, label: str, zone: str) -> dict:
        status, resolved = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": label,
                "preferred_zone": zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolved)
        return resolved

    def _approve_and_simulate_safe(self, *, plan_id: int) -> dict:
        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {"actor": "operator", "reason": "approved", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)

        status, simulated = post_json(
            f"/workspace/action-plans/{plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate for execution",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated)
        self.assertEqual(simulated.get("simulation_outcome"), "plan_safe")
        return simulated

    def test_safe_reach_execution_paths(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        safe_zone = f"front-center-obj32-safe-{run_id}"
        safe_label = f"obj32 safe target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=safe_zone,
            observations=[{"label": safe_label, "zone": safe_zone, "confidence": 0.97}],
        )

        safe_resolve = self._resolve_target(label=safe_label, zone=safe_zone)
        safe_plan = self._create_plan(target_resolution_id=safe_resolve["target_resolution_id"])
        safe_plan_id = safe_plan["plan_id"]

        status, unapproved_execute = post_json(
            f"/workspace/action-plans/{safe_plan_id}/execute",
            {
                "actor": "operator",
                "reason": "try execute without approval",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, unapproved_execute)

        self._approve_and_simulate_safe(plan_id=safe_plan_id)

        status, executed = post_json(
            f"/workspace/action-plans/{safe_plan_id}/execute",
            {
                "actor": "operator",
                "reason": "execute safe simulated plan",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, executed)
        self.assertEqual(executed.get("status"), "executing")
        self.assertEqual(executed.get("execution_capability"), "reach_target")
        self.assertIn("execution_id", executed)

        execution_id = executed["execution_id"]
        for state in ["accepted", "running", "succeeded"]:
            status, feedback = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": f"objective32 {state}",
                    "actor": "tod",
                    "feedback_json": {"objective32_state": state},
                },
            )
            self.assertEqual(status, 200, feedback)
            self.assertEqual(feedback.get("status"), state)

        status, feedback_view = get_json(f"/gateway/capabilities/executions/{execution_id}/feedback")
        self.assertEqual(status, 200, feedback_view)
        self.assertEqual(feedback_view.get("status"), "succeeded")

        obstacle_zone = f"front-left-obj32-obstacle-{run_id}"
        obstacle_target = f"obj32 blocked target {run_id}"
        obstacle_label = f"obj32 blocker {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=obstacle_zone,
            observations=[
                {"label": obstacle_target, "zone": obstacle_zone, "confidence": 0.95},
                {"label": obstacle_label, "zone": obstacle_zone, "confidence": 0.94},
            ],
        )

        obstacle_resolve = self._resolve_target(label=obstacle_target, zone=obstacle_zone)
        obstacle_plan = self._create_plan(target_resolution_id=obstacle_resolve["target_resolution_id"])
        obstacle_plan_id = obstacle_plan["plan_id"]

        status, approved = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/approve",
            {"actor": "operator", "reason": "approve for simulation", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)

        status, blocked_sim = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate blocked",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, blocked_sim)
        self.assertEqual(blocked_sim.get("simulation_outcome"), "plan_blocked")

        status, blocked_execute = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/execute",
            {
                "actor": "operator",
                "reason": "should be blocked",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, blocked_execute)

        abort_zone = f"rear-center-obj32-abort-{run_id}"
        abort_label = f"obj32 abort target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=abort_zone,
            observations=[{"label": abort_label, "zone": abort_zone, "confidence": 0.96}],
        )

        abort_resolve = self._resolve_target(label=abort_label, zone=abort_zone)
        abort_plan = self._create_plan(target_resolution_id=abort_resolve["target_resolution_id"])
        abort_plan_id = abort_plan["plan_id"]
        self._approve_and_simulate_safe(plan_id=abort_plan_id)

        status, abort_execute = post_json(
            f"/workspace/action-plans/{abort_plan_id}/execute",
            {
                "actor": "operator",
                "reason": "start then abort",
                "requested_executor": "tod",
                "capability_name": "arm_move_safe",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, abort_execute)

        abort_execution_id = abort_execute["execution_id"]
        status, aborted = post_json(
            f"/workspace/action-plans/{abort_plan_id}/abort",
            {
                "actor": "operator",
                "reason": "new obstacle detected",
                "metadata_json": {"trigger": "new_obstacle_detected"},
            },
        )
        self.assertEqual(status, 200, aborted)
        self.assertEqual(aborted.get("status"), "aborted")
        self.assertEqual(aborted.get("abort_status"), "aborted")

        status, aborted_feedback = get_json(f"/gateway/capabilities/executions/{abort_execution_id}/feedback")
        self.assertEqual(status, 200, aborted_feedback)
        self.assertEqual(aborted_feedback.get("status"), "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
