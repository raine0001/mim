import json
import os
import unittest
import urllib.error
import urllib.request
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
        return exc.code, json.loads(body) if body else {}


class Objective80ExecutionTruthConstraintInfluenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 80 constraint influence",
            base_url=BASE_URL,
            require_execution_truth_projection=True,
        )

    def _register_probe(self, *, run_id: str) -> str:
        capability_name = f"execution_truth_constraint_probe_{run_id}"
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80 constraint influence probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)
        return capability_name

    def _seed_execution_truth(
        self,
        *,
        run_id: str,
        scope: str,
        actual_duration_ms: int,
        retry_count: int,
        fallback_used: bool,
        environment_shift_detected: bool,
        simulation_match_status: str,
    ) -> int:
        capability_name = self._register_probe(run_id=run_id)
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run constraint probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for constraint evaluation",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "runtime truth captured",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": actual_duration_ms,
                    "duration_delta_ratio": round(max(0.0, (actual_duration_ms - 900) / 900), 6),
                    "retry_count": retry_count,
                    "fallback_used": fallback_used,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": environment_shift_detected,
                    "simulation_match_status": simulation_match_status,
                    "truth_confidence": 0.94,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "contract": "execution_truth_v1",
                },
            },
        )
        self.assertEqual(status, 200, payload)
        return execution_id

    def test_fresh_execution_truth_runtime_drift_requires_replan(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"constraint-drift-{run_id}"
        execution_id = self._seed_execution_truth(
            run_id=run_id,
            scope=scope,
            actual_duration_ms=1680,
            retry_count=2,
            fallback_used=True,
            environment_shift_detected=True,
            simulation_match_status="mismatch",
        )

        status, response = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective80-test",
                "source": "objective80-constraint-truth",
                "goal": {"goal_id": f"obj80-constraint-drift-{run_id}", "desired_state": "safe_physical_execution"},
                "action_plan": {
                    "action_type": "execute_action_plan",
                    "is_physical": True,
                    "target_scope": scope,
                },
                "workspace_state": {
                    "managed_scope": scope,
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.95,
                    "map_freshness_seconds": 30,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {
                    "min_target_confidence": 0.7,
                    "map_freshness_limit_seconds": 900,
                    "unlawful_action": False,
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(response.get("decision"), "requires_replan", response)
        self.assertEqual(response.get("recommended_next_step"), "reconfirm_runtime_and_replan")

        warnings = response.get("warnings", []) if isinstance(response.get("warnings", []), list) else []
        self.assertIn(
            "execution_truth_runtime_drift",
            [str(item.get("constraint", "")) for item in warnings if isinstance(item, dict)],
            warnings,
        )
        explanation = response.get("explanation", {}) if isinstance(response.get("explanation", {}), dict) else {}
        truth_influence = (
            explanation.get("execution_truth_influence", {})
            if isinstance(explanation.get("execution_truth_influence", {}), dict)
            else {}
        )
        self.assertGreaterEqual(int(truth_influence.get("signal_count", 0) or 0), 5, truth_influence)
        self.assertIn("simulation_reality_mismatch", truth_influence.get("signal_types", []), truth_influence)

        persisted_workspace_state = (
            response.get("workspace_state", {})
            if isinstance(response.get("workspace_state", {}), dict)
            else {}
        )
        summary = (
            persisted_workspace_state.get("execution_truth_summary", {})
            if isinstance(persisted_workspace_state.get("execution_truth_summary", {}), dict)
            else {}
        )
        self.assertEqual(summary.get("managed_scope"), scope, summary)
        self.assertIn(
            execution_id,
            [
                int(item.get("execution_id", 0) or 0)
                for item in (summary.get("recent_executions", []) if isinstance(summary.get("recent_executions", []), list) else [])
                if isinstance(item, dict)
            ],
            summary,
        )

    def test_execution_truth_runtime_instability_allows_conditioned_execution(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"constraint-instability-{run_id}"
        self._seed_execution_truth(
            run_id=run_id,
            scope=scope,
            actual_duration_ms=1440,
            retry_count=2,
            fallback_used=True,
            environment_shift_detected=False,
            simulation_match_status="matched",
        )

        status, response = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective80-test",
                "source": "objective80-constraint-truth",
                "goal": {"goal_id": f"obj80-constraint-instability-{run_id}", "desired_state": "lower_risk_execution"},
                "action_plan": {
                    "action_type": "execute_action_plan",
                    "is_physical": False,
                    "target_scope": scope,
                },
                "workspace_state": {
                    "managed_scope": scope,
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.95,
                    "map_freshness_seconds": 30,
                },
                "system_state": {"throttle_blocked": False, "integrity_risk": False},
                "policy_state": {
                    "min_target_confidence": 0.7,
                    "map_freshness_limit_seconds": 900,
                    "unlawful_action": False,
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(response.get("decision"), "allowed_with_conditions", response)
        self.assertEqual(response.get("recommended_next_step"), "reduce_execution_risk")

        warnings = response.get("warnings", []) if isinstance(response.get("warnings", []), list) else []
        self.assertIn(
            "execution_truth_runtime_instability",
            [str(item.get("constraint", "")) for item in warnings if isinstance(item, dict)],
            warnings,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)