"""
test_objective_mim_arm_supervised_micro_step_execution.py
Objective 177: MIM-ARM-SUPERVISED-MICRO-STEP-EXECUTION-STUB

Covers the full lifecycle of one operator-triggered supervised physical
micro-step execution stub.  No hardware movement is dispatched in this
objective.

Tests:
  01 — Execution created from approved authorization
  02 — Execution blocked if authorization is pending (not approved)
  03 — Execution blocked if authorization is rejected
  04 — Execution blocked if authorization is expired
  05 — Execution blocked if authorization is already consumed
  06 — Execution log contains required start entries
  07 — physical_movement_dispatched is always False
  08 — Authorization status transitions to consumed after execution begins
  09 — Safe-home trigger transitions execution status and appends log entry
  10 — Safe-home trigger blocked if execution is not in executing/pending status
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from core.db import SessionLocal, engine
from core.models import ArmEnvelopeProbeAttempt, ArmProbeAuthorization, ArmServoEnvelope

BASE_URL = "http://127.0.0.1:18001"
TEST_ARM_ID = f"test-exec-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def post_json(endpoint: str, payload: dict, params: dict | None = None) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {"json": payload, "timeout": 90.0}
    if params:
        kwargs["params"] = params
    resp = httpx.post(f"{BASE_URL}{endpoint}", **kwargs)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


def get_json(endpoint: str, params: dict | None = None) -> tuple[int, Any]:
    kwargs: dict[str, Any] = {"timeout": 90.0}
    if params:
        kwargs["params"] = params
    resp = httpx.get(f"{BASE_URL}{endpoint}", **kwargs)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------

async def _set_envelope_ready(arm_id: str, servo_id: int) -> None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmServoEnvelope).where(
                ArmServoEnvelope.arm_id == arm_id,
                ArmServoEnvelope.servo_id == servo_id,
            )
        )
        envelope = result.scalar_one()
        envelope.confidence = 0.9
        envelope.last_verified_at = datetime.now(timezone.utc)
        envelope.stale_after_seconds = 86400
        envelope.unstable_regions = []
        envelope.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def _expire_authorization(authorization_id: str) -> None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmProbeAuthorization).where(
                ArmProbeAuthorization.authorization_id == authorization_id
            )
        )
        row = result.scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def _force_authorization_status(authorization_id: str, status: str) -> None:
    """Directly set authorization status for negative-path tests."""
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmProbeAuthorization).where(
                ArmProbeAuthorization.authorization_id == authorization_id
            )
        )
        row = result.scalar_one()
        row.authorization_status = status
        if status in {"consumed", "rejected", "expired"}:
            row.physical_execution_allowed = False
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def _dispose_engine() -> None:
    await engine.dispose()


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class MimArmSupervisedMicroStepExecutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls._loop)
        # Dispose any asyncpg connections from prior test classes so this class
        # starts with a clean pool bound to its own event loop.
        cls._loop.run_until_complete(_dispose_engine())
        cls.arm_id = TEST_ARM_ID
        status, data = post_json(f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}", {})
        assert status == 200, data
        assert data.get("initialized_count") == 6, data

    @classmethod
    def tearDownClass(cls):
        cls._loop.close()

    def _run_async(self, coro):
        """Run a coroutine in the class event loop."""
        return self._loop.run_until_complete(coro)

    def _create_persisted_dry_run_command(self, servo_id: int = 2) -> str:
        """Helper: ensure envelope ready and generate a persisted dry-run command."""
        self._run_async(_set_envelope_ready(self.arm_id, servo_id))
        status, data = post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-commands/dry-run",
            {
                "arm_id": self.arm_id,
                "skip_unstable_regions": True,
                "max_target_angles": 10,
                "persist_as_attempts": True,
            },
        )
        self.assertEqual(status, 200, data)
        self.assertGreater(len(data.get("commands", [])), 0, data)
        return data["commands"][0]["command_id"]

    def _request_authorization(
        self, servo_id: int, command_id: str, expires_in_seconds: int = 300
    ) -> tuple[int, Any]:
        return post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-authorizations/request",
            {
                "arm_id": self.arm_id,
                "dry_run_command_id": command_id,
                "operator_id": "operator.test",
                "expires_in_seconds": expires_in_seconds,
            },
        )

    def _approve_authorization(self, authorization_id: str) -> tuple[int, Any]:
        return post_json(
            f"/mim/arm/probe-authorizations/{authorization_id}/approve",
            {"authorized_by": "supervisor.test"},
        )

    def _execute(self, authorization_id: str, operator_id: str = "operator.test") -> tuple[int, Any]:
        return post_json(
            f"/mim/arm/probe-authorizations/{authorization_id}/execute",
            {"operator_id": operator_id},
        )

    def _create_approved_authorization(self, servo_id: int = 2) -> str:
        """Full setup helper: dry-run → request → approve → return authorization_id."""
        command_id = self._create_persisted_dry_run_command(servo_id=servo_id)
        status, auth = self._request_authorization(servo_id, command_id)
        self.assertEqual(status, 200, auth)
        auth_id = auth["authorization_id"]
        status, approved = self._approve_authorization(auth_id)
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["authorization_status"], "approved")
        return auth_id

    # -----------------------------------------------------------------------
    # Tests
    # -----------------------------------------------------------------------

    def test_01_execution_created_from_approved_authorization(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        status, data = self._execute(auth_id)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["authorization_id"], auth_id)
        self.assertIn("execution_id", data)
        self.assertIsNotNone(data["execution_id"])
        self.assertEqual(data["execution_status"], "executing")

    def test_02_execution_blocked_if_authorization_is_pending(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, auth = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, auth)
        auth_id = auth["authorization_id"]
        # Do NOT approve — attempt to execute pending authorization
        status, data = self._execute(auth_id)
        self.assertEqual(status, 400, data)
        self.assertIn("pending", data.get("detail", "").lower())

    def test_03_execution_blocked_if_authorization_is_rejected(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, auth = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, auth)
        auth_id = auth["authorization_id"]
        # Reject then try to execute
        status, _ = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/reject",
            {"rejected_by": "supervisor.test", "reason": "test rejection"},
        )
        self.assertEqual(status, 200)
        status, data = self._execute(auth_id)
        self.assertEqual(status, 400, data)
        self.assertIn("rejected", data.get("detail", "").lower())

    def test_04_execution_blocked_if_authorization_is_expired(self):
        command_id = self._create_persisted_dry_run_command(servo_id=3)
        status, auth = self._request_authorization(3, command_id, expires_in_seconds=300)
        self.assertEqual(status, 200, auth)
        auth_id = auth["authorization_id"]
        # Approve then manually expire
        self._approve_authorization(auth_id)
        self._run_async(_expire_authorization(auth_id))
        status, data = self._execute(auth_id)
        self.assertEqual(status, 400, data)
        self.assertIn("expir", data.get("detail", "").lower())

    def test_05_execution_blocked_if_authorization_already_consumed(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        # First execution — should succeed
        status, first = self._execute(auth_id)
        self.assertEqual(status, 200, first)
        # Second execution — authorization now consumed
        status, data = self._execute(auth_id)
        self.assertEqual(status, 400, data)
        self.assertIn("consumed", data.get("detail", "").lower())

    def test_06_execution_log_contains_required_start_entries(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        status, data = self._execute(auth_id)
        self.assertEqual(status, 200, data)
        log_events = [entry["event"] for entry in data.get("log_entries", [])]
        self.assertIn("execution_started", log_events)
        self.assertIn("authorization_consumed", log_events)
        self.assertIn("physical_movement_dispatched", log_events)

    def test_07_physical_movement_dispatched_is_always_false(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        status, data = self._execute(auth_id)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["physical_movement_dispatched"])
        # Verify via GET endpoint as well
        exec_id = data["execution_id"]
        status2, fetched = get_json(f"/mim/arm/supervised-executions/{exec_id}")
        self.assertEqual(status2, 200, fetched)
        self.assertFalse(fetched["physical_movement_dispatched"])

    def test_08_authorization_consumed_after_execution_begins(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        status, data = self._execute(auth_id)
        self.assertEqual(status, 200, data)
        # Check authorization is now consumed via GET
        status2, auth_data = get_json(f"/mim/arm/probe-authorizations/{auth_id}")
        self.assertEqual(status2, 200, auth_data)
        self.assertEqual(auth_data["authorization_status"], "consumed")
        self.assertFalse(auth_data["physical_execution_allowed"])

    def test_09_safe_home_trigger_transitions_status_and_appends_log(self):
        auth_id = self._create_approved_authorization(servo_id=2)
        status, exec_data = self._execute(auth_id)
        self.assertEqual(status, 200, exec_data)
        exec_id = exec_data["execution_id"]

        # Trigger safe-home
        status2, sh_data = post_json(
            f"/mim/arm/supervised-executions/{exec_id}/safe-home",
            {"operator_id": "operator.safety", "reason": "test_safe_home"},
        )
        self.assertEqual(status2, 200, sh_data)
        self.assertEqual(sh_data["execution_status"], "safe_home_triggered")
        self.assertTrue(sh_data["safe_home_triggered"])
        self.assertIsNotNone(sh_data["safe_home_triggered_at"])
        # Log must contain safe_home_triggered entry
        log_events = [entry["event"] for entry in sh_data.get("log_entries", [])]
        self.assertIn("safe_home_triggered", log_events)
        # Stub: no hardware
        self.assertFalse(sh_data["physical_movement_dispatched"])

    def test_10_safe_home_blocked_if_execution_not_in_executing_status(self):
        auth_id = self._create_approved_authorization(servo_id=3)
        status, exec_data = self._execute(auth_id)
        self.assertEqual(status, 200, exec_data)
        exec_id = exec_data["execution_id"]

        # Trigger safe-home once — valid
        status2, _ = post_json(
            f"/mim/arm/supervised-executions/{exec_id}/safe-home",
            {"operator_id": "operator.safety", "reason": "first_trigger"},
        )
        self.assertEqual(status2, 200)

        # Second safe-home trigger — execution already in safe_home_triggered status
        status3, data3 = post_json(
            f"/mim/arm/supervised-executions/{exec_id}/safe-home",
            {"operator_id": "operator.safety", "reason": "second_attempt"},
        )
        self.assertEqual(status3, 400, data3)
        self.assertIn("safe_home", data3.get("detail", "").lower())


if __name__ == "__main__":
    unittest.main()
