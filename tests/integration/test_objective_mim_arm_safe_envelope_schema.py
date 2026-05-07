"""
Integration tests for MIM-ARM-SAFE-ENVELOPE-SCHEMA (Objective 173).

Covers:
  1. Envelope rows initialize for servos 0–5 from configured limits.
  2. Repeated initialization is idempotent (no duplicate rows).
  3. List endpoint returns all 6 envelope rows.
  4. Single-servo GET endpoint returns correct configured limits.
  5. Probe-attempts endpoint returns empty list (no probes yet).
  6. Dry-run plan endpoint returns plan with no hardware dispatch.
  7. No hardware command path is invoked by any endpoint.

All tests are read-only after initialization.  No arm movement.
"""

import json
import os
import unittest
import urllib.error
import urllib.request
from uuid import uuid4

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL

BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
HTTP_TIMEOUT = float(os.getenv("MIM_TEST_HTTP_TIMEOUT", "60"))

# Expected servo configuration from execution_lane_service.MIM_ARM_SERVO_LIMITS
_EXPECTED_SERVO_LIMITS = {
    0: (0, 180),
    1: (15, 165),
    2: (0, 180),
    3: (0, 180),
    4: (0, 180),
    5: (0, 180),
}

_EXPECTED_SERVO_NAMES = {
    0: "base",
    1: "shoulder",
    2: "elbow",
    3: "wrist_pitch",
    4: "wrist_roll",
    5: "gripper",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def post_json(path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode("utf-8")
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
# Test class
# ---------------------------------------------------------------------------


class MimArmEnvelopeSchemaTest(unittest.TestCase):
    """Integration tests for the servo envelope persistence layer."""

    # Unique arm_id per test run so tests are isolated from prior runs
    arm_id: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.arm_id = f"test-{uuid4().hex[:8]}"

        # Initialize envelopes for this test run's arm_id
        status, payload = post_json(
            f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}"
        )
        if status != 200:
            raise AssertionError(
                f"setUpClass: envelope initialize failed status={status} body={payload}"
            )
        cls._init_payload = payload

    # -----------------------------------------------------------------------
    # Test 1 — Initialization creates 6 rows
    # -----------------------------------------------------------------------

    def test_01_initialize_creates_six_envelopes(self) -> None:
        """Envelope initialization seeds exactly 6 rows (one per servo)."""
        payload = self.__class__._init_payload
        self.assertEqual(payload.get("initialized_count"), 6, payload)
        self.assertEqual(len(payload.get("envelopes", [])), 6, payload)

        servo_ids = {e["servo_id"] for e in payload["envelopes"]}
        self.assertEqual(servo_ids, {0, 1, 2, 3, 4, 5}, payload)

    # -----------------------------------------------------------------------
    # Test 2 — Initialization is idempotent
    # -----------------------------------------------------------------------

    def test_02_initialize_is_idempotent(self) -> None:
        """Calling initialize a second time does not duplicate rows."""
        status, payload = post_json(
            f"/mim/arm/envelopes/initialize?arm_id={self.arm_id}"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("initialized_count"), 6, payload)

        # List should still return 6
        status2, list_payload = get_json(
            f"/mim/arm/envelopes?arm_id={self.arm_id}"
        )
        self.assertEqual(status2, 200, list_payload)
        self.assertEqual(list_payload.get("count"), 6, list_payload)

    # -----------------------------------------------------------------------
    # Test 3 — List endpoint returns all 6 envelopes with correct limits
    # -----------------------------------------------------------------------

    def test_03_list_envelopes_returns_correct_limits(self) -> None:
        """GET /mim/arm/envelopes returns all servos with correct configured limits."""
        status, payload = get_json(f"/mim/arm/envelopes?arm_id={self.arm_id}")
        self.assertEqual(status, 200, payload)

        envelopes = payload.get("envelopes", [])
        self.assertEqual(len(envelopes), 6, payload)

        by_id = {e["servo_id"]: e for e in envelopes}
        for servo_id, (lo, hi) in _EXPECTED_SERVO_LIMITS.items():
            e = by_id.get(servo_id)
            self.assertIsNotNone(e, f"servo_id={servo_id} missing from list")
            self.assertEqual(e["configured_min"], lo, f"servo {servo_id} min mismatch")
            self.assertEqual(e["configured_max"], hi, f"servo {servo_id} max mismatch")
            self.assertEqual(
                e["servo_name"],
                _EXPECTED_SERVO_NAMES[servo_id],
                f"servo {servo_id} name mismatch",
            )

    # -----------------------------------------------------------------------
    # Test 4 — Single-servo GET returns correct data
    # -----------------------------------------------------------------------

    def test_04_get_single_envelope_shoulder(self) -> None:
        """GET /mim/arm/envelopes/1 returns shoulder servo with narrow limits."""
        status, payload = get_json(
            f"/mim/arm/envelopes/1?arm_id={self.arm_id}"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("servo_id"), 1, payload)
        self.assertEqual(payload.get("servo_name"), "shoulder", payload)
        self.assertEqual(payload.get("configured_min"), 15, payload)
        self.assertEqual(payload.get("configured_max"), 165, payload)
        # No probing has occurred — learned values should be None
        self.assertIsNone(payload.get("learned_soft_min"), payload)
        self.assertIsNone(payload.get("learned_soft_max"), payload)
        # Confidence starts at 0
        self.assertEqual(payload.get("confidence"), 0.0, payload)
        # is_stale should be False (never probed)
        self.assertFalse(payload.get("is_stale"), payload)

    # -----------------------------------------------------------------------
    # Test 5 — Single-servo GET for out-of-range ID returns 400
    # -----------------------------------------------------------------------

    def test_05_get_invalid_servo_id_returns_400(self) -> None:
        """GET /mim/arm/envelopes/99 returns HTTP 400."""
        status, payload = get_json(
            f"/mim/arm/envelopes/99?arm_id={self.arm_id}"
        )
        self.assertEqual(status, 400, payload)

    # -----------------------------------------------------------------------
    # Test 6 — Probe-attempts endpoint returns empty list before any probing
    # -----------------------------------------------------------------------

    def test_06_probe_attempts_empty_before_probing(self) -> None:
        """GET /mim/arm/envelopes/{servo_id}/probe-attempts is empty on fresh init."""
        for servo_id in range(6):
            status, payload = get_json(
                f"/mim/arm/envelopes/{servo_id}/probe-attempts?arm_id={self.arm_id}"
            )
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload.get("servo_id"), servo_id, payload)
            self.assertEqual(payload.get("count"), 0, payload)
            self.assertEqual(payload.get("attempts"), [], payload)

    # -----------------------------------------------------------------------
    # Test 7 — Dry-run plan returns plan with no hardware dispatch
    # -----------------------------------------------------------------------

    def test_07_dry_run_plan_no_hardware(self) -> None:
        """GET /mim/arm/envelopes/probe-plan/dry-run returns steps without dispatch."""
        status, payload = get_json(
            f"/mim/arm/envelopes/probe-plan/dry-run?arm_id={self.arm_id}"
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(
            payload.get("hardware_command_issued"),
            "Dry-run plan must not set hardware_command_issued=True",
        )
        self.assertEqual(payload.get("phase"), "dry_run", payload)

        steps = payload.get("steps", [])
        self.assertGreater(len(steps), 0, "Dry-run plan should have steps")

        # Every step must declare would_dispatch=False
        for step in steps:
            self.assertFalse(
                step.get("would_dispatch"),
                f"Step {step} has would_dispatch=True — no hardware should be dispatched",
            )

        # All 6 servo IDs should appear in the plan
        servo_ids_in_plan = {s["servo_id"] for s in steps}
        self.assertEqual(servo_ids_in_plan, {0, 1, 2, 3, 4, 5}, payload)

        # Stop conditions list must be present
        self.assertIn("stop_conditions_checked", payload, payload)
        self.assertGreater(len(payload["stop_conditions_checked"]), 0, payload)

    # -----------------------------------------------------------------------
    # Test 8 — GET /envelopes for unknown arm_id returns 0 envelopes, not 404
    # -----------------------------------------------------------------------

    def test_08_list_unknown_arm_id_returns_empty(self) -> None:
        """GET /mim/arm/envelopes for an arm_id with no rows returns count=0."""
        unknown_arm = f"nonexistent-{uuid4().hex[:8]}"
        status, payload = get_json(f"/mim/arm/envelopes?arm_id={unknown_arm}")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("count"), 0, payload)
        self.assertEqual(payload.get("envelopes"), [], payload)

    # -----------------------------------------------------------------------
    # Test 9 — Gripper servo has correct configured limits
    # -----------------------------------------------------------------------

    def test_09_gripper_servo_limits(self) -> None:
        """Gripper (servo 5) has configured_min=0, configured_max=180."""
        status, payload = get_json(
            f"/mim/arm/envelopes/5?arm_id={self.arm_id}"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("servo_name"), "gripper", payload)
        self.assertEqual(payload.get("configured_min"), 0, payload)
        self.assertEqual(payload.get("configured_max"), 180, payload)


if __name__ == "__main__":
    unittest.main()
