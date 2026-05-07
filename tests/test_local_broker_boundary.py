import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.local_broker_boundary import (
    LocalBrokerBoundary,
    build_broker_request_artifact,
    build_broker_result_artifact,
    build_broker_tool_schemas,
    build_handoff_broker_session_context,
)


class LocalBrokerBoundaryTest(unittest.TestCase):
    def test_build_broker_tool_schemas_exposes_only_bounded_tools(self) -> None:
        schemas = build_broker_tool_schemas()
        self.assertEqual(
            [schema["name"] for schema in schemas],
            [
                "get_current_objective",
                "get_tod_status",
                "get_recent_changes",
                "get_current_warnings",
                "get_bridge_warning_explanation",
                "get_bridge_warning_next_step",
                "list_bounded_actions",
                "run_bounded_action",
            ],
        )

    def test_invoke_without_client_reports_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("core.local_broker_boundary.live_openai_broker_configured", return_value=False):
                boundary = LocalBrokerBoundary(shared_root=Path(tmp_dir))
                session_context = build_handoff_broker_session_context(
                    handoff_id="handoff-broker-001",
                    payload={
                        "source": "strategy-conversation",
                        "topic": "Broker prep",
                        "summary": "Prepare the broker boundary.",
                        "requested_outcome": "Keep the slice local and bounded.",
                        "constraints": [],
                        "next_bounded_steps": [],
                        "bounded_actions_allowed": [],
                    },
                )
                result = self.loop_run(boundary.invoke(session_context=session_context))
                self.assertEqual(result["status"], "not_configured")
                self.assertIsNone(result["tool_call_intent"])

    def test_build_broker_request_and_result_artifacts(self) -> None:
        session_context = build_handoff_broker_session_context(
            handoff_id="handoff-broker-001",
            payload={
                "source": "strategy-conversation",
                "topic": "Broker prep",
                "summary": "Prepare the broker boundary.",
                "requested_outcome": "Keep the slice local and bounded.",
                "constraints": [],
                "next_bounded_steps": [],
                "bounded_actions_allowed": [],
            },
        )
        tool_schemas = build_broker_tool_schemas()
        request_artifact = build_broker_request_artifact(
            handoff_id="handoff-broker-001",
            task_id="handoff-task-handoff-broker-001",
            session_context=session_context,
            tool_schemas=tool_schemas,
        )
        self.assertEqual(request_artifact["handoff_id"], "handoff-broker-001")
        self.assertEqual(request_artifact["task_id"], "handoff-task-handoff-broker-001")
        self.assertEqual(request_artifact["task_linkage"]["session_id"], "handoff:handoff-broker-001")
        self.assertEqual(
            request_artifact["tool_names"],
            [schema["name"] for schema in tool_schemas],
        )

        result_artifact = build_broker_result_artifact(
            handoff_id="handoff-broker-001",
            task_id="handoff-task-handoff-broker-001",
            broker_response={
                "status": "not_configured",
                "reason": "local_broker_client_not_configured",
                "available_tools": [schema["name"] for schema in tool_schemas],
                "output_text": "",
                "tool_call_intent": None,
            },
        )
        self.assertEqual(result_artifact["response_kind"], "not_configured")
        self.assertEqual(result_artifact["response"]["status"], "not_configured")

    def test_run_bounded_action_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            boundary = LocalBrokerBoundary(shared_root=Path(tmp_dir))
            with self.assertRaisesRegex(ValueError, "existing bounded action"):
                boundary.execute_tool(
                    tool_name="run_bounded_action",
                    arguments={"action_name": "shell_out"},
                    session_context={"session_id": "handoff:test", "summary": ""},
                )

    def test_run_bounded_action_executes_existing_dispatch_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_root = Path(tmp_dir)
            (shared_root / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps({"objective_active": "objective-170"}, indent=2) + "\n",
                encoding="utf-8",
            )
            boundary = LocalBrokerBoundary(shared_root=shared_root)
            result = boundary.execute_tool(
                tool_name="run_bounded_action",
                arguments={"action_name": "tod_status_check"},
                session_context={
                    "session_id": "handoff:test",
                    "summary": "Check current status.",
                    "requested_outcome": "Check current status.",
                },
            )
            self.assertEqual(result["action_name"], "tod_status_check")
            self.assertEqual(result["result_status"], "succeeded")
            self.assertTrue((shared_root / "TOD_MIM_TASK_RESULT.latest.json").exists())

    @staticmethod
    def loop_run(awaitable):
        import asyncio

        return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()