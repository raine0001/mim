"""
test_objective_mim_arm_supervised_micro_step_authorization.py
Objective 176: MIM-ARM-SUPERVISED-MICRO-STEP-AUTHORIZATION

Covers supervised authorization lifecycle for one dry-run micro-step command.
No hardware movement is executed in this objective.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from core.db import SessionLocal
from core.models import ArmEnvelopeProbeAttempt, ArmProbeAuthorization, ArmServoEnvelope

BASE_URL = "http://127.0.0.1:18001"
TEST_ARM_ID = f"test-auth-{uuid4().hex[:8]}"


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


async def _set_unstable_region(arm_id: str, servo_id: int, start: int, end: int) -> None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmServoEnvelope).where(
                ArmServoEnvelope.arm_id == arm_id,
                ArmServoEnvelope.servo_id == servo_id,
            )
        )
        envelope = result.scalar_one()
        envelope.unstable_regions = [{"start": int(start), "end": int(end), "reason": "test"}]
        envelope.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def _mutate_dry_run_flags(command_id: str, new_flags: dict[str, Any]) -> None:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmEnvelopeProbeAttempt).where(ArmEnvelopeProbeAttempt.probe_id == command_id)
        )
        row = result.scalar_one()
        row.stop_condition_flags = new_flags
        await db.commit()


async def _get_command_target_angle(command_id: str) -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmEnvelopeProbeAttempt).where(ArmEnvelopeProbeAttempt.probe_id == command_id)
        )
        row = result.scalar_one()
        return int(row.commanded_angle)


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


async def _count_supervised_probe_dispatch_rows(arm_id: str) -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            select(ArmEnvelopeProbeAttempt)
            .join(ArmServoEnvelope, ArmEnvelopeProbeAttempt.envelope_id == ArmServoEnvelope.id)
            .where(
                ArmServoEnvelope.arm_id == arm_id,
                ArmEnvelopeProbeAttempt.phase == "supervised_micro",
                ArmEnvelopeProbeAttempt.execution_id != "",
            )
        )
        return len(list(result.scalars().all()))


class MimArmSupervisedMicroStepAuthorizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls._loop)
        cls.arm_id = TEST_ARM_ID
        status, data = post_json(f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}", {})
        assert status == 200, data
        assert data.get("initialized_count") == 6, data

    @classmethod
    def tearDownClass(cls):
        cls._loop.close()

    def _run_async(self, coro):
        return self._loop.run_until_complete(coro)

    def _create_persisted_dry_run_command(self, servo_id: int = 2) -> str:
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

    def _request_authorization(self, servo_id: int, command_id: str, expires_in_seconds: int = 300) -> tuple[int, Any]:
        return post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-authorizations/request",
            {
                "arm_id": self.arm_id,
                "dry_run_command_id": command_id,
                "operator_id": "operator.test",
                "expires_in_seconds": expires_in_seconds,
            },
        )

    def test_01_authorization_request_created_from_valid_dry_run_command(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["authorization_status"], "pending")
        self.assertFalse(data["physical_execution_allowed"])
        self.assertEqual(data["dry_run_command_id"], command_id)

    def test_02_request_blocked_if_no_dry_run_command_exists(self):
        status, data = self._request_authorization(2, str(uuid4()))
        self.assertEqual(status, 404, data)

    def test_03_request_blocked_if_command_targets_unstable_region(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        target_angle = self._run_async(_get_command_target_angle(command_id))
        self._run_async(_set_unstable_region(self.arm_id, 2, target_angle, target_angle))

        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 400, data)
        self.assertIn("unstable region", data.get("detail", ""))

    def test_04_request_blocked_if_safe_home_fallback_missing(self):
        command_id = self._create_persisted_dry_run_command(servo_id=3)
        self._run_async(
            _mutate_dry_run_flags(
                command_id,
                {
                    "stop_conditions": ["operator_stop", "estop_not_ok"],
                    "dry_run": True,
                    "physical_execution_allowed": False,
                },
            )
        )
        status, data = self._request_authorization(3, command_id)
        self.assertEqual(status, 400, data)
        self.assertIn("safe_home", data.get("detail", ""))

    def test_05_request_blocked_if_stop_conditions_missing(self):
        command_id = self._create_persisted_dry_run_command(servo_id=4)
        self._run_async(
            _mutate_dry_run_flags(
                command_id,
                {
                    "stop_conditions": [],
                    "dry_run": True,
                    "physical_execution_allowed": False,
                    "safe_home_fallback": {"target_angle": 90, "reason": "safe_home_fallback"},
                },
            )
        )
        status, data = self._request_authorization(4, command_id)
        self.assertEqual(status, 400, data)
        self.assertIn("stop_conditions", data.get("detail", ""))

    def test_06_approval_changes_status_to_approved(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, data)
        auth_id = data["authorization_id"]

        approve_status, approve_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/approve",
            {"authorized_by": "supervisor.test"},
        )
        self.assertEqual(approve_status, 200, approve_data)
        self.assertEqual(approve_data["authorization_status"], "approved")
        self.assertTrue(approve_data["physical_execution_allowed"])

    def test_07_rejection_blocks_execution(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, data)
        auth_id = data["authorization_id"]

        reject_status, reject_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/reject",
            {"rejected_by": "supervisor.test", "reason": "operator_cancelled"},
        )
        self.assertEqual(reject_status, 200, reject_data)
        self.assertEqual(reject_data["authorization_status"], "rejected")

        gate_status, gate_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/gate-check?consume=true",
            {},
        )
        self.assertEqual(gate_status, 200, gate_data)
        self.assertFalse(gate_data["allowed"])

    def test_08_expired_authorization_blocks_execution(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, data)
        auth_id = data["authorization_id"]

        approve_status, _ = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/approve",
            {"authorized_by": "supervisor.test"},
        )
        self.assertEqual(approve_status, 200)

        self._run_async(_expire_authorization(auth_id))

        gate_status, gate_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/gate-check",
            {},
        )
        self.assertEqual(gate_status, 200, gate_data)
        self.assertFalse(gate_data["allowed"])
        self.assertEqual(gate_data["authorization_status"], "expired")

    def test_09_consumed_authorization_cannot_be_reused(self):
        command_id = self._create_persisted_dry_run_command(servo_id=2)
        status, data = self._request_authorization(2, command_id)
        self.assertEqual(status, 200, data)
        auth_id = data["authorization_id"]

        approve_status, _ = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/approve",
            {"authorized_by": "supervisor.test"},
        )
        self.assertEqual(approve_status, 200)

        first_gate_status, first_gate_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/gate-check?consume=true",
            {},
        )
        self.assertEqual(first_gate_status, 200, first_gate_data)
        self.assertTrue(first_gate_data["allowed"])

        second_gate_status, second_gate_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/gate-check?consume=true",
            {},
        )
        self.assertEqual(second_gate_status, 200, second_gate_data)
        self.assertFalse(second_gate_data["allowed"])
        self.assertEqual(second_gate_data["authorization_status"], "consumed")

    def test_10_no_hardware_dispatch_occurs(self):
        command_id = self._create_persisted_dry_run_command(servo_id=5)
        status, data = self._request_authorization(5, command_id)
        self.assertEqual(status, 200, data)
        auth_id = data["authorization_id"]

        approve_status, _ = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/approve",
            {"authorized_by": "supervisor.test"},
        )
        self.assertEqual(approve_status, 200)

        gate_status, gate_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/gate-check?consume=true",
            {},
        )
        self.assertEqual(gate_status, 200, gate_data)
        self.assertTrue(gate_data["allowed"])

        dispatch_count = self._run_async(_count_supervised_probe_dispatch_rows(self.arm_id))
        self.assertEqual(dispatch_count, 0, "No hardware dispatch rows should be created")


if __name__ == "__main__":
    unittest.main()
