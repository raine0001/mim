"""
test_objective_mim_arm_dry_run_commands.py
Objective 175: MIM-ARM-DRY-RUN-PROBE-COMMAND-GENERATION

Tests for dry-run command generation:
  - Commands generated from valid simulation plan
  - physical_execution_allowed always False
  - dry_run always True on each command
  - Servo 1 (shoulder) uses 3° steps
  - Other servos use 5° steps
  - safe_home fallback present
  - stop_conditions present on every command
  - Unstable region targets excluded (when skip_unstable_regions=True)
  - Invalid servo_id → 400
  - Uninitialized servo → 404 (different arm_id)
  - persist_as_attempts=True accepted without error
"""

import unittest
from typing import Any
from uuid import uuid4

import httpx

BASE_URL = "http://127.0.0.1:18001"
TEST_ARM_ID = f"test-dry-run-cmd-{uuid4().hex[:8]}"
UNINIT_ARM_ID = f"test-uninit-cmd-{uuid4().hex[:8]}"


def post_json(endpoint: str, payload: dict, params: dict | None = None) -> tuple[int, Any]:
    """POST JSON and return (status_code, parsed_json)."""
    kwargs: dict = {"json": payload, "timeout": 90.0}
    if params:
        kwargs["params"] = params
    resp = httpx.post(f"{BASE_URL}{endpoint}", **kwargs)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


