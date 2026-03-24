import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timezone
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


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


class Objective80ExecutionTruthStewardshipHookTest(unittest.TestCase):
    def test_execution_truth_drives_stewardship_followup(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"execution-truth-scope-{run_id}"
        capability_name = f"execution_truth_scope_probe_{run_id}"

        status, registered = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80 stewardship hook probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, registered)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run execution truth stewardship probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for stewardship",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution = event.get("execution", {}) if isinstance(event, dict) else {}
        execution_id = int(execution.get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0, event)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "executor accepted scope probe",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, accepted)

        published_at = datetime.now(timezone.utc).isoformat()
        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "probe completed with runtime mismatch",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"run_id": run_id, "managed_scope": scope},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 1000,
                    "actual_duration_ms": 1710,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.91,
                    "published_at": published_at,
                },
            },
        )
        self.assertEqual(status, 200, succeeded)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-stewardship",
                "managed_scope": scope,
                "stale_after_seconds": 900,
                "lookback_hours": 24,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": False,
                "target_environment_state": {
                    "zone_freshness_seconds": 900,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.9,
                    "max_system_drift_rate": 0.9,
                    "max_missing_key_objects": 0,
                    "key_objects": [],
                },
                "metadata_json": {"run_id": run_id, "objective80": True},
            },
        )
        self.assertEqual(status, 200, cycled)

        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        cycle = cycled.get("cycle", {}) if isinstance(cycled, dict) else {}
        assessment = cycle.get("assessment", {}) if isinstance(cycle, dict) else {}
        post_assessment = (
            assessment.get("post", {}) if isinstance(assessment.get("post", {}), dict) else {}
        )
        execution_truth_summary = (
            post_assessment.get("execution_truth_summary", {})
            if isinstance(post_assessment.get("execution_truth_summary", {}), dict)
            else {}
        )
        degraded_signals = (
            post_assessment.get("deviation_signals", [])
            if isinstance(post_assessment.get("deviation_signals", []), list)
            else []
        )
        surfaced_types = set(summary.get("inquiry_candidate_types", []))

        self.assertTrue(bool(summary.get("persistent_degradation", False)), summary)
        self.assertGreaterEqual(int(summary.get("execution_truth_signal_count", 0) or 0), 5)
        self.assertTrue(
            {
                "execution_slower_than_expected",
                "retry_instability_detected",
                "fallback_path_used",
                "simulation_reality_mismatch",
                "environment_shift_during_execution",
            }.issubset(surfaced_types),
            summary,
        )
        self.assertEqual(str(summary.get("followup_status", "")), "generated")

        self.assertGreaterEqual(int(execution_truth_summary.get("execution_count", 0) or 0), 1)
        self.assertGreaterEqual(int(execution_truth_summary.get("signal_count", 0) or 0), 5)
        self.assertIn(
            "simulation_reality_mismatch",
            execution_truth_summary.get("signal_types", []),
            execution_truth_summary,
        )

        execution_signal = next(
            (
                item
                for item in degraded_signals
                if isinstance(item, dict)
                and str(item.get("signal_type", "")) == "simulation_reality_mismatch"
            ),
            None,
        )
        self.assertIsNotNone(execution_signal, degraded_signals)
        self.assertEqual(int((execution_signal or {}).get("execution_id", 0) or 0), execution_id)
        self.assertEqual(str((execution_signal or {}).get("target_scope", "")), scope)

        stewardship_id = int(
            (
                cycled.get("stewardship", {})
                if isinstance(cycled.get("stewardship", {}), dict)
                else {}
            ).get("stewardship_id", 0)
            or 0
        )
        self.assertGreater(stewardship_id, 0)

        status, history = get_json(
            "/stewardship/history", {"stewardship_id": stewardship_id, "limit": 10}
        )
        self.assertEqual(status, 200, history)
        history_rows = history.get("history", []) if isinstance(history, dict) else []
        history_cycle = next(
            (
                item
                for item in history_rows
                if isinstance(item, dict)
                and int(item.get("cycle_id", 0) or 0) == int(cycle.get("cycle_id", 0) or 0)
            ),
            None,
        )
        self.assertIsNotNone(history_cycle, history_rows)
        self.assertGreaterEqual(int((history_cycle or {}).get("inquiry_candidate_count", 0) or 0), 1)
        self.assertEqual(
            set((history_cycle or {}).get("inquiry_candidate_types", [])),
            surfaced_types,
        )

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-stewardship",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "stewardship_persistent_degradation"
                and str(
                    (
                        item.get("trigger_evidence", {})
                        if isinstance(item.get("trigger_evidence", {}), dict)
                        else {}
                    ).get("managed_scope", "")
                )
                == scope
            ),
            None,
        )
        self.assertIsNotNone(question, questions)
        evidence = (
            question.get("trigger_evidence", {})
            if isinstance(question.get("trigger_evidence", {}), dict)
            else {}
        )
        self.assertGreaterEqual(int(evidence.get("execution_truth_signal_count", 0) or 0), 5)
        self.assertIn(
            "simulation_reality_mismatch",
            evidence.get("execution_truth_signal_types", []),
            evidence,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)