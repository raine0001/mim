import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.local_broker_artifact_worker import consume_broker_request_artifact
from core.local_broker_result_interpreter import interpret_broker_result_artifact
from core.local_openai_broker_artifact_worker import consume_broker_request_artifact_with_live_response


ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT = ROOT / "scripts" / "run_handoff_intake_once.py"


class _FakeOpenAIResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class LocalBrokerResultInterpreterTest(unittest.TestCase):
    def test_interprets_no_tool_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-interpret-no-tool-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement no-tool placeholder interpretation",
                "summary": "Prepare and classify a no-tool placeholder broker result for a bounded implementation path.",
                "requested_outcome": "Implement classification for a no-tool placeholder only.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Classify the existing broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "interpret-no-tool.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            worker_result = consume_broker_request_artifact(
                request_artifact_path=Path(broker_preparation["broker_request_artifact"])
            )
            interpretation = interpret_broker_result_artifact(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(interpretation["classification"], "no_tool_placeholder")
            self.assertEqual(interpretation["handoff_id"], artifact["handoff_id"])
            self.assertEqual(interpretation["task_id"], "handoff-task-handoff-interpret-no-tool-001")
            self.assertEqual(
                interpretation["task_linkage"]["task_id"],
                "handoff-task-handoff-interpret-no-tool-001",
            )
            self.assertEqual(
                interpretation["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )

    def test_interprets_live_model_response_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-interpret-live-text-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement interpretation for one live model response as text only",
                "summary": "Implement preparation and classification for one real live broker result artifact as response-only text.",
                "requested_outcome": "Implement classification for one live model response without any tool execution.",
                "constraints": ["No shell access", "Response only"],
                "next_bounded_steps": ["Interpret the existing live broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "interpret-live-text.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            def fake_urlopen(request, timeout=0):
                return _FakeOpenAIResponse(
                    {
                        "id": "chatcmpl-live-text-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "Live broker response: this handoff is ready for the next bounded manual slice.",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 14, "total_tokens": 114},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    worker_result = consume_broker_request_artifact_with_live_response(
                        request_artifact_path=Path(broker_preparation["broker_request_artifact"])
                    )

            interpretation = interpret_broker_result_artifact(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(interpretation["classification"], "model_response_text")
            self.assertEqual(interpretation["handoff_id"], artifact["handoff_id"])
            self.assertEqual(interpretation["task_id"], "handoff-task-handoff-interpret-live-text-001")
            self.assertEqual(
                interpretation["task_linkage"]["task_id"],
                "handoff-task-handoff-interpret-live-text-001",
            )
            self.assertEqual(
                interpretation["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(
                interpretation["output_text"],
                "Live broker response: this handoff is ready for the next bounded manual slice.",
            )

    def test_interprets_live_model_single_bounded_tool_intent_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-interpret-live-intent-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement interpretation for one live model tool-intent request",
                "summary": "Implement preparation and classification for one live broker result artifact that asks for a single bounded tool.",
                "requested_outcome": "Implement classification for one bounded tool-intent request without executing it.",
                "constraints": ["No shell access", "Interpret only"],
                "next_bounded_steps": ["Interpret the existing live broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "interpret-live-intent.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            def fake_urlopen(request, timeout=0):
                return _FakeOpenAIResponse(
                    {
                        "id": "chatcmpl-live-intent-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"tool_name":"get_tod_status","arguments":{}}',
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 102, "completion_tokens": 10, "total_tokens": 112},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    worker_result = consume_broker_request_artifact_with_live_response(
                        request_artifact_path=Path(broker_preparation["broker_request_artifact"])
                    )

            interpretation = interpret_broker_result_artifact(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(
                interpretation["classification"],
                "model_response_single_bounded_tool_intent_request",
            )
            self.assertEqual(interpretation["handoff_id"], artifact["handoff_id"])
            self.assertEqual(interpretation["task_id"], "handoff-task-handoff-interpret-live-intent-001")
            self.assertEqual(
                interpretation["task_linkage"]["task_id"],
                "handoff-task-handoff-interpret-live-intent-001",
            )
            self.assertEqual(
                interpretation["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(interpretation["tool_name"], "get_tod_status")
            self.assertEqual(interpretation["arguments"], {})
            self.assertEqual(
                interpretation["output_text"],
                '{"tool_name":"get_tod_status","arguments":{}}',
            )

    def test_interprets_single_bounded_tool_intent_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-interpret-intent-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement bounded tool intent placeholder interpretation",
                "summary": "Prepare and classify one bounded tool intent placeholder for a broker implementation path.",
                "requested_outcome": "Implement classification for a single bounded tool intent placeholder only.",
                "constraints": ["No shell access", "Existing bounded actions only"],
                "next_bounded_steps": ["Classify the existing broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "interpret-intent.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(INTAKE_SCRIPT)],
                cwd=ROOT,
                env={
                    **dict(os.environ),
                    "MIM_HANDOFF_ROOT": str(handoff_root),
                    "MIM_SHARED_ROOT": str(shared_root),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            intake_result = json.loads(completed.stdout)
            status_payload = json.loads(Path(intake_result["status_path"]).read_text(encoding="utf-8"))
            broker_preparation = status_payload["latest_result"]["broker_preparation"]

            worker_result = consume_broker_request_artifact(
                request_artifact_path=Path(broker_preparation["broker_request_artifact"]),
                tool_name="run_bounded_action",
                arguments={"action_name": "tod_status_check"},
            )
            interpretation = interpret_broker_result_artifact(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(
                interpretation["classification"],
                "single_bounded_tool_intent_placeholder",
            )
            self.assertEqual(interpretation["handoff_id"], artifact["handoff_id"])
            self.assertEqual(interpretation["task_id"], "handoff-task-handoff-interpret-intent-001")
            self.assertEqual(
                interpretation["task_linkage"]["task_id"],
                "handoff-task-handoff-interpret-intent-001",
            )
            self.assertEqual(
                interpretation["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )


if __name__ == "__main__":
    unittest.main()