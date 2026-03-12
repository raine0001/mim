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


class Objective31SafeReachApproachSimulationTest(unittest.TestCase):
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
                "text": f"scan workspace objective31 {run_id}",
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

    def _create_plan(self, *, target_resolution_id: int, action_type: str = "prepare_reach_plan") -> dict:
        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": target_resolution_id,
                "action_type": action_type,
                "source": "integration-test",
                "notes": "objective31 simulation",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        return plan

    def test_safe_reach_approach_simulation_gate_paths(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        safe_zone = f"front-center-obj31-safe-{run_id}"
        safe_label = f"obj31 safe target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=safe_zone,
            observations=[{"label": safe_label, "zone": safe_zone, "confidence": 0.97}],
        )

        status, safe_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": safe_label,
                "preferred_zone": safe_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, safe_resolve)
        self.assertEqual(safe_resolve.get("policy_outcome"), "target_confirmed")

        safe_plan = self._create_plan(target_resolution_id=safe_resolve["target_resolution_id"])
        safe_plan_id = safe_plan["plan_id"]

        status, approved = post_json(
            f"/workspace/action-plans/{safe_plan_id}/approve",
            {"actor": "operator", "reason": "safe plan approved", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved.get("status"), "approved")

        status, simulated_safe = post_json(
            f"/workspace/action-plans/{safe_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate safe reach",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated_safe)
        self.assertEqual(simulated_safe.get("simulation_outcome"), "plan_safe")
        self.assertTrue(simulated_safe.get("simulation_gate_passed"))
        self.assertTrue(simulated_safe.get("simulation", {}).get("reachable"))

        status, simulation_view = get_json(f"/workspace/action-plans/{safe_plan_id}/simulation")
        self.assertEqual(status, 200, simulation_view)
        self.assertEqual(simulation_view.get("simulation_outcome"), "plan_safe")
        self.assertIn("target_zone", simulation_view.get("simulation", {}))
        self.assertIn("approach_direction", simulation_view.get("simulation", {}))
        self.assertIn("clearance", simulation_view.get("simulation", {}))
        self.assertIn("obstacle_warnings", simulation_view.get("simulation", {}))

        status, queued = post_json(
            f"/workspace/action-plans/{safe_plan_id}/queue",
            {
                "actor": "operator",
                "reason": "queue after safe simulation",
                "requested_executor": "tod",
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued.get("status"), "queued")
        self.assertIsNotNone(queued.get("queued_task_id"))

        obstacle_zone = f"front-left-obj31-obstacle-{run_id}"
        obstacle_target = f"obj31 obstacle target {run_id}"
        obstacle_label = f"obj31 obstacle blocker {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=obstacle_zone,
            observations=[
                {"label": obstacle_target, "zone": obstacle_zone, "confidence": 0.95},
                {"label": obstacle_label, "zone": obstacle_zone, "confidence": 0.91},
            ],
        )

        status, obstacle_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": obstacle_target,
                "preferred_zone": obstacle_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, obstacle_resolve)
        self.assertEqual(obstacle_resolve.get("policy_outcome"), "target_confirmed")

        obstacle_plan = self._create_plan(target_resolution_id=obstacle_resolve["target_resolution_id"])
        obstacle_plan_id = obstacle_plan["plan_id"]
        post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/approve",
            {"actor": "operator", "reason": "approve for simulation", "metadata_json": {}},
        )

        status, simulated_obstacle = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate obstacle path",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated_obstacle)
        self.assertEqual(simulated_obstacle.get("simulation_outcome"), "plan_blocked")
        self.assertFalse(simulated_obstacle.get("simulation_gate_passed"))
        self.assertGreaterEqual(len(simulated_obstacle.get("simulation", {}).get("collision_candidates", [])), 1)

        status, blocked_queue = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/queue",
            {
                "actor": "operator",
                "reason": "queue blocked simulation",
                "requested_executor": "tod",
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, blocked_queue)

        stale_zone = f"rear-center-obj31-stale-{run_id}"
        stale_label = f"obj31 stale target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=stale_zone,
            observations=[{"label": stale_label, "zone": stale_zone, "confidence": 0.9}],
        )
        self._run_scan(
            run_id=run_id,
            scan_area=stale_zone,
            observations=[{"label": f"different stale marker {run_id}", "zone": stale_zone, "confidence": 0.93}],
        )

        status, stale_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": stale_label,
                "preferred_zone": stale_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, stale_resolve)
        self.assertEqual(stale_resolve.get("policy_outcome"), "target_stale_reobserve")

        stale_plan = self._create_plan(target_resolution_id=stale_resolve["target_resolution_id"])
        stale_plan_id = stale_plan["plan_id"]
        status, simulated_stale = post_json(
            f"/workspace/action-plans/{stale_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate stale target",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated_stale)
        self.assertEqual(simulated_stale.get("simulation_outcome"), "plan_requires_adjustment")
        warnings = simulated_stale.get("simulation", {}).get("obstacle_warnings", [])
        self.assertIn("uncertain_object_identity", warnings)

        status, unknown_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": f"obj31 unknown target {run_id}",
                "preferred_zone": f"unknown-zone-obj31-{run_id}",
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, unknown_resolve)

        unknown_plan = self._create_plan(
            target_resolution_id=unknown_resolve["target_resolution_id"],
            action_type="request_confirmation",
        )
        unknown_plan_id = unknown_plan["plan_id"]
        status, simulated_unknown = post_json(
            f"/workspace/action-plans/{unknown_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "simulate unknown zone",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated_unknown)
        self.assertEqual(simulated_unknown.get("simulation_outcome"), "plan_blocked")
        self.assertIn("unknown_zone", simulated_unknown.get("simulation", {}).get("obstacle_warnings", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
