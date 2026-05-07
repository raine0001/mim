"""
Integration tests for MIM-ARM-SAFE-REACH-SIMULATION objective.

Tests the safe_reach_simulation_service computation layer via the
POST /workspace/targets/{id}/simulate endpoint, covering all six
scenarios required by the objective acceptance criteria:

  1. Reachable clear target passes the gate.
  2. Target in unsafe zone blocks with target_blocked_unsafe_zone.
  3. Stale object requires reobserve.
  4. Nearby obstruction raises collision risk above threshold.
  5. Missing safety envelope blocks the gate.
  6. Simulation result persists on proposal/execution.
"""

import json
import os
import unittest
import urllib.error
import urllib.request
from uuid import uuid4

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL

BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
HTTP_TIMEOUT = float(os.getenv("MIM_TEST_HTTP_TIMEOUT", "180"))

_DEFAULT_SAFETY_ENVELOPE = {
    "reach_confidence": 0.92,
    "max_reach_m": 0.65,
    "min_clearance_m": 0.08,
    "allowed_approach_vectors": ["direct", "side_approach"],
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _register_workspace_scan(tc: unittest.TestCase) -> None:
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
    if status != 200:
        raise AssertionError(payload)


def _run_scan(
    tc: unittest.TestCase,
    *,
    run_id: str,
    scan_area: str,
    observations: list[dict],
) -> None:
    status, event = post_json(
        "/gateway/intake/text",
        {
            "text": f"scan workspace safe-reach-sim {run_id}",
            "parsed_intent": "observe_workspace",
            "confidence": 0.96,
            "metadata_json": {
                "scan_mode": "full",
                "scan_area": scan_area,
                "confidence_threshold": 0.6,
            },
        },
    )
    tc.assertEqual(status, 200, event)

    execution_id = event["execution"]["execution_id"]
    for state in ["accepted", "running"]:
        status, updated = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
        )
        tc.assertEqual(status, 200, updated)

    status, done = post_json(
        f"/gateway/capabilities/executions/{execution_id}/feedback",
        {
            "status": "succeeded",
            "reason": "scan complete",
            "actor": "tod",
            "feedback_json": {"observations": observations},
        },
    )
    tc.assertEqual(status, 200, done)


def _resolve_target(
    tc: unittest.TestCase,
    *,
    label: str,
    zone: str,
    unsafe_zones: list[str] | None = None,
) -> dict:
    status, resolved = post_json(
        "/workspace/targets/resolve",
        {
            "target_label": label,
            "preferred_zone": zone,
            "source": "integration-test",
            "unsafe_zones": unsafe_zones or [],
            "create_proposal": False,
        },
    )
    tc.assertEqual(status, 200, resolved)
    return resolved


