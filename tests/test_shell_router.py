import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.routers import shell


class ShellRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_travel_mode_blocks_destructive_request(self) -> None:
        reason = shell._travel_mode_block_reason("Delete the runtime logs and wipe the repo state.")

        self.assertIn("destructive", reason.lower())

    def test_travel_mode_blocks_large_refactor_request(self) -> None:
        reason = shell._travel_mode_block_reason("Do a large refactor across the whole repo and rewrite the routing layer.")

        self.assertIn("large refactors", reason.lower())

    def test_shell_health_flags_fall_back_to_checks_payload(self) -> None:
        db_ok, runtime_ready = shell._shell_health_flags(
            {
                "status": "healthy",
                "checks": {
                    "backend": {"ok": True, "status": "healthy"},
                    "database": {"ok": True, "status": "healthy"},
                },
            }
        )

        self.assertTrue(db_ok)
        self.assertTrue(runtime_ready)

    async def test_shell_state_returns_compact_payload(self) -> None:
        fake_db = SimpleNamespace()
        fake_session = SimpleNamespace(
            id=1,
            source="shell",
            actor="operator",
            session_key="travel_shell",
            channel="chat",
            status="active",
            last_input_at=None,
            last_output_at=None,
            context_json={},
            metadata_json={},
            updated_at=None,
            created_at=None,
        )
        fake_messages = [
            SimpleNamespace(
                id=1,
                session_id=1,
                source="shell",
                actor="operator",
                direction="inbound",
                role="operator",
                content="hello",
                parsed_intent="shell_text",
                confidence=1.0,
                requires_approval=False,
                delivery_status="accepted",
                metadata_json={"message_type": "user"},
                created_at="2026-04-16T00:00:00Z",
            ),
            SimpleNamespace(
                id=2,
                session_id=1,
                source="shell",
                actor="mim",
                direction="outbound",
                role="mim",
                content="hi back",
                parsed_intent="shell_reply",
                confidence=1.0,
                requires_approval=False,
                delivery_status="accepted",
                metadata_json={"message_type": "mim_reply"},
                created_at="2026-04-16T00:00:01Z",
            ),
        ]

        with patch.object(shell, "_ensure_shell_session", AsyncMock(return_value=fake_session)), patch.object(
            shell,
            "list_interface_messages",
            AsyncMock(return_value=(fake_session, fake_messages)),
        ), patch.object(
            shell,
            "build_mim_ui_health_snapshot",
            AsyncMock(return_value={"status": "ok", "summary": "runtime healthy", "db_ok": True, "runtime_ready": True}),
        ), patch.object(
            shell,
            "build_initiative_status",
            AsyncMock(
                return_value={
                    "summary": "Active objective summary.",
                    "execution_state": "executing",
                    "active_objective": {
                        "objective_id": 40,
                        "title": "Travel shell objective",
                        "status": "in_progress",
                        "initiative_id": "TOD-MIM-REMOTE-ACCESS-SHELL",
                    },
                    "active_task": {
                        "task_id": 12,
                        "title": "Keep shell healthy",
                        "status": "in_progress",
                        "dispatch_status": "queued",
                        "execution_state": "executing",
                    },
                    "completed_recently": [],
                    "next_task": {},
                    "blocked": [],
                }
            ),
        ):
            state = await shell.shell_state(db=fake_db)

        self.assertEqual(state["objective"]["initiative_id"], "TOD-MIM-REMOTE-ACCESS-SHELL")
        self.assertEqual(state["task"]["dispatch_status"], "queued")
        self.assertEqual(state["health"]["status"], "ok")
        self.assertEqual(state["latest_reply_text"], "hi back")

    async def test_shell_chat_blocks_risky_remote_request(self) -> None:
        fake_db = SimpleNamespace(commit=AsyncMock())
        fake_session = SimpleNamespace(
            id=1,
            source="shell",
            actor="operator",
            session_key="travel_shell",
            channel="chat",
            status="active",
            last_input_at=None,
            last_output_at=None,
            context_json={},
            metadata_json={},
            updated_at=None,
            created_at=None,
        )
        inbound = SimpleNamespace(
            id=1,
            session_id=1,
            source="shell",
            actor="operator",
            direction="inbound",
            role="operator",
            content="delete the repo logs",
            parsed_intent="shell_text",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "user"},
            created_at="2026-04-16T00:00:00Z",
        )
        outbound = SimpleNamespace(
            id=2,
            session_id=1,
            source="shell",
            actor="mim",
            direction="outbound",
            role="system",
            content="Travel mode blocks destructive changes from the remote shell.",
            parsed_intent="travel_mode_block",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "system_summary"},
            created_at="2026-04-16T00:00:01Z",
        )

        with patch.object(shell, "_ensure_shell_session", AsyncMock(return_value=fake_session)), patch.object(
            shell,
            "append_interface_message",
            AsyncMock(side_effect=[(fake_session, inbound), (fake_session, outbound)]),
        ), patch("core.routers.gateway.intake_text", new=AsyncMock()) as gateway_mock:
            result = await shell.shell_chat(
                shell.ShellChatRequest(message="Delete the repo logs and wipe runtime state."),
                db=fake_db,
            )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["accepted"])
        self.assertFalse(gateway_mock.await_count)
        fake_db.commit.assert_awaited_once()

    async def test_shell_chat_forwards_safe_request_to_gateway(self) -> None:
        fake_db = SimpleNamespace(commit=AsyncMock())
        fake_session = SimpleNamespace(
            id=1,
            source="shell",
            actor="operator",
            session_key="travel_shell",
            channel="chat",
            status="active",
            last_input_at=None,
            last_output_at=None,
            context_json={},
            metadata_json={},
            updated_at=None,
            created_at=None,
        )
        inbound = SimpleNamespace(
            id=1,
            session_id=1,
            source="shell",
            actor="operator",
            direction="inbound",
            role="operator",
            content="Show blockers and validate shell state.",
            parsed_intent="shell_text",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "user"},
            created_at="2026-04-16T00:00:00Z",
        )
        outbound = SimpleNamespace(
            id=2,
            session_id=1,
            source="shell",
            actor="mim",
            direction="outbound",
            role="mim",
            content="Current blockers are clear.",
            parsed_intent="shell_reply",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "mim_reply"},
            created_at="2026-04-16T00:00:01Z",
        )
        gateway_result = {
            "request_id": "req-shell-1",
            "mim_interface": {"reply_text": "Current blockers are clear."},
        }

        with patch.object(shell, "_ensure_shell_session", AsyncMock(return_value=fake_session)), patch.object(
            shell,
            "upsert_interface_session",
            AsyncMock(return_value=fake_session),
        ), patch.object(
            shell,
            "_shell_local_command_response",
            AsyncMock(return_value=None),
        ), patch.object(
            shell,
            "append_interface_message",
            AsyncMock(side_effect=[(fake_session, inbound), (fake_session, outbound)]),
        ), patch("core.routers.gateway.intake_text", new=AsyncMock(return_value=gateway_result)) as gateway_mock:
            result = await shell.shell_chat(
                shell.ShellChatRequest(message="Please summarize the parsing fixes plan."),
                db=fake_db,
            )

        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["request_id"], "req-shell-1")
        gateway_mock.assert_awaited_once()
        fake_db.commit.assert_awaited_once()

    async def test_shell_chat_handles_local_blocker_summary_without_gateway(self) -> None:
        fake_db = SimpleNamespace(commit=AsyncMock())
        fake_session = SimpleNamespace(
            id=1,
            source="shell",
            actor="operator",
            session_key="travel_shell",
            channel="chat",
            status="active",
            last_input_at=None,
            last_output_at=None,
            context_json={},
            metadata_json={},
            updated_at=None,
            created_at=None,
        )
        inbound = SimpleNamespace(
            id=1,
            session_id=1,
            source="shell",
            actor="operator",
            direction="inbound",
            role="operator",
            content="Show current blockers.",
            parsed_intent="shell_text",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "user"},
            created_at="2026-04-16T00:00:00Z",
        )
        outbound = SimpleNamespace(
            id=2,
            session_id=1,
            source="shell",
            actor="mim",
            direction="outbound",
            role="mim",
            content="Current blockers: none.",
            parsed_intent="shell_blocker_summary",
            confidence=1.0,
            requires_approval=False,
            delivery_status="accepted",
            metadata_json={"message_type": "mim_reply"},
            created_at="2026-04-16T00:00:01Z",
        )

        with patch.object(shell, "_ensure_shell_session", AsyncMock(return_value=fake_session)), patch.object(
            shell,
            "upsert_interface_session",
            AsyncMock(return_value=fake_session),
        ), patch.object(
            shell,
            "_shell_local_command_response",
            AsyncMock(return_value=("Current blockers: none.", "shell_blocker_summary")),
        ), patch.object(
            shell,
            "append_interface_message",
            AsyncMock(side_effect=[(fake_session, inbound), (fake_session, outbound)]),
        ), patch("core.routers.gateway.intake_text", new=AsyncMock()) as gateway_mock:
            result = await shell.shell_chat(
                shell.ShellChatRequest(message="Show current blockers."),
                db=fake_db,
            )

        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["reply"]["content"], "Current blockers: none.")
        self.assertFalse(gateway_mock.await_count)
        fake_db.commit.assert_awaited_once()