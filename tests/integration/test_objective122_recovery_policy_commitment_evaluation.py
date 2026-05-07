import unittest
from uuid import uuid4

from tests.integration.operator_resolution_test_utils import cleanup_objective122_rows
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime
from tests.integration.test_objective120_recovery_policy_tuning import (
    create_operator_required_resume,
    get_autonomy_policy,
    seed_high_autonomy_boundary,
    seed_operator_reasoning_scope,
    set_autonomy_policy,
)
from tests.integration.test_objective97_recovery_learning_escalation_loop import (
    BASE_URL,
    cleanup_objective97_rows,
    create_execution,
    get_json,
    post_json,
    refresh_execution_readiness_artifacts,
    update_execution_feedback,
)


class Objective122RecoveryPolicyCommitmentEvaluationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 122",
            base_url=BASE_URL or DEFAULT_BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective97_rows()
        cleanup_objective122_rows()
        refresh_execution_readiness_artifacts()
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

    def tearDown(self) -> None:
        cleanup_objective97_rows()
        cleanup_objective122_rows()

    def test_objective122_evaluates_recovery_policy_commitments_with_recovery_evidence(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective122-{run_id}"
        baseline_autonomy = get_autonomy_policy()

        try:
            seed_high_autonomy_boundary(scope, run_id)
            create_operator_required_resume(scope)
            create_operator_required_resume(scope)

            execution_id, trace_id = create_execution(scope)
            update_execution_feedback(
                execution_id,
                status_value="blocked",
                reason="objective122 tuning trigger",
            )

            status, apply_payload = post_json(
                "/execution/recovery/policy-tuning/apply",
                {
                    "actor": "objective122-test-operator",
                    "source": "objective122",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "duration_seconds": 1800,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, apply_payload)
            commitment = apply_payload.get("commitment", {}) if isinstance(apply_payload, dict) else {}
            commitment_id = int(commitment.get("commitment_id", 0) or 0)
            self.assertGreater(commitment_id, 0, apply_payload)

            recovered_execution_id, recovered_trace_id = create_operator_required_resume(scope)
            self.assertGreater(recovered_execution_id, 0)
            self.assertTrue(recovered_trace_id)

            status, evaluate_payload = post_json(
                "/execution/recovery/policy-tuning/commitment/evaluate",
                {
                    "actor": "objective122-test",
                    "source": "objective122",
                    "trace_id": recovered_trace_id,
                    "execution_id": recovered_execution_id,
                    "managed_scope": scope,
                    "lookback_hours": 168,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, evaluate_payload)

            evaluated_commitment = evaluate_payload.get("commitment", {}) if isinstance(evaluate_payload, dict) else {}
            monitoring = evaluate_payload.get("monitoring", {}) if isinstance(evaluate_payload, dict) else {}
            outcome = evaluate_payload.get("outcome", {}) if isinstance(evaluate_payload, dict) else {}
            self.assertEqual(int(evaluated_commitment.get("commitment_id", 0) or 0), commitment_id, evaluated_commitment)
            self.assertEqual(str(evaluated_commitment.get("managed_scope", "")), scope, evaluated_commitment)
            self.assertGreater(int(monitoring.get("evidence_count", 0) or 0), 0, monitoring)
            monitoring_reasoning = monitoring.get("reasoning", {}) if isinstance(monitoring.get("reasoning", {}), dict) else {}
            self.assertTrue(bool(monitoring_reasoning.get("recovery_commitment", False)), monitoring_reasoning)
            self.assertGreaterEqual(int(monitoring_reasoning.get("recovery_outcome_count", 0) or 0), 1, monitoring_reasoning)
            self.assertIn(
                str(outcome.get("outcome_status", "")),
                {"satisfied", "ineffective", "harmful"},
                outcome,
            )
            outcome_reasoning = outcome.get("reasoning", {}) if isinstance(outcome.get("reasoning", {}), dict) else {}
            outcome_counts = outcome_reasoning.get("counts", {}) if isinstance(outcome_reasoning.get("counts", {}), dict) else {}
            self.assertGreaterEqual(int(outcome_counts.get("recovery_outcomes", 0) or 0), 1, outcome_counts)
            self.assertGreaterEqual(int(outcome_counts.get("recovery_operator_required", 0) or 0), 1, outcome_counts)

            seed_operator_reasoning_scope(scope, run_id)
            status, ui_state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, ui_state)
            operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
            recovery_commitment = operator_reasoning.get("execution_recovery_policy_commitment", {}) if isinstance(operator_reasoning.get("execution_recovery_policy_commitment", {}), dict) else {}
            recovery_monitoring = operator_reasoning.get("execution_recovery_policy_commitment_monitoring", {}) if isinstance(operator_reasoning.get("execution_recovery_policy_commitment_monitoring", {}), dict) else {}
            recovery_outcome = operator_reasoning.get("execution_recovery_policy_commitment_outcome", {}) if isinstance(operator_reasoning.get("execution_recovery_policy_commitment_outcome", {}), dict) else {}
            self.assertEqual(int(recovery_commitment.get("commitment_id", 0) or 0), commitment_id, operator_reasoning)
            self.assertEqual(int(recovery_monitoring.get("commitment_id", 0) or 0), commitment_id, recovery_monitoring)
            self.assertEqual(int(recovery_outcome.get("commitment_id", 0) or 0), commitment_id, recovery_outcome)
        finally:
            set_autonomy_policy(
                actor="objective122-test",
                reason="restore baseline autonomy policy",
                autonomy=baseline_autonomy,
            )