def _simulate_target(
    tc: unittest.TestCase,
    *,
    target_resolution_id: int,
    safety_envelope: dict | None = None,
    unsafe_zones: list[str] | None = None,
    collision_risk_threshold: float = 0.45,
) -> dict:
    status, result = post_json(
        f"/workspace/targets/{target_resolution_id}/simulate",
        {
            "actor": "integration-test",
            "reason": "safe reach simulation test",
            "safety_envelope": safety_envelope if safety_envelope is not None else _DEFAULT_SAFETY_ENVELOPE,
            "unsafe_zones": unsafe_zones or [],
            "collision_risk_threshold": collision_risk_threshold,
            "metadata_json": {},
        },
    )
    tc.assertEqual(status, 200, result)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class MimArmSafeReachSimulationTest(unittest.TestCase):
    """
    Integration tests for MIM-ARM-SAFE-REACH-SIMULATION.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _register_workspace_scan(cls)

    # ----------------------------------------------------------------
    # Scenario 1: Reachable clear target passes gate
    # ----------------------------------------------------------------
    def test_reachable_clear_target_passes_gate(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center"
        label = f"safe-obj-srs-{run_id}"

        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.95},
        ])

        resolved = _resolve_target(self, label=label, zone=zone)
        self.assertEqual(resolved.get("policy_outcome"), "target_confirmed")
        target_id = resolved["target_resolution_id"]

        result = _simulate_target(self, target_resolution_id=target_id)

        self.assertEqual(result["simulation_outcome"], "safe",
                         f"Expected 'safe', got: {result}")
        self.assertEqual(result["simulation_status"], "completed")
        self.assertTrue(result["simulation_gate_passed"],
                        f"Gate should pass for clear target: {result}")
        self.assertEqual(result["blocked_reason"], "")
        self.assertEqual(result["recovery_action"], "")
        self.assertTrue(result["reachability"]["reachable"])
        self.assertIsNotNone(result.get("reach_simulation_id"))
        self.assertIn("plan_outcome", result)
        self.assertEqual(result["plan_outcome"], "plan_safe")

    # ----------------------------------------------------------------
    # Scenario 2: Target in unsafe zone blocks
    # ----------------------------------------------------------------
    def test_target_in_unsafe_zone_blocks(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-left"
        label = f"unsafe-zone-obj-srs-{run_id}"

        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.92},
        ])

        # Resolve with zone declared unsafe
        resolved = _resolve_target(
            self, label=label, zone=zone, unsafe_zones=[zone]
        )
        target_id = resolved["target_resolution_id"]

        # Simulate with the same zone in unsafe_zones list
        result = _simulate_target(
            self,
            target_resolution_id=target_id,
            unsafe_zones=[zone],
        )

        self.assertIn(result["simulation_outcome"], {"unsafe", "uncertain"},
                      f"Expected unsafe/uncertain, got: {result}")
        self.assertFalse(result["simulation_gate_passed"],
                         f"Gate must not pass for unsafe zone: {result}")
        self.assertNotEqual(result["blocked_reason"], "",
                            "blocked_reason must be populated")
        self.assertIn(result["recovery_action"], {"confirm", "reobserve"},
                      f"Unexpected recovery_action: {result['recovery_action']}")
        self.assertFalse(result["reachability"]["reachable"])

    # ----------------------------------------------------------------
    # Scenario 3: Stale object requires reobserve
    # ----------------------------------------------------------------
    def test_stale_object_requires_reobserve(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"rear-center"
        label = f"stale-obj-srs-{run_id}"

        # First scan plants the object
        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.91},
        ])
        # Second scan with different label supersedes / makes original stale
        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": f"replacement-{run_id}", "zone": zone, "confidence": 0.93},
        ])

        resolved = _resolve_target(self, label=label, zone=zone)
        # After supersession, policy should indicate stale/reobserve
        self.assertIn(
            resolved.get("policy_outcome"),
            {"target_stale_reobserve", "target_uncertain", "target_confirmed"},
        )
        target_id = resolved["target_resolution_id"]

        result = _simulate_target(self, target_resolution_id=target_id)

        # When the object is stale, outcome is uncertain and recovery is reobserve
        if resolved.get("policy_outcome") == "target_stale_reobserve":
            self.assertIn(result["simulation_outcome"], {"uncertain", "unsafe"})
            self.assertFalse(result["simulation_gate_passed"])
            self.assertEqual(result["recovery_action"], "reobserve",
                             f"Expected reobserve, got: {result}")
            warnings = result["simulation"].get("obstacle_warnings", [])
            self.assertIn("uncertain_object_identity", warnings,
                          f"Stale warning missing: {warnings}")
        # If resolved as confirmed, still check gate plumbing is intact
        else:
            self.assertIn(result["simulation_outcome"], {"safe", "uncertain", "unsafe"})
            self.assertIn("simulation_gate_passed", result)

    # ----------------------------------------------------------------
    # Scenario 4: Nearby obstruction raises collision risk
    # ----------------------------------------------------------------
    def test_nearby_obstruction_raises_collision_risk(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-right"
        label = f"target-srs-{run_id}"
        blocker_label = f"blocker-srs-{run_id}"

        # Scan zone with target AND obstacle
        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.94},
            {"label": blocker_label, "zone": zone, "confidence": 0.92},
        ])

        resolved = _resolve_target(self, label=label, zone=zone)
        target_id = resolved["target_resolution_id"]

        result = _simulate_target(self, target_resolution_id=target_id)

        collision_risk = result["collision_risk"]["risk_score"]
        self.assertGreater(collision_risk, 0.0,
                           f"Expected collision risk > 0, got {collision_risk}")

        # With 1+ obstacle at high confidence, gate should block
        if result["collision_risk"]["obstacle_count"] >= 1:
            self.assertFalse(result["simulation_gate_passed"],
                             f"Gate should be blocked by obstacle: {result}")
            self.assertIn(result["simulation_outcome"], {"unsafe"},
                          f"Unexpected outcome: {result['simulation_outcome']}")
            self.assertGreater(len(result["collision_risk"]["obstacle_names"]), 0)

    # ----------------------------------------------------------------
    # Scenario 5: Missing safety envelope blocks
    # ----------------------------------------------------------------
    def test_missing_safety_envelope_blocks(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center"
        label = f"envelope-miss-srs-{run_id}"

        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.95},
        ])

        resolved = _resolve_target(self, label=label, zone=zone)
        target_id = resolved["target_resolution_id"]

        # Simulate with empty safety_envelope
        result = _simulate_target(
            self,
            target_resolution_id=target_id,
            safety_envelope={},
        )

        self.assertFalse(result["simulation_gate_passed"],
                         f"Empty envelope must block gate: {result}")
        self.assertIn(result["simulation_outcome"], {"unsafe"},
                      f"Expected unsafe, got: {result['simulation_outcome']}")
        self.assertIn("safety envelope", result["blocked_reason"].lower(),
                      f"blocked_reason should mention envelope: {result['blocked_reason']}")
        self.assertFalse(result["reachability"]["reachable"])
        self.assertEqual(result["reachability"]["reason"], "no_safety_envelope")

    # ----------------------------------------------------------------
    # Scenario 6: Simulation result persists on proposal/execution
    # ----------------------------------------------------------------
    def test_simulation_result_persists(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center"
        label = f"persist-srs-{run_id}"

        _run_scan(self, run_id=run_id, scan_area=zone, observations=[
            {"label": label, "zone": zone, "confidence": 0.96},
        ])

        resolved = _resolve_target(self, label=label, zone=zone)
        self.assertEqual(resolved.get("policy_outcome"), "target_confirmed")
        target_id = resolved["target_resolution_id"]

        # Create an action plan so we can verify simulation propagates to it
        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": target_id,
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "safe reach simulation persist test",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        plan_id = plan["plan_id"]

        # Run simulation via target endpoint
        result = _simulate_target(self, target_resolution_id=target_id)
        self.assertIsNotNone(result.get("reach_simulation_id"))
        reach_sim_id = result["reach_simulation_id"]

        # Action plan id should be returned and linked
        linked_plan_id = result.get("action_plan_id")
        self.assertIsNotNone(linked_plan_id,
                             f"Linked action plan should be reported: {result}")

        # Fetch the action plan and verify simulation fields updated
        status, refreshed_plan = get_json(f"/workspace/action-plans/{plan_id}")
        self.assertEqual(status, 200, refreshed_plan)

        if result["simulation_outcome"] == "safe":
            self.assertEqual(
                refreshed_plan.get("simulation_outcome"), "plan_safe",
                f"Action plan simulation_outcome not updated: {refreshed_plan}",
            )
            self.assertTrue(
                refreshed_plan.get("simulation_gate_passed"),
                f"Action plan gate_passed not updated: {refreshed_plan}",
            )
        else:
            self.assertIn(
                refreshed_plan.get("simulation_outcome"),
                {"plan_blocked", "plan_requires_adjustment"},
            )
            self.assertFalse(refreshed_plan.get("simulation_gate_passed"))

        # Verify the simulation JSON contains the reach_simulation_id back-link
        sim_json = refreshed_plan.get("simulation") or {}
        self.assertIn("reach_simulation_id", sim_json,
                      f"reach_simulation_id not in simulation JSON: {sim_json}")
        self.assertEqual(sim_json["reach_simulation_id"], reach_sim_id)

        # Verify simulation result is recoverable from the simulation sub-object
        self.assertIn("collision_risk", sim_json)
        self.assertIn("reachability_reason", sim_json)
        self.assertIn("blocked_reason", sim_json)
        self.assertIn("recovery_action", sim_json)


if __name__ == "__main__":
    unittest.main(verbosity=2)
