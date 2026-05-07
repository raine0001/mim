import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.local_broker_result_artifact_interpretation_worker import (
    persist_broker_result_artifact_interpretation,
)
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


class LocalBrokerResultArtifactInterpretationWorkerTest(unittest.TestCase):
    def test_persists_live_model_response_text_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-persist-live-text-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement persistence for one live model response interpretation",
                "summary": "Implement persistence for a live broker result interpretation without executing any tool.",
                "requested_outcome": "Implement one persisted interpretation for a live model response.",
                "constraints": ["No shell access", "Interpret only"],
                "next_bounded_steps": ["Persist the interpretation onto the existing broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "persist-live-text.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
                        "id": "chatcmpl-persist-live-text-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "Persisted interpretation: this live broker result is response-only text.",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 101, "completion_tokens": 11, "total_tokens": 112},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    worker_result = consume_broker_request_artifact_with_live_response(
                        request_artifact_path=Path(broker_preparation["broker_request_artifact"])
                    )

            interpretation_result = persist_broker_result_artifact_interpretation(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(interpretation_result["status"], "completed")
            self.assertEqual(interpretation_result["classification"], "model_response_text")
            result_payload = json.loads(Path(worker_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["interpretation"]["classification"], "model_response_text")
            self.assertEqual(
                result_payload["interpretation"]["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(
                result_payload["interpretation"]["task_linkage"]["task_id"],
                "handoff-task-handoff-persist-live-text-001",
            )

    def test_persists_live_model_single_tool_intent_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-persist-live-intent-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement persistence for one live model tool request interpretation",
                "summary": "Implement persistence for a live broker result interpretation that asks for one bounded tool.",
                "requested_outcome": "Implement one persisted interpretation for a single bounded tool request.",
                "constraints": ["No shell access", "Interpret only"],
                "next_bounded_steps": ["Persist the interpretation onto the existing broker result artifact."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "persist-live-intent.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
                        "id": "chatcmpl-persist-live-intent-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": '{"tool_name":"get_current_objective","arguments":{}}',
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 103, "completion_tokens": 9, "total_tokens": 112},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    worker_result = consume_broker_request_artifact_with_live_response(
                        request_artifact_path=Path(broker_preparation["broker_request_artifact"])
                    )

            interpretation_result = persist_broker_result_artifact_interpretation(
                result_artifact_path=Path(worker_result["result_artifact"])
            )

            self.assertEqual(interpretation_result["status"], "completed")
            self.assertEqual(
                interpretation_result["classification"],
                "model_response_single_bounded_tool_intent_request",
            )
            result_payload = json.loads(Path(worker_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(
                result_payload["interpretation"]["classification"],
                "model_response_single_bounded_tool_intent_request",
            )
            self.assertEqual(result_payload["interpretation"]["tool_name"], "get_current_objective")
            self.assertEqual(result_payload["interpretation"]["arguments"], {})


if __name__ == "__main__":
    unittest.main()