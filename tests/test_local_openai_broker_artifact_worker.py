import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.local_openai_broker_artifact_worker import (
    consume_broker_request_artifact_with_live_response,
)


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


class LocalOpenAIBrokerArtifactWorkerTest(unittest.TestCase):
    def test_live_worker_rewrites_existing_result_with_real_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            handoff_root = root / "handoff"
            shared_root = root / "runtime" / "shared"
            inbox = handoff_root / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            artifact = {
                "schema_version": "mim-handoff-input-v1",
                "handoff_id": "handoff-openai-broker-001",
                "created_at": "2026-04-13T12:00:00Z",
                "source": "strategy-conversation",
                "topic": "Implement a bounded live broker response",
                "summary": "Prepare a bounded live broker response path using the existing request artifact.",
                "requested_outcome": "Write one real model response into the existing broker result artifact.",
                "constraints": ["No shell access", "Response only"],
                "next_bounded_steps": ["Send the request artifact to OpenAI once."],
                "bounded_actions_allowed": [],
                "status": "pending",
            }
            (inbox / "openai-live.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

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
            observed_requests: list[dict[str, object]] = []

            def fake_urlopen(request, timeout=0):
                self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
                observed_requests.append(json.loads(request.data.decode("utf-8")))
                return _FakeOpenAIResponse(
                    {
                        "id": "chatcmpl-test-001",
                        "model": "gpt-4.1-mini",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "Live broker response: the bounded request is ready for the next manual slice.",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 18, "total_tokens": 138},
                    }
                )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False):
                with patch("core.local_broker_boundary.urllib_request.urlopen", side_effect=fake_urlopen):
                    worker_result = consume_broker_request_artifact_with_live_response(
                        request_artifact_path=Path(broker_preparation["broker_request_artifact"])
                    )

            self.assertEqual(worker_result["status"], "completed")
            result_payload = json.loads(Path(worker_result["result_artifact"]).read_text(encoding="utf-8"))
            self.assertEqual(result_payload["response_kind"], "model_response")
            self.assertEqual(result_payload["handoff_id"], artifact["handoff_id"])
            self.assertEqual(result_payload["task_id"], "handoff-task-handoff-openai-broker-001")
            self.assertEqual(
                result_payload["linked_request_artifact"],
                broker_preparation["broker_request_artifact"],
            )
            self.assertEqual(
                result_payload["task_linkage"]["task_id"],
                "handoff-task-handoff-openai-broker-001",
            )
            self.assertEqual(result_payload["response"]["status"], "completed")
            self.assertEqual(
                result_payload["response"]["output_text"],
                "Live broker response: the bounded request is ready for the next manual slice.",
            )
            self.assertIsNone(result_payload["response"]["tool_call_intent"])
            self.assertEqual(result_payload["response"]["model_response"]["response_id"], "chatcmpl-test-001")
            self.assertEqual(result_payload["response"]["model_response"]["provider"], "openai")
            self.assertEqual(len(observed_requests), 1)
            user_message = observed_requests[0]["messages"][1]["content"]
            self.assertIn('"tool_schemas"', user_message)
            self.assertIn('"run_bounded_action"', user_message)
            self.assertIn('"handoff_id": "handoff-openai-broker-001"', user_message)


if __name__ == "__main__":
    unittest.main()