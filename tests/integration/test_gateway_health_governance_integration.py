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
    async def test_status_offer_followup_does_not_confirm_stale_pending_action(self):
        event = _event(raw_input="yes please", parsed_intent="discussion")
        event.metadata_json = {
            "route_preference": "conversation_layer",
            "conversation_session_id": "session-yes-please-status-offer",
        }
        db = _FakeDB()
        conversation_context = {
            "pending_action_request": "Smart recovery for handle that thing",
            "last_prompt": "Objective 2 is not complete yet. Would you like a status update or details on next steps?",
            "last_topic": "objective",
            "last_control_state": "active",
            "clarification_state": {
                "active": True,
                "reason": "conversation_override",
                "pending_action_request": "Smart recovery for handle that thing",
            },
            "program_status_summary": "Objective 2 is still in progress while the active blocker is being cleared.",
            "current_recommendation_summary": "Verify the blocker, complete the active slice, and report the updated state.",
        }

        with patch.object(
            gateway,
            "_build_live_operational_context",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "_get_recent_text_conversation_context",
            AsyncMock(return_value=conversation_context),
        ), patch.object(
            gateway,
            "_latest_camera_observation_context",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "_camera_object_inquiry_context",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "_object_memory_context_for_query",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "_learn_from_object_inquiry_reply",
            AsyncMock(return_value=("", {})),
        ), patch.object(
            gateway,
            "write_journal",
            AsyncMock(),
        ), patch.object(
            gateway._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "healthy"},
        ):
            resolution = await gateway._resolve_event(event, db)

        self.assertEqual(resolution.internal_intent, "speak_response")
        self.assertEqual(resolution.outcome, "store_only")
        self.assertEqual(resolution.reason, "conversation_override")
        self.assertIn("status update", resolution.clarification_prompt.lower())
        self.assertIn("next steps", resolution.clarification_prompt.lower())
        self.assertNotIn("smart recovery for handle that thing", resolution.clarification_prompt.lower())

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


class TestGatewayAuthorizedInitiativeDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_training_request_auto_executes_authorized_initiative(self):
        event = _event(raw_input="start training", parsed_intent="discussion")
        db = _FakeDB()
        initiative_result = {
            "objective": {
                "title": "Drive natural-language self-evolution training",
                "priority": "high",
            },
            "human_prompt_required": False,
            "continuation": {
                "status": {
                    "summary": "Training initiative is active.",
                    "active_task": {"title": "Run the active training slice"},
                }
            },
        }

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(return_value=initiative_result),
        ):
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-training",
                session_id="session-training",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(str(result.get("reason", "")).strip(), "authorized_initiative_auto_execute")
        self.assertEqual(str(result.get("outcome", "")).strip(), "auto_execute")
        self.assertTrue(bool(result.get("initiative_auto_execute")))
        self.assertEqual(str(result.get("interface_status", "")).strip(), "doing")
        self.assertIn("initiative", str(result.get("interface_result", "")).lower())
        self.assertNotIn(
            "waiting for explicit confirmation",
            str(result.get("interface_reply", "")).lower(),
        )

    async def test_active_soft_initiative_request_reuses_initiative_authority(self):
        event = _event(raw_input="continue with the next implementation step", parsed_intent="discussion")
        db = _FakeDB()
        status_payload = {
            "active_objective": {
                "title": "Drive natural-language self-evolution training",
                "priority": "high",
                "owner": "mim",
                "boundary_mode": "soft",
                "metadata_json": {"managed_scope": "workspace"},
            }
        }

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value=status_payload),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {"title": "Drive natural-language self-evolution training"},
                    "human_prompt_required": False,
                    "continuation": {
                        "status": {
                            "summary": "The active initiative continues automatically.",
                            "active_task": {"title": "Implement bounded work"},
                        }
                    },
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-soft",
                session_id="session-soft",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(str(result.get("outcome", "")).strip(), "auto_execute")
        self.assertEqual(str(result.get("interface_status", "")).strip(), "doing")
        self.assertEqual(drive_mock.await_args.kwargs.get("objective_title"), "Drive natural-language self-evolution training")
        self.assertTrue(bool(drive_mock.await_args.kwargs.get("metadata_json", {}).get("resume_existing")))

    async def test_explicit_initiative_id_does_not_reuse_active_soft_initiative(self):
        event = _event(
            raw_input=(
                "INITIATIVE_ID: PLAN-ONLY-ISOLATION-002\n\n"
                "OBJECTIVE:\nCreate a planning-only initiative that stays isolated from prior corrective work.\n\n"
                "AUTHORITY:\nNo human confirmation required."
            ),
            parsed_intent="discussion",
        )
        db = _FakeDB()
        status_payload = {
            "active_objective": {
                "title": "Create a bounded corrective implementation task in MIM's own workspace code",
                "priority": "high",
                "owner": "mim",
                "boundary_mode": "soft",
                "metadata_json": {
                    "managed_scope": "workspace",
                    "initiative_id": "OLDER-CORRECTIVE-ID",
                },
            }
        }

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value=status_payload),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {
                        "title": "Create a planning-only initiative that stays isolated from prior corrective work.",
                        "initiative_id": "PLAN-ONLY-ISOLATION-002",
                    },
                    "human_prompt_required": False,
                    "continuation": {
                        "status": {
                            "summary": "Planning-only initiative remains isolated.",
                            "active_task": {"title": "Implement bounded work"},
                        }
                    },
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-isolation",
                session_id="session-isolation",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(str(result.get("outcome", "")).strip(), "auto_execute")
        self.assertEqual(str(result.get("interface_status", "")).strip(), "doing")
        self.assertEqual(str(drive_mock.await_args.kwargs.get("objective_title") or "").strip(), "")
        self.assertFalse(bool(drive_mock.await_args.kwargs.get("metadata_json", {}).get("resume_existing")))
        self.assertEqual(
            str(drive_mock.await_args.kwargs.get("metadata_json", {}).get("initiative_id") or "").strip(),
            "PLAN-ONLY-ISOLATION-002",
        )

    async def test_fresh_non_resume_request_does_not_inherit_active_soft_initiative(self):
        event = _event(raw_input="fix the unrelated mobile shell ui bug", parsed_intent="discussion")
        db = _FakeDB()
        status_payload = {
            "active_objective": {
                "title": "Drive natural-language self-evolution training",
                "priority": "high",
                "owner": "mim",
                "boundary_mode": "soft",
                "metadata_json": {"managed_scope": "workspace"},
            }
        }

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value=status_payload),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {"title": "Fix the unrelated mobile shell ui bug"},
                    "human_prompt_required": False,
                    "continuation": {"status": {"summary": "Fresh initiative created."}},
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-ui-bugfix",
                session_id="session-ui-bugfix",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(str(result.get("outcome", "")).strip(), "auto_execute")
        self.assertEqual(str(drive_mock.await_args.kwargs.get("objective_title") or "").strip(), "")
        self.assertFalse(bool(drive_mock.await_args.kwargs.get("metadata_json", {}).get("resume_existing")))

    async def test_single_line_explicit_initiative_id_is_sanitized_before_gateway_dispatch(self):
        event = _event(
            raw_input=(
                "INITIATIVE_ID: MIM-EXECUTION-COMPLETION-CHECK OBJECTIVE: Dispatch one bounded executable task "
                "GOAL: Verify completion only after execution evidence exists."
            ),
            parsed_intent="discussion",
        )
        db = _FakeDB()

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {
                        "title": "Dispatch one bounded executable task",
                        "initiative_id": "MIM-EXECUTION-COMPLETION-CHECK",
                    },
                    "human_prompt_required": False,
                    "continuation": {
                        "status": {
                            "summary": "Queued bounded execution work.",
                            "execution_state": "created",
                            "active_task": {"title": "Implement bounded work"},
                        }
                    },
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-single-line-id",
                session_id="session-single-line-id",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(str(result.get("outcome", "")).strip(), "auto_execute")
        self.assertEqual(
            str(drive_mock.await_args.kwargs.get("metadata_json", {}).get("initiative_id") or "").strip(),
            "MIM-EXECUTION-COMPLETION-CHECK",
        )

    async def test_planning_only_initiative_skips_continuation_dispatch(self):
        event = _event(
            raw_input=(
                "INITIATIVE_ID: MIM-PLANNING-ONLY-CHECK\n\n"
                "OBJECTIVE: Create a bounded implementation plan only. Do not dispatch code execution.\n\n"
                "RULES:\n"
                "- Create objective\n"
                "- Create task\n"
                "- Produce implementation plan\n"
                "- Do NOT dispatch execution\n"
                "- Do NOT create result artifact\n"
                "- Do NOT mark complete"
            ),
            parsed_intent="discussion",
        )
        db = _FakeDB()

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {
                        "title": "Create a bounded implementation plan only.",
                        "initiative_id": "MIM-PLANNING-ONLY-CHECK",
                    },
                    "human_prompt_required": False,
                    "continuation": {
                        "status": {
                            "summary": "Planning complete for objective Create a bounded implementation plan only. Awaiting execution dispatch.",
                            "execution_state": "created",
                            "active_task": {},
                        }
                    },
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-planning-only",
                session_id="session-planning-only",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(
            str(result.get("reason", "")).strip(),
            "authorized_planning_only_initiative_created",
        )
        self.assertEqual(str(result.get("interface_status", "")).strip(), "doing")
        self.assertIn("hold execution dispatch", str(result.get("interface_next_action", "")).lower())
        self.assertFalse(bool(drive_mock.await_args.kwargs.get("continue_chain")))
        self.assertTrue(bool(drive_mock.await_args.kwargs.get("metadata_json", {}).get("planning_only")))

    async def test_hard_boundary_initiative_keeps_confirmation_gate(self):
        event = _event(raw_input="start training for production deployment", parsed_intent="discussion")
        db = _FakeDB()

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {"title": "Start production deployment"},
                    "human_prompt_required": True,
                    "continuation": {
                        "status": {
                            "summary": "The requested initiative reached a hard boundary.",
                        }
                    },
                }
            ),
        ):
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-hard",
                session_id="session-hard",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(
            str(result.get("reason", "")).strip(),
            "initiative_hard_boundary_requires_confirmation",
        )
        self.assertEqual(str(result.get("outcome", "")).strip(), "requires_confirmation")
        self.assertFalse(bool(result.get("initiative_auto_execute")))
        self.assertEqual(str(result.get("interface_status", "")).strip(), "deferred")
        self.assertIn("explicit confirmation", str(result.get("interface_next_action", "")).lower())

    async def test_continuation_validation_request_uses_full_auto_step_budget(self):
        event = _event(
            raw_input=(
                "INITIATIVE_ID: MIM-CONTINUOUS-EXECUTION-VALIDATION\n\n"
                "OBJECTIVE:\nVerify that MIM and TOD can execute, recover, and continue work autonomously without human intervention.\n\n"
                "EXECUTION MODEL:\nThis is a CONTROLLED CONTINUATION TEST\n\n"
                "GOAL:\nDemonstrate sustained multi-step execution with automatic continuation after task completion, recovery events, and readiness transitions.\n\n"
                "AUTHORITY:\n- No human confirmation required\n\n"
                "Task 5: Validate auto-resume\n\n"
                "SUCCESS CRITERIA:\n- 5+ tasks executed in sequence"
            ),
            parsed_intent="discussion",
        )
        db = _FakeDB()

        with patch.object(
            gateway,
            "build_initiative_status",
            AsyncMock(return_value={}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {"title": "Drive autonomous continuation validation [-GATEWAY]"},
                    "human_prompt_required": False,
                    "continuation": {"status": {"summary": "Validation initiative is active."}},
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id="req-validation",
                session_id="session-validation",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(drive_mock.await_args.kwargs.get("max_auto_steps"), 8)

    async def test_repeated_tod_status_loop_escalates_to_corrective_implementation(self):
        event = _event(raw_input="check tod status", parsed_intent="discussion")
        db = _FakeDB()

        with patch.object(
            gateway,
            "_recent_tod_status_dispatch_loop_signal",
            AsyncMock(
                return_value={
                    "detected": True,
                    "count": 3,
                    "reason": "tod_status_dispatch",
                    "result_status": "succeeded",
                    "result_reason": "TOD bridge accepted the bounded status check.",
                }
            ),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(
                return_value={
                    "objective": {"title": "Correct repeated TOD status loop"},
                    "human_prompt_required": False,
                    "continuation": {
                        "status": {
                            "summary": "Corrective implementation initiative is active.",
                            "active_task": {"title": "Implement corrective routing change"},
                        }
                    },
                }
            ),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_repeated_tod_status_loop_recovery(
                event=event,
                request_id="req-status-loop",
                session_id="session-status-loop",
                db=db,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(
            str(result.get("reason", "")).strip(),
            "stale_tod_status_loop_escalated_to_implementation",
        )
        self.assertTrue(bool(result.get("initiative_auto_execute")))
        self.assertEqual(int(result.get("status_loop_repeat_count", 0)), 3)
        self.assertIn("corrective implementation initiative", str(result.get("interface_result", "")).lower())
        self.assertEqual(drive_mock.await_args.kwargs.get("managed_scope"), "workspace")
        self.assertEqual(drive_mock.await_args.kwargs.get("max_auto_steps"), 3)
        self.assertIn(
            "prevent repeated tod status-check loops",
            str(drive_mock.await_args.kwargs.get("user_intent", "")).lower(),
        )

    async def test_repeated_tod_status_loop_recovery_skips_when_no_stale_signal(self):
        event = _event(raw_input="check tod status", parsed_intent="discussion")
        db = _FakeDB()

        with patch.object(
            gateway,
            "_recent_tod_status_dispatch_loop_signal",
            AsyncMock(return_value={"detected": False, "count": 1}),
        ), patch.object(
            gateway,
            "drive_initiative_from_intent",
            AsyncMock(),
        ) as drive_mock:
            result = await gateway._maybe_dispatch_repeated_tod_status_loop_recovery(
                event=event,
                request_id="req-status-loop",
                session_id="session-status-loop",
                db=db,
            )

        self.assertIsNone(result)
        drive_mock.assert_not_awaited()


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