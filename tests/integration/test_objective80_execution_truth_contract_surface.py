import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)


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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective80ExecutionTruthContractSurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 80 contract surface",
            base_url=BASE_URL,
            require_execution_truth_projection=True,
        )

    def test_execution_truth_changes_reasoning_input(self) -> None:
        run_id = uuid4().hex[:8]
        capability_name = f"execution_truth_probe_{run_id}"

        status, registered = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80.1 execution truth probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, registered)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run execution truth probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution = (
            event.get("execution", {})
            if isinstance(event.get("execution", {}), dict)
            else {}
        )
        execution_id = int(execution.get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0, event)

        for step in [
            {
                "status": "accepted",
                "reason": "executor accepted probe",
                "feedback_json": {"queue": "executor-a"},
            },
            {
                "status": "running",
                "reason": "probe running",
                "feedback_json": {"progress": 60},
            },
        ]:
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {**step, "actor": "tod"},
            )
            self.assertEqual(status, 200, payload)

        published_at = datetime.now(timezone.utc).isoformat()
        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "probe completed with measurable drift",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "feedback_json": {"progress": 100, "run_id": run_id},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 1000,
                    "actual_duration_ms": 1680,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.92,
                    "published_at": published_at,
                },
            },
        )
        self.assertEqual(status, 200, succeeded)

        execution_truth = (
            succeeded.get("execution_truth", {})
            if isinstance(succeeded.get("execution_truth", {}), dict)
            else {}
        )
        self.assertEqual(execution_truth.get("contract"), "execution_truth_v1")
        self.assertEqual(int(execution_truth.get("execution_id", 0) or 0), execution_id)
        self.assertAlmostEqual(
            float(execution_truth.get("duration_delta_ratio", 0.0) or 0.0),
            0.68,
            places=2,
        )

        feedback_json = (
            succeeded.get("feedback_json", {})
            if isinstance(succeeded.get("feedback_json", {}), dict)
            else {}
        )
        signal_types = set(feedback_json.get("execution_truth_signal_types", []))
        self.assertTrue(
            {
                "execution_slower_than_expected",
                "retry_instability_detected",
                "fallback_path_used",
                "simulation_reality_mismatch",
                "environment_shift_during_execution",
            }.issubset(signal_types),
            feedback_json,
        )

        status, feedback_view = get_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback"
        )
        self.assertEqual(status, 200, feedback_view)
        feedback_truth = (
            feedback_view.get("execution_truth", {})
            if isinstance(feedback_view.get("execution_truth", {}), dict)
            else {}
        )
        self.assertEqual(feedback_truth.get("contract"), "execution_truth_v1")

        status, built = post_json(
            "/reasoning/context/build",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-v1",
                "lookback_hours": 24,
                "max_items_per_domain": 25,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, built)
        context = built.get("context", {}) if isinstance(built, dict) else {}
        workspace_state = (
            context.get("workspace_state", {})
            if isinstance(context.get("workspace_state", {}), dict)
            else {}
        )
        execution_truth_summary = (
            workspace_state.get("execution_truth_summary", {})
            if isinstance(workspace_state.get("execution_truth_summary", {}), dict)
            else {}
        )
        reasoning = (
            context.get("reasoning", {})
            if isinstance(context.get("reasoning", {}), dict)
            else {}
        )

        self.assertGreaterEqual(
            int(execution_truth_summary.get("execution_count", 0) or 0),
            1,
            execution_truth_summary,
        )
        self.assertGreaterEqual(
            int(execution_truth_summary.get("deviation_signal_count", 0) or 0),
            5,
            execution_truth_summary,
        )

        recent_executions = (
            execution_truth_summary.get("recent_executions", [])
            if isinstance(execution_truth_summary.get("recent_executions", []), list)
            else []
        )
        target = next(
            (
                item
                for item in recent_executions
                if isinstance(item, dict)
                and int(item.get("execution_id", 0) or 0) == execution_id
            ),
            None,
        )
        self.assertIsNotNone(target, recent_executions)
        self.assertIn(
            "simulation_reality_mismatch", target.get("signal_types", []), target
        )

        links = (
            reasoning.get("cross_domain_links", [])
            if isinstance(reasoning.get("cross_domain_links", []), list)
            else []
        )
        self.assertIn(
            "Execution-truth deviations should influence planning assumptions, runtime trust, and operator guidance.",
            links,
            reasoning,
        )
        influence = (
            reasoning.get("execution_truth_influence", {})
            if isinstance(reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )
        self.assertGreaterEqual(
            int(influence.get("deviation_signal_count", 0) or 0), 5, influence
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
