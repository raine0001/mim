"""Integration-style tests for gateway health governance and precedence.

These tests drive the live gateway resolution logic in-process with a lightweight
fake DB so health-state and combined governance signals remain deterministic.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from core.models import InputEvent
from core.routers import gateway, safety_router
from core.schemas import ExecutionDispatchRequest
from core.user_action_inquiry_service import UserActionInquiryService
from core.user_action_safety_monitor import (
    ActionCategory,
    UserAction,
    UserActionSafetyMonitor,
)


class _FakeExecuteResult:
    def __init__(self, first_value):
        self._first_value = first_value

    def scalars(self):
        return self

    def first(self):
        return self._first_value

    def all(self):
        if self._first_value is None:
            return []
        return [self._first_value]


class _FakeDB:
    def __init__(self, *, capability=None, event=None, resolution=None):
        self.capability = capability
        self.event = event
        self.resolution = resolution
        self.added = []
        self._next_id = 2000

    async def execute(self, stmt):
        stmt_text = str(stmt)
        if "capability_registrations" in stmt_text:
            return _FakeExecuteResult(self.capability)
        if "input_event_resolutions" in stmt_text:
            return _FakeExecuteResult(self.resolution)
        return _FakeExecuteResult(None)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            self._next_id += 1
            obj.id = self._next_id
        self.added.append(obj)

    async def flush(self):
        return None

    async def get(self, model, key):
        if model is gateway.InputEvent and self.event is not None and int(self.event.id) == int(key):
            return self.event
        return None

    async def commit(self):
        return None

    async def refresh(self, obj):
        return None


def _event(*, raw_input: str, parsed_intent: str = "execute_capability", event_id: int = 101) -> InputEvent:
    return InputEvent(
        id=event_id,
        source="text",
        raw_input=raw_input,
        parsed_intent=parsed_intent,
        confidence=0.95,
        target_system="tod",
        requested_goal="",
        safety_flags=[],
        metadata_json={},
        normalized=True,
    )


class TestGatewayHealthGovernanceResolution(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_execution_keeps_auto_execute(self):
        event = _event(raw_input="run workspace check")
        db = _FakeDB(capability=SimpleNamespace(enabled=True, requires_confirmation=False))

        with patch.object(gateway, "write_journal", AsyncMock()), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "healthy"},
        ):
            resolution = await gateway._resolve_event(event, db)

        self.assertEqual(resolution.outcome, "auto_execute")
        self.assertEqual(resolution.reason, "policy_allows_auto_execute")
        governance = resolution.metadata_json.get("governance", {})
        self.assertEqual(governance.get("primary_signal"), "benign_healthy_auto_execution")
        self.assertEqual(governance.get("system_health_status"), "healthy")
        self.assertIn("healthy", str(governance.get("summary", "")).lower())

    async def test_degraded_health_requires_confirmation(self):
        event = _event(raw_input="run workspace check")
        db = _FakeDB(capability=SimpleNamespace(enabled=True, requires_confirmation=False))

        with patch.object(gateway, "write_journal", AsyncMock()), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "degraded"},
        ):
            resolution = await gateway._resolve_event(event, db)

        self.assertEqual(resolution.outcome, "requires_confirmation")
        self.assertEqual(resolution.reason, "system_health_degraded")
        self.assertIn("System health is degraded", resolution.clarification_prompt)
        self.assertIn("system_health_degraded", list(resolution.escalation_reasons or []))
        governance = resolution.metadata_json.get("governance", {})
        self.assertEqual(governance.get("primary_signal"), "degraded_health_confirmation")

    async def test_critical_health_requires_confirmation(self):
        event = _event(raw_input="run workspace check")
        db = _FakeDB(capability=SimpleNamespace(enabled=True, requires_confirmation=False))

        with patch.object(gateway, "write_journal", AsyncMock()), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "critical"},
        ):
            resolution = await gateway._resolve_event(event, db)

        self.assertEqual(resolution.outcome, "requires_confirmation")
        self.assertEqual(resolution.reason, "system_health_degraded")
        self.assertIn("System health is critical", resolution.clarification_prompt)
        governance = resolution.metadata_json.get("governance", {})
        self.assertEqual(governance.get("system_health_status"), "critical")

    async def test_safety_risk_and_degraded_health_keep_stable_precedence(self):
        event = _event(raw_input="run apt install dangerous-package")
        db = _FakeDB(capability=SimpleNamespace(enabled=True, requires_confirmation=False))
        safety_result = {
            "action_id": "gateway-action-101",
            "risk_level": "high",
            "risk_category": "software_installation",
            "reasoning": "High-risk package installation",
            "specific_concerns": ["May alter runtime state"],
            "recommended_inquiry": True,
            "safe_to_execute": False,
            "inquiry_id": "inq-123",
        }

        with patch.object(gateway, "write_journal", AsyncMock()), patch.object(
            gateway, "_assess_user_action_safety_for_event", return_value=safety_result
        ), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "degraded"},
        ):
            resolution = await gateway._resolve_event(event, db)

        self.assertEqual(resolution.outcome, "requires_confirmation")
        self.assertEqual(resolution.reason, "user_action_safety_requires_inquiry")
        self.assertIn("user_action_safety_risk", list(resolution.escalation_reasons or []))
        self.assertIn("system_health_degraded", list(resolution.escalation_reasons or []))
        self.assertIn("High-risk action detected", resolution.clarification_prompt)
        self.assertIn("System health is degraded", resolution.clarification_prompt)
        governance = resolution.metadata_json.get("governance", {})
        self.assertEqual(governance.get("primary_signal"), "hard_safety_escalation")
        self.assertEqual(
            governance.get("precedence_order"),
            gateway.GATEWAY_GOVERNANCE_PRECEDENCE,
        )
        self.assertIn("user_action_safety_risk", governance.get("signal_codes", []))
        self.assertIn("system_health_degraded", governance.get("signal_codes", []))
        # Safety is primary; governance summary must lead with safety reason,
        # then note health as secondary context with "Additionally:".
        summary = governance.get("summary", "")
        self.assertIn("Additionally:", summary)
        self.assertIn("system health is also degraded", summary)
        # Clarification prompt must contain the shorter secondary health note, not
        # the redundant "Automatic execution is paused until confirmation" clause.
        cp = resolution.clarification_prompt or ""
        self.assertNotIn("Automatic execution is paused until confirmation", cp)

    async def test_suboptimal_health_does_not_gate_execution(self):
        """'suboptimal' status must NOT trigger the confirmation gate."""
        event = _event(raw_input="run workspace check")
        db = _FakeDB(capability=SimpleNamespace(enabled=True, requires_confirmation=False))

        with patch.object(gateway, "write_journal", AsyncMock()), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "suboptimal"},
        ):
            resolution = await gateway._resolve_event(event, db)

        # Should still auto-execute despite suboptimal health.
        self.assertEqual(resolution.outcome, "auto_execute")
        governance = resolution.metadata_json.get("governance", {})
        # Primary signal should still be benign auto-execution.
        self.assertEqual(governance.get("primary_signal"), "benign_healthy_auto_execution")
        # Health code must distinguish suboptimal from fully healthy.
        self.assertEqual(governance.get("system_health_status"), "suboptimal")


class TestGatewayDispatchGovernanceSurface(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_refusal_includes_governance_context(self):
        event = _event(raw_input="apt install dangerous-package", event_id=222)
        resolution = SimpleNamespace(
            capability_name="workspace_check",
            escalation_reasons=["user_action_safety_risk", "system_health_degraded"],
            metadata_json={
                "user_action_safety": {"inquiry_id": "inq-blocked"},
                "governance": {
                    "summary": "High-risk user action requires inquiry approval before execution. System health is degraded; automatic execution requires operator confirmation.",
                },
            },
        )
        db = _FakeDB(event=event, resolution=resolution)

        with self.assertRaises(HTTPException) as ctx:
            await gateway.dispatch_event_execution(
                event_id=222,
                payload=ExecutionDispatchRequest(),
                db=db,
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("unresolved user-action safety inquiry", str(ctx.exception.detail))
        self.assertIn("Governance context:", str(ctx.exception.detail))
        self.assertIn("System health is degraded", str(ctx.exception.detail))


class TestSafetyInquiryGovernanceSurface(unittest.IsolatedAsyncioTestCase):
    async def test_inquiry_payload_surfaces_system_health_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            inquiry_service = UserActionInquiryService(Path(tmpdir))
            action = UserAction(
                action_id="action-risky-1",
                timestamp=datetime.datetime.utcnow().isoformat() + "Z",
                user_id="operator",
                action_type="execute_capability",
                description="apt install dangerous-package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
                command="apt install dangerous-package",
                target_path=None,
                parameters={},
            )
            assessment = monitor.assess_action(action)
            self.assertTrue(assessment.recommended_inquiry)

            with patch.object(safety_router, "safety_monitor", monitor), patch.object(
                safety_router, "inquiry_service", inquiry_service
            ), patch.object(
                safety_router._mim_health_monitor,
                "get_health_summary",
                return_value={"status": "degraded"},
            ):
                response = await safety_router.create_inquiry_for_action(
                    action_id=action.action_id,
                    user_id="operator",
                    action_description=action.description,
                )

        self.assertEqual(response.system_health_status, "degraded")
        self.assertIn("System health is degraded", response.governance_summary)
        self.assertIn("approve explicitly before execution", response.operator_prompt)