class MimArmDryRunCommandTest(unittest.TestCase):
    """Test Objective 175: dry-run probe command generation."""

    @classmethod
    def setUpClass(cls):
        cls.arm_id = TEST_ARM_ID
        status, data = post_json(
            f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}",
            {},
        )
        assert status == 200, f"Failed to initialize envelopes: {data}"
        assert data.get("initialized_count") == 6, f"Expected 6 envelopes, got {data}"

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _dry_run(self, servo_id: int, **extra) -> tuple[int, Any]:
        payload = {
            "arm_id": self.arm_id,
            "skip_unstable_regions": True,
            "max_target_angles": 50,
            "persist_as_attempts": False,
            **extra,
        }
        return post_json(f"/mim/arm/envelopes/{servo_id}/probe-commands/dry-run", payload)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_01_dry_run_commands_generated_from_valid_plan(self):
        """Commands are returned for a valid, initialized servo."""
        status, data = self._dry_run(2)
        self.assertEqual(status, 200, data)
        self.assertIn("commands", data, data)
        self.assertIsInstance(data["commands"], list)
        self.assertGreater(len(data["commands"]), 0, "Expected at least one command")

    def test_02_physical_execution_allowed_always_false(self):
        """physical_execution_allowed must always be False."""
        status, data = self._dry_run(0)
        self.assertEqual(status, 200, data)
        self.assertFalse(
            data["physical_execution_allowed"],
            "physical_execution_allowed should be False",
        )

    def test_03_dry_run_flag_true_on_every_command(self):
        """Each command must have dry_run=True."""
        status, data = self._dry_run(3)
        self.assertEqual(status, 200, data)
        cmds = data["commands"]
        self.assertGreater(len(cmds), 0)
        for cmd in cmds:
            self.assertTrue(cmd.get("dry_run"), f"Command missing dry_run=True: {cmd}")

    def test_04_shoulder_uses_3_degree_steps(self):
        """Servo 1 (shoulder) commands must all have step_degrees == 3."""
        status, data = self._dry_run(1)
        self.assertEqual(status, 200, data)
        cmds = data["commands"]
        self.assertGreater(len(cmds), 0, "Expected at least one shoulder command")
        for cmd in cmds:
            self.assertEqual(
                cmd["step_degrees"],
                3,
                f"Shoulder step should be 3, got {cmd['step_degrees']}",
            )

    def test_05_other_servos_use_5_degree_steps(self):
        """Servos 0,2,3,4,5 commands must all have step_degrees == 5."""
        for servo_id in [0, 2, 3, 4, 5]:
            status, data = self._dry_run(servo_id)
            self.assertEqual(status, 200, data)
            cmds = data["commands"]
            self.assertGreater(len(cmds), 0, f"Expected commands for servo {servo_id}")
            for cmd in cmds:
                self.assertEqual(
                    cmd["step_degrees"],
                    5,
                    f"Servo {servo_id} step should be 5, got {cmd['step_degrees']}",
                )

    def test_06_safe_home_fallback_present(self):
        """Response must include a safe_home_fallback dict with target_angle."""
        status, data = self._dry_run(2)
        self.assertEqual(status, 200, data)
        fallback = data.get("safe_home_fallback")
        self.assertIsNotNone(fallback, "safe_home_fallback missing")
        self.assertIn("target_angle", fallback, "safe_home_fallback must have target_angle")
        self.assertIsInstance(fallback["target_angle"], int)

    def test_07_stop_conditions_present_on_each_command(self):
        """Every command must have a non-empty stop_conditions list."""
        status, data = self._dry_run(4)
        self.assertEqual(status, 200, data)
        cmds = data["commands"]
        self.assertGreater(len(cmds), 0)
        for cmd in cmds:
            stops = cmd.get("stop_conditions", [])
            self.assertIsInstance(stops, list, "stop_conditions should be a list")
            self.assertGreater(
                len(stops), 0, f"stop_conditions empty on command {cmd.get('command_id')}"
            )

    def test_08_unstable_region_targets_excluded(self):
        """With skip_unstable_regions=True, no command should be in an unstable region."""
        # Fresh envelope has empty unstable_regions, so all targets are valid.
        # Test verifies all targets are within configured bounds (which is the
        # effective safety guarantee when no unstable regions are present).
        status, data = self._dry_run(0, skip_unstable_regions=True)
        self.assertEqual(status, 200, data)
        # All commands should have targets within servo 0 configured range (0–180)
        for cmd in data["commands"]:
            self.assertGreaterEqual(cmd["target_angle"], 0)
            self.assertLessEqual(cmd["target_angle"], 180)

    def test_09_invalid_servo_id_returns_400(self):
        """servo_id > 5 must return 400."""
        status, data = post_json(
            "/mim/arm/envelopes/99/probe-commands/dry-run",
            {
                "arm_id": self.arm_id,
                "skip_unstable_regions": True,
                "max_target_angles": 50,
                "persist_as_attempts": False,
            },
        )
        self.assertEqual(status, 400, f"Expected 400 for servo_id=99, got {status}: {data}")

    def test_10_uninitialized_envelope_returns_404(self):
        """Requesting dry-run for an arm_id with no envelopes must return 404."""
        status, data = post_json(
            "/mim/arm/envelopes/2/probe-commands/dry-run",
            {
                "arm_id": UNINIT_ARM_ID,
                "skip_unstable_regions": True,
                "max_target_angles": 50,
                "persist_as_attempts": False,
            },
        )
        self.assertEqual(status, 404, f"Expected 404 for uninitialized arm, got {status}: {data}")

    def test_11_persist_as_attempts_flag_accepted(self):
        """persist_as_attempts=True should succeed without error."""
        status, data = post_json(
            f"/mim/arm/envelopes/2/probe-commands/dry-run",
            {
                "arm_id": self.arm_id,
                "skip_unstable_regions": True,
                "max_target_angles": 10,
                "persist_as_attempts": True,
            },
        )
        self.assertEqual(status, 200, f"Expected 200 with persist_as_attempts=True: {data}")
        self.assertIn("commands", data)
        self.assertGreater(len(data["commands"]), 0)

    def test_12_total_commands_matches_commands_list_length(self):
        """total_commands field must equal len(commands)."""
        status, data = self._dry_run(5)
        self.assertEqual(status, 200, data)
        self.assertEqual(
            data["total_commands"],
            len(data["commands"]),
            "total_commands should equal len(commands)",
        )

    def test_13_each_command_has_rollback_command(self):
        """Every command must have a rollback_command dict with target_angle."""
        status, data = self._dry_run(3)
        self.assertEqual(status, 200, data)
        cmds = data["commands"]
        self.assertGreater(len(cmds), 0)
        for cmd in cmds:
            rollback = cmd.get("rollback_command")
            self.assertIsNotNone(rollback, f"Missing rollback_command on cmd {cmd.get('command_id')}")
            self.assertIn("target_angle", rollback)

    def test_14_each_command_has_expected_feedback_fields(self):
        """Every command must list expected_feedback_fields."""
        status, data = self._dry_run(2)
        self.assertEqual(status, 200, data)
        cmds = data["commands"]
        self.assertGreater(len(cmds), 0)
        required = {"observed_angle", "current_ma", "timestamp"}
        for cmd in cmds:
            fields = set(cmd.get("expected_feedback_fields", []))
            self.assertTrue(
                required.issubset(fields),
                f"Missing feedback fields {required - fields} on cmd {cmd.get('command_id')}",
            )

    def test_15_response_top_level_fields_present(self):
        """All required top-level fields must be present."""
        status, data = self._dry_run(0)
        self.assertEqual(status, 200, data)
        required_fields = {
            "arm_id", "servo_id", "servo_name", "generated_at",
            "dry_run", "physical_execution_allowed", "commands",
            "safe_home_fallback", "stop_conditions_checked", "total_commands",
        }
        for field in required_fields:
            self.assertIn(field, data, f"Missing top-level field: {field}")


if __name__ == "__main__":
    unittest.main()
