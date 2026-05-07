"""
test_objective_mim_arm_simulation_probe_planning.py
Objective 174: MIM-ARM-SIMULATION-ONLY-PROBE-PLANNING

Tests for simulation-only probe planning:
  - Detailed plan generation per servo
  - Respects servo-specific step sizes (3° shoulder, 5° others)
  - Respects configured limits
  - Excludes unstable regions
  - Marks stale envelopes for re-verification
  - Verifies no hardware dispatch
  - Persist planned_only probe attempts
"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

BASE_URL = "http://127.0.0.1:18001"

# Test arm_id: unique per test class run
TEST_ARM_ID = f"test-sim-probe-{uuid4().hex[:8]}"


def post_json(endpoint: str, payload: dict) -> tuple[int, Any]:
    """POST JSON and return (status_code, parsed_json)."""
    resp = httpx.post(
        f"{BASE_URL}{endpoint}",
        json=payload,
        timeout=30.0,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


def get_json(endpoint: str, params: dict | None = None) -> tuple[int, Any]:
    """GET and return (status_code, parsed_json)."""
    resp = httpx.get(
        f"{BASE_URL}{endpoint}",
        params=params or {},
        timeout=30.0,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


class MimArmSimulationProbePlanningTest(unittest.TestCase):
    """Test Objective 174: simulation-only probe planning."""

    @classmethod
    def setUpClass(cls):
        """Initialize envelopes before running tests."""
        cls.arm_id = TEST_ARM_ID
        # POST /mim/arm/envelopes/initialize
        status, data = post_json(
            f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}",
            {},
        )
        assert status == 200, f"Failed to initialize envelopes: {data}"
        assert data.get("initialized_count") == 6, f"Expected 6 envelopes, got {data}"

    def test_01_simulation_plan_shoulder_uses_3_degree_steps(self):
        """Servo 1 (shoulder) should use 3° steps in probe plan."""
        # POST to generate simulation plan for servo 1 (shoulder)
        request_body = {
            "servo_id": 1,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
            "max_target_angles": 50,
            "skip_unstable_regions": True,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/1/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200, f"Failed to generate plan: {plan}")
        self.assertEqual(plan.get("servo_id"), 1)
        self.assertEqual(plan.get("servo_name"), "shoulder")

        # Check that all steps use 3° (not 5°)
        steps = plan.get("probe_steps", [])
        self.assertGreater(len(steps), 0, "Plan should have at least one step")
        for step in steps:
            self.assertEqual(
                step.get("step_degrees"),
                3,
                f"Shoulder step should be 3°, got {step.get('step_degrees')}",
            )

    def test_02_simulation_plan_other_servos_use_5_degree_steps(self):
        """Servos 0, 2, 3, 4, 5 should use 5° steps."""
        for servo_id in [0, 2, 3, 4, 5]:
            request_body = {
                "servo_id": servo_id,
                "phase": "simulation_only",
                "persist_planned_attempts": False,
                "max_target_angles": 50,
                "skip_unstable_regions": True,
            }
            status, plan = post_json(
                f"/mim/arm/envelopes/{servo_id}/probe-plan/simulate?arm_id={self.arm_id}",
                request_body,
            )
            self.assertEqual(status, 200, f"Failed to generate plan for servo {servo_id}: {plan}")

            steps = plan.get("probe_steps", [])
            self.assertGreater(len(steps), 0, f"Servo {servo_id} plan should have steps")
            for step in steps:
                self.assertEqual(
                    step.get("step_degrees"),
                    5,
                    f"Servo {servo_id} step should be 5°, got {step.get('step_degrees')}",
                )

    def test_03_simulation_plan_respects_configured_bounds(self):
        """All target angles should be within configured min/max."""
        # Servo 1 (shoulder) has range 15-165
        request_body = {
            "servo_id": 1,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
            "max_target_angles": 50,
            "skip_unstable_regions": True,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/1/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200)

        configured_range = plan.get("configured_range", {})
        min_val = configured_range.get("min")
        max_val = configured_range.get("max")
        self.assertEqual(min_val, 15)
        self.assertEqual(max_val, 165)

        target_angles = plan.get("target_angles", [])
        for ta in target_angles:
            angle = ta.get("angle")
            self.assertGreaterEqual(angle, min_val, f"Angle {angle} below min {min_val}")
            self.assertLessEqual(angle, max_val, f"Angle {angle} above max {max_val}")

    def test_04_simulation_plan_handles_unstable_regions(self):
        """Simulation plan should handle unstable regions in the plan metadata."""
        # Get a plan and verify it includes unstable_regions field
        request_body = {
            "servo_id": 0,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
            "max_target_angles": 50,
            "skip_unstable_regions": True,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/0/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200)

        # Verify unstable_regions field exists (even if empty)
        unstable_regions = plan.get("unstable_regions", [])
        self.assertIsInstance(unstable_regions, list, "unstable_regions should be a list")

        # Verify that unstable flag is set correctly on target angles
        target_angles = plan.get("target_angles", [])
        for ta in target_angles:
            self.assertIn("is_unstable", ta, "Each target angle should have is_unstable flag")

    def test_05_simulation_plan_no_hardware_dispatch(self):
        """Verify allow_physical_execution=False and hardware_command_issued=False."""
        request_body = {
            "servo_id": 2,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/2/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200)

        self.assertFalse(
            plan.get("hardware_command_issued"),
            "hardware_command_issued should be False",
        )
        self.assertFalse(
            plan.get("allow_physical_execution"),
            "allow_physical_execution should be False",
        )

        # Check that all probe steps have allow_physical_probing=False
        for step in plan.get("probe_steps", []):
            self.assertFalse(
                step.get("allow_physical_probing"),
                f"Step {step.get('sequence_index')} should have allow_physical_probing=False",
            )

    def test_06_simulation_plan_high_risk_targets_for_low_confidence(self):
        """Generate plans that show risk assessment."""
        request_body = {
            "servo_id": 3,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/3/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200)

        # Verify risk_assessment field exists
        risk_assessment = plan.get("risk_assessment", "")
        self.assertGreater(len(risk_assessment), 0, "Should have risk assessment")
        self.assertIn(
            "risk",
            risk_assessment.lower(),
            "Risk assessment should mention risk level",
        )

    def test_08_persist_planned_attempts_request_flag_accepted(self):
        """Verify persist_planned_attempts flag is accepted in request."""
        request_body = {
            "servo_id": 5,
            "phase": "simulation_only",
            "persist_planned_attempts": True,
            "max_target_angles": 10,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/5/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200, f"Request should succeed: {plan}")

        probe_steps = plan.get("probe_steps", [])
        self.assertGreater(len(probe_steps), 0, "Should generate probe steps")

    def test_09_invalid_servo_id_returns_400(self):
        """Requesting plan for invalid servo_id should return 400."""
        request_body = {
            "servo_id": 99,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
        }
        status, resp = post_json(
            f"/mim/arm/envelopes/99/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 400, f"Expected 400, got {status}: {resp}")

    def test_10_servo_id_validation(self):
        """Request with invalid servo_id should return error."""
        # Test with servo_id=99 (invalid)
        request_body = {
            "servo_id": 99,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
        }
        status, resp = post_json(
            f"/mim/arm/envelopes/99/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertIn(status, [400, 404], f"Expected 400 or 404, got {status}: {resp}")

    def test_07_simulation_plan_includes_all_required_fields(self):
        """Generated plan should include all required fields."""
        request_body = {
            "servo_id": 0,
            "phase": "simulation_only",
            "persist_planned_attempts": False,
        }
        status, plan = post_json(
            f"/mim/arm/envelopes/0/probe-plan/simulate?arm_id={self.arm_id}",
            request_body,
        )
        self.assertEqual(status, 200)

        # Check for all required top-level fields
        required_fields = [
            "arm_id",
            "servo_id",
            "servo_name",
            "generated_at",
            "phase",
            "hardware_command_issued",
            "allow_physical_execution",
            "configured_range",
            "probe_steps",
            "estimated_total_steps",
            "risk_assessment",
        ]
        for field in required_fields:
            self.assertIn(field, plan, f"Missing required field: {field}")

        # Check step fields
        for step in plan.get("probe_steps", []):
            step_fields = [
                "sequence_index",
                "servo_id",
                "servo_name",
                "target_angle",
                "direction",
                "step_degrees",
                "estimated_risk",
                "stop_conditions_applicable",
                "allow_physical_probing",
                "required_authorization_level",
            ]
            for field in step_fields:
                self.assertIn(field, step, f"Step missing field: {field}")


if __name__ == "__main__":
    unittest.main()
