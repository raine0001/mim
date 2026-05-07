"""
Objective 180: ARM envelope learning UI / operator workflow

Covers a UI-friendly operator workflow surface that consolidates:
  - envelope state
  - probe-plan preview
  - dry-run generation action
  - authorization request + approve/reject actions
  - one micro-step execution action
  - execution feedback
  - learned envelope update visibility
"""

import asyncio
import unittest
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from core.db import SessionLocal, engine
from core.models import ArmServoEnvelope

BASE_URL = "http://127.0.0.1:18001"
TEST_ARM_ID = f"test-uiwf-{uuid4().hex[:8]}"


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


async def _dispose_engine() -> None:
    await engine.dispose()


class MimArmEnvelopeLearningUiWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls._loop)
        cls._loop.run_until_complete(_dispose_engine())
        cls.arm_id = TEST_ARM_ID
        status, data = post_json(f"/mim/arm/envelopes/initialize?arm_id={cls.arm_id}", {})
        assert status == 200, data
        assert data.get("initialized_count") == 6, data

    @classmethod
    def tearDownClass(cls):
        cls._loop.close()

    def _run_async(self, coro):
        return self._loop.run_until_complete(coro)

    def test_01_workflow_surface_shows_state_and_preview(self):
        servo_id = 2
        self._run_async(_set_envelope_ready(self.arm_id, servo_id))

        status, data = get_json(
            f"/mim/arm/operator-workflow/envelopes/{servo_id}",
            params={"arm_id": self.arm_id},
        )
        self.assertEqual(status, 200, data)

        self.assertEqual(data["arm_id"], self.arm_id)
        self.assertEqual(data["servo_id"], servo_id)
        self.assertIn("envelope_state", data)
        self.assertIn("probe_plan_preview", data)
        self.assertIn("actions", data)

        expected_steps = {
            "show_envelope_state",
            "preview_probe_plan",
            "generate_dry_run",
            "request_authorization",
            "approve_or_reject",
            "execute_one_micro_step",
            "show_feedback",
            "show_learned_envelope_update",
        }
        self.assertTrue(expected_steps.issubset(set(data.get("workflow_steps", []))), data)
        self.assertGreaterEqual(len(data["probe_plan_preview"].get("probe_steps", [])), 0)

    def test_02_workflow_runs_execution_and_shows_feedback_and_learning(self):
        servo_id = 3
        self._run_async(_set_envelope_ready(self.arm_id, servo_id))

        s1, dry = post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-commands/dry-run",
            {
                "arm_id": self.arm_id,
                "skip_unstable_regions": True,
                "max_target_angles": 3,
                "persist_as_attempts": True,
            },
        )
        self.assertEqual(s1, 200, dry)
        self.assertGreater(len(dry.get("commands", [])), 0, dry)
        command_id = dry["commands"][0]["command_id"]

        s2, auth = post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-authorizations/request",
            {
                "arm_id": self.arm_id,
                "dry_run_command_id": command_id,
                "operator_id": "operator.test",
                "expires_in_seconds": 300,
            },
        )
        self.assertEqual(s2, 200, auth)
        auth_id = auth["authorization_id"]

        s3, approved = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/approve",
            {"authorized_by": "supervisor.test"},
        )
        self.assertEqual(s3, 200, approved)
        self.assertEqual(approved["authorization_status"], "approved")

        s4, exec_data = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/execute-physical-micro-step",
            {"operator_id": "operator.test"},
        )
        self.assertEqual(s4, 200, exec_data)
        execution_id = exec_data["execution_id"]

        s5, outcome = post_json(
            f"/mim/arm/physical-executions/{execution_id}/record-probe-outcome",
            {"execution_id": execution_id},
        )
        self.assertEqual(s5, 200, outcome)

        s6, workflow = get_json(
            f"/mim/arm/operator-workflow/envelopes/{servo_id}",
            params={"arm_id": self.arm_id, "execution_id": execution_id},
        )
        self.assertEqual(s6, 200, workflow)

        feedback = workflow.get("latest_execution_feedback")
        self.assertIsNotNone(feedback, workflow)
        self.assertEqual(feedback["execution_id"], execution_id)

        learned = workflow.get("latest_learned_envelope_update")
        self.assertIsNotNone(learned, workflow)
        self.assertEqual(learned["execution_id"], execution_id)
        self.assertEqual(learned["phase"], "supervised_micro")

    def test_03_workflow_reflects_reject_path(self):
        servo_id = 4
        self._run_async(_set_envelope_ready(self.arm_id, servo_id))

        s1, dry = post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-commands/dry-run",
            {
                "arm_id": self.arm_id,
                "skip_unstable_regions": True,
                "max_target_angles": 3,
                "persist_as_attempts": True,
            },
        )
        self.assertEqual(s1, 200, dry)
        command_id = dry["commands"][0]["command_id"]

        s2, auth = post_json(
            f"/mim/arm/envelopes/{servo_id}/probe-authorizations/request",
            {
                "arm_id": self.arm_id,
                "dry_run_command_id": command_id,
                "operator_id": "operator.test",
                "expires_in_seconds": 300,
            },
        )
        self.assertEqual(s2, 200, auth)
        auth_id = auth["authorization_id"]

        s3, rejected = post_json(
            f"/mim/arm/probe-authorizations/{auth_id}/reject",
            {"rejected_by": "supervisor.test", "reason": "operator_rejected"},
        )
        self.assertEqual(s3, 200, rejected)
        self.assertEqual(rejected["authorization_status"], "rejected")

        s4, workflow = get_json(
            f"/mim/arm/operator-workflow/envelopes/{servo_id}",
            params={"arm_id": self.arm_id, "authorization_id": auth_id},
        )
        self.assertEqual(s4, 200, workflow)
        self.assertEqual(
            workflow.get("latest_authorization", {}).get("authorization_status"),
            "rejected",
            workflow,
        )
        self.assertFalse(workflow.get("actions", {}).get("execute_one_micro_step", {}).get("enabled", True))
