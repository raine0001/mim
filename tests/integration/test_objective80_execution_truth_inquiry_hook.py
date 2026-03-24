import json
import os
import urllib.error
import urllib.request
import unittest
from uuid import uuid4


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


class Objective80ExecutionTruthInquiryHookTest(unittest.TestCase):
    def test_execution_truth_generates_inquiry_and_bounded_improvement_proposal(
        self,
    ) -> None:
        run_id = uuid4().hex[:8]
        capability_name = f"execution_truth_inquiry_probe_{run_id}"

        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80.3 inquiry hook probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run execution truth inquiry probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "generate inquiry from execution truth",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(
            (
                event.get("execution", {})
                if isinstance(event.get("execution", {}), dict)
                else {}
            ).get("execution_id", 0)
        )
        self.assertGreater(execution_id, 0)

        for payload in [
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "feedback_json": {"run_id": run_id},
            },
            {
                "status": "running",
                "reason": "running",
                "actor": "tod",
                "feedback_json": {"run_id": run_id},
            },
            {
                "status": "succeeded",
                "reason": "execution truth indicates runtime mismatch",
                "actor": "tod",
                "runtime_outcome": "recovered",
                "feedback_json": {"run_id": run_id},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 800,
                    "actual_duration_ms": 1440,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.91,
                    "published_at": "2026-03-23T23:10:00Z",
                },
            },
        ]:
            status, result = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                payload,
            )
            self.assertEqual(status, 200, result)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-inquiry",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = (
            generated.get("questions", []) if isinstance(generated, dict) else []
        )
        execution_truth_question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "execution_truth_runtime_mismatch"
            ),
            None,
        )
        self.assertIsNotNone(execution_truth_question, questions)

        evidence = (
            execution_truth_question.get("trigger_evidence", {})
            if isinstance(execution_truth_question.get("trigger_evidence", {}), dict)
            else {}
        )
        self.assertEqual(int(evidence.get("execution_id", 0) or 0), execution_id)
        signal_types = set(evidence.get("signal_types", []))
        self.assertIn("simulation_reality_mismatch", signal_types)
        self.assertIn("fallback_path_used", signal_types)

        question_id = int(execution_truth_question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "request_execution_truth_review",
                "answer_json": {
                    "reason": "capture bounded execution improvement review"
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = (
            answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        )
        self.assertTrue(
            bool(applied_effect.get("improvement_proposal_created", False)),
            applied_effect,
        )
        proposal_id = int(applied_effect.get("improvement_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)

        status, proposal_detail = get_json(f"/improvement/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        proposal = (
            proposal_detail.get("proposal", {})
            if isinstance(proposal_detail, dict)
            else {}
        )
        self.assertEqual(
            str(proposal.get("proposal_type", "")), "capability_workflow_improvement"
        )
        self.assertEqual(
            str(proposal.get("affected_component", "")), "execution_truth_bridge"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
