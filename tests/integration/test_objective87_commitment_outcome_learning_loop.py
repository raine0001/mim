import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tests.integration.operator_resolution_test_utils import cleanup_objective87_rows
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


class Objective87CommitmentOutcomeLearningLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 87",
            base_url=BASE_URL,
            require_ui_state=True,
        )
        cleanup_objective87_rows()

    def setUp(self) -> None:
        cleanup_objective87_rows()

    def tearDown(self) -> None:
        cleanup_objective87_rows()

    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200, payload)

    def _create_stale_observation(self, *, zone: str, run_id: str) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective87 stale scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.6,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": f"obj87-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_prereqs(self, *, scope: str, run_id: str, source: str) -> None:
        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.82,
                "confidence": 0.9,
                "source": source,
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, goals)

    def _create_commitment(self, *, scope: str, run_id: str) -> int:
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective87-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "hold auto-execution until the environment is revalidated",
                "recommendation_snapshot_json": {
                    "recommendation": "keep autonomous remediation deferred",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.95,
                "duration_seconds": 7200,
                "provenance_json": {"source": "objective87-focused", "run_id": run_id},
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                    "maintenance_mode": "deferred",
                    "stewardship_defer_actions": True,
                    "strategy_priority_mode": "prefer_stabilization",
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        commitment = created.get("commitment", {}) if isinstance(created, dict) else {}
        commitment_id = int(commitment.get("commitment_id", 0) or 0)
        self.assertGreater(commitment_id, 0, created)
        return commitment_id

    def _run_stewardship_cycle(self, *, scope: str, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective87-test",
                "source": source,
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective87-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        self.assertTrue(
            bool(summary.get("operator_resolution_blocked_auto_execution", False)),
            payload,
        )
        return payload

    def _seed_execution_truth(self, *, scope: str, run_id: str) -> None:
        capability_name = f"objective87_truth_probe_{run_id}"
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 87 execution truth probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective87 execution truth probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect commitment outcome evidence",
                "metadata_json": {
                    "capability": capability_name,
                    "managed_scope": scope,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, accepted)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "execution truth recorded",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
                "execution_truth": {
                    "contract": "execution_truth_v1",
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 800,
                    "actual_duration_ms": 1900,
                    "duration_delta_ratio": round((1900 - 800) / 800.0, 6),
                    "retry_count": 3,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.94,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _prepare_outcome_fixture(self) -> tuple[str, str, str, int]:
        scope = f"objective87-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective87-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)
        commitment_id = self._create_commitment(scope=scope, run_id=run_id)
        for _ in range(3):
            self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        self._seed_execution_truth(scope=scope, run_id=run_id)
        return scope, run_id, source, commitment_id

    def test_commitment_outcome_evaluation_updates_commitment_and_downstream_reasoning(self) -> None:
        scope, run_id, source, commitment_id = self._prepare_outcome_fixture()

        status, monitored = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, monitored)

        status, evaluated = post_json(
            f"/operator/resolution-commitments/{commitment_id}/outcomes/evaluate",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, evaluated)
        outcome = evaluated.get("outcome", {}) if isinstance(evaluated, dict) else {}
        commitment = evaluated.get("commitment", {}) if isinstance(evaluated, dict) else {}
        outcome_id = int(outcome.get("outcome_id", 0) or 0)
        self.assertGreater(outcome_id, 0, evaluated)
        self.assertEqual(str(outcome.get("managed_scope", "")), scope)
        self.assertEqual(str(outcome.get("outcome_status", "")), "ineffective", outcome)
        self.assertEqual(str(commitment.get("status", "")), "ineffective", commitment)
        self.assertEqual(
            str(outcome.get("learning_signals", {}).get("repeat_commitment_bias", "")),
            "avoid",
            outcome,
        )

        status, strategy_payload = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, strategy_payload)
        goals = strategy_payload.get("goals", []) if isinstance(strategy_payload, dict) else []
        self.assertTrue(goals, strategy_payload)
        first_goal = goals[0] if isinstance(goals[0], dict) else {}
        self.assertIn(
            "operator_resolution_outcome",
            first_goal.get("reasoning", {}),
            first_goal,
        )

        status, autonomy_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective87-test",
                "source": source,
                "scope": scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, autonomy_payload)
        boundary = autonomy_payload.get("boundary", {}) if isinstance(autonomy_payload, dict) else {}
        self.assertIn(
            "operator_resolution_outcome",
            boundary.get("adaptation_reasoning", {}),
            boundary,
        )

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = (
            ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        )
        ui_outcome = operator_reasoning.get("commitment_outcome", {})
        self.assertEqual(int(ui_outcome.get("outcome_id", 0) or 0), outcome_id)
        self.assertEqual(str(ui_outcome.get("outcome_status", "")), "ineffective")

    def test_commitment_learning_inquiry_records_avoid_similar_bias(self) -> None:
        _, run_id, source, commitment_id = self._prepare_outcome_fixture()

        status, _ = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        status, evaluated = post_json(
            f"/operator/resolution-commitments/{commitment_id}/outcomes/evaluate",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, evaluated)
        outcome = evaluated.get("outcome", {}) if isinstance(evaluated, dict) else {}
        outcome_id = int(outcome.get("outcome_id", 0) or 0)
        self.assertGreater(outcome_id, 0)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective87-test",
                "source": source,
                "lookback_hours": 168,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        learning_question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "operator_commitment_learning_review"
            ),
            None,
        )
        self.assertIsNotNone(learning_question, generated)

        question_id = int(learning_question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "objective87-test-operator",
                "selected_path_id": "avoid_similar_commitments",
                "answer_json": {
                    "reason": "the prior deferment created too much drag without improving stability"
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        self.assertTrue(
            bool(applied_effect.get("commitment_learning_bias_recorded", False)),
            answered,
        )
        self.assertEqual(str(applied_effect.get("learning_bias", "")), "avoid_similar_commitments")

        status, detail = get_json(
            f"/operator/resolution-commitments/{commitment_id}/outcomes/{outcome_id}"
        )
        self.assertEqual(status, 200, detail)
        refreshed = detail.get("outcome", {}) if isinstance(detail, dict) else {}
        self.assertEqual(
            str(refreshed.get("learning_signals", {}).get("repeat_commitment_bias", "")),
            "avoid",
            refreshed,
        )
        learning_bias = refreshed.get("metadata_json", {}).get("operator_learning_bias", {})
        self.assertEqual(str(learning_bias.get("bias", "")), "avoid_similar_commitments")

    def test_commitment_can_be_manually_resolved_as_abandoned(self) -> None:
        scope = f"objective87-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective87-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)
        commitment_id = self._create_commitment(scope=scope, run_id=run_id)

        status, resolved = post_json(
            f"/operator/resolution-commitments/{commitment_id}/resolve",
            {
                "actor": "objective87-test-operator",
                "source": source,
                "target_status": "abandoned",
                "reason": "operator chose to abandon this temporary hold",
                "lookback_hours": 24,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, resolved)
        outcome = resolved.get("outcome", {}) if isinstance(resolved, dict) else {}
        commitment = resolved.get("commitment", {}) if isinstance(resolved, dict) else {}
        self.assertEqual(str(outcome.get("outcome_status", "")), "abandoned")
        self.assertEqual(str(commitment.get("status", "")), "abandoned")


if __name__ == "__main__":
    unittest.main()