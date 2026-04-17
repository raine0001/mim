import unittest
from uuid import uuid4


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime
from tests.integration.test_objective97_recovery_learning_escalation_loop import (
    BASE_URL,
    cleanup_objective97_rows,
    create_execution,
    get_json,
    post_json,
    refresh_execution_readiness_artifacts,
    update_execution_feedback,
)


def get_autonomy_policy() -> dict:
    status, payload = get_json("/workspace/autonomy/policy")
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError(payload)
    autonomy = payload.get("autonomy", {}) if isinstance(payload.get("autonomy", {}), dict) else {}
    if not autonomy:
        raise AssertionError(payload)
    return autonomy


def set_autonomy_policy(*, actor: str, reason: str, autonomy: dict) -> dict:
    payload = {
        "actor": actor,
        "reason": reason,
        "auto_execution_enabled": bool(autonomy.get("auto_execution_enabled", True)),
        "force_manual_approval": bool(autonomy.get("force_manual_approval", False)),
        "max_auto_actions_per_minute": int(autonomy.get("max_auto_actions_per_minute", 6) or 6),
        "max_auto_tasks_per_window": int(autonomy.get("max_auto_tasks_per_window", 6) or 6),
        "auto_window_seconds": int(autonomy.get("auto_window_seconds", 60) or 60),
        "cooldown_between_actions_seconds": int(autonomy.get("cooldown_between_actions_seconds", 5) or 5),
        "capability_cooldown_seconds": autonomy.get("capability_cooldown_seconds", {}) if isinstance(autonomy.get("capability_cooldown_seconds", {}), dict) else {},
        "zone_action_limits": autonomy.get("zone_action_limits", {}) if isinstance(autonomy.get("zone_action_limits", {}), dict) else {},
        "restricted_zones": autonomy.get("restricted_zones", []) if isinstance(autonomy.get("restricted_zones", []), list) else [],
        "auto_safe_confidence_threshold": float(autonomy.get("auto_safe_confidence_threshold", 0.8) or 0.8),
        "auto_preferred_confidence_threshold": float(autonomy.get("auto_preferred_confidence_threshold", 0.7) or 0.7),
        "low_risk_score_max": float(autonomy.get("low_risk_score_max", 0.3) or 0.3),
        "max_autonomy_retries": int(autonomy.get("max_autonomy_retries", 1) or 1),
        "reset_auto_history": True,
    }
    status, response = post_json("/workspace/autonomy/override", payload)
    if status != 200:
        raise AssertionError(response)
    return response


def seed_high_autonomy_boundary(scope: str, run_id: str) -> dict:
    set_autonomy_policy(
        actor="objective120-test",
        reason="objective120 elevated baseline",
        autonomy={
            "auto_execution_enabled": True,
            "force_manual_approval": False,
            "max_auto_actions_per_minute": 10,
            "max_auto_tasks_per_window": 10,
            "auto_window_seconds": 60,
            "cooldown_between_actions_seconds": 1,
            "auto_safe_confidence_threshold": 0.8,
            "auto_preferred_confidence_threshold": 0.7,
            "low_risk_score_max": 0.45,
            "max_autonomy_retries": 1,
        },
    )
    status, payload = post_json(
        "/autonomy/boundaries/recompute",
        {
            "actor": "objective120-test",
            "source": "objective120-recovery-policy-tuning",
            "scope": scope,
            "lookback_hours": 72,
            "min_samples": 1,
            "apply_recommended_boundaries": False,
            "hard_ceiling_overrides": {
                "human_safety": True,
                "legality": True,
                "system_integrity": True,
            },
            "evidence_inputs_override": {
                "sample_count": 20,
                "success_rate": 0.98,
                "escalation_rate": 0.01,
                "retry_rate": 0.02,
                "interruption_rate": 0.01,
                "memory_delta_rate": 0.84,
                "override_rate": 0.01,
                "replan_rate": 0.02,
                "environment_stability": 0.94,
                "development_confidence": 0.86,
                "constraint_reliability": 0.92,
                "experiment_confidence": 0.83,
            },
            "metadata_json": {"run_id": run_id, "objective": "120"},
        },
    )
    if status != 200:
        raise AssertionError(payload)
    boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
    if str(boundary.get("current_level", "")) not in {"operator_required", "bounded_auto", "strategy_auto"}:
        raise AssertionError(boundary)
    return boundary


def create_operator_required_resume(scope: str) -> tuple[int, str]:
    execution_id, trace_id = create_execution(scope)
    update_execution_feedback(
        execution_id,
        status_value="blocked",
        reason="objective120 blocked execution",
    )
    status, attempt_payload = post_json(
        "/execution/recovery/attempt",
        {
            "actor": "objective120-test",
            "source": "objective120",
            "trace_id": trace_id,
            "requested_decision": "resume_from_checkpoint",
            "operator_ack": True,
        },
    )
    if status != 200:
        raise AssertionError(attempt_payload)
    attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
    if str(attempt.get("status") or "") != "accepted":
        raise AssertionError(attempt_payload)

    update_execution_feedback(
        execution_id,
        status_value="running",
        reason="objective120 resumed execution running",
    )
    update_execution_feedback(
        execution_id,
        status_value="blocked",
        reason="objective120 operator still required after resume",
    )
    status, outcome_payload = get_json(f"/execution/recovery/outcomes/{trace_id}")
    if status != 200:
        raise AssertionError(outcome_payload)
    outcome = (
        outcome_payload.get("latest_outcome", {})
        if isinstance(outcome_payload, dict)
        else {}
    )
    if str(outcome.get("outcome_status") or "") != "operator_required":
        raise AssertionError(outcome_payload)
    return execution_id, trace_id


def seed_operator_reasoning_scope(scope: str, run_id: str) -> dict:
    status, payload = post_json(
        "/stewardship/cycle",
        {
            "actor": "objective120-test",
            "source": "objective120-recovery-policy-tuning",
            "managed_scope": scope,
            "stale_after_seconds": 300,
            "lookback_hours": 168,
            "max_strategies": 3,
            "max_actions": 3,
            "auto_execute": False,
            "force_degraded": True,
            "target_environment_state": {
                "zone_freshness_seconds": 300,
                "critical_object_confidence": 0.8,
                "max_degraded_zones": 0,
                "max_zone_uncertainty_score": 0.35,
                "max_system_drift_rate": 0.05,
                "max_missing_key_objects": 0,
                "key_objects": [f"objective120-missing-{run_id}"],
            },
            "metadata_json": {"run_id": run_id, "managed_scope": scope},
        },
    )
    if status != 200:
        raise AssertionError(payload)
    return payload


class Objective120RecoveryPolicyTuningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 120",
            base_url=BASE_URL or DEFAULT_BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective97_rows()
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

    def _find_journal_entry(self, *, action: str, run_id: str) -> dict:
        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        rows = journal if isinstance(journal, list) else []
        match = next(
            (
                entry
                for entry in rows
                if isinstance(entry, dict)
                and str(entry.get("action", "")) == action
                and str(
                    (
                        entry.get("metadata_json", {})
                        if isinstance(entry.get("metadata_json", {}), dict)
                        else {}
                    ).get("run_id", "")
                )
                == run_id
            ),
            None,
        )
        self.assertIsNotNone(match, rows[:20])
        return match or {}

    def test_objective120_recovery_policy_tuning_is_operator_visible_and_persisted(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective120-{run_id}"
        baseline_autonomy = get_autonomy_policy()

        try:
            boundary = seed_high_autonomy_boundary(scope, run_id)
            current_level = str(boundary.get("current_level", ""))
            self.assertIn(current_level, {"operator_required", "bounded_auto", "strategy_auto"}, boundary)

            create_operator_required_resume(scope)
            create_operator_required_resume(scope)

            execution_id, trace_id = create_execution(scope)
            update_execution_feedback(
                execution_id,
                status_value="blocked",
                reason="objective120 tuning trigger",
            )

            status, recovery_payload = post_json(
                "/execution/recovery/evaluate",
                {
                    "actor": "objective120-test",
                    "source": "objective120",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, recovery_payload)
            recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
            learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
            tuning = recovery.get("recovery_policy_tuning", {}) if isinstance(recovery.get("recovery_policy_tuning", {}), dict) else {}

            self.assertEqual(str(learning.get("escalation_decision", "")), "lower_scope_autonomy_for_recovery", learning)
            self.assertEqual(str(tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", tuning)
            self.assertEqual(str(tuning.get("current_boundary_level", "")), current_level, tuning)
            expected_level = (
                "operator_required"
                if current_level in {"operator_required", "bounded_auto"}
                else "bounded_auto"
            )
            self.assertEqual(str(tuning.get("recommended_boundary_level", "")), expected_level, tuning)
            self.assertTrue(bool(tuning.get("operator_review_required", False)), tuning)
            self.assertEqual(
                bool(tuning.get("boundary_floor_applied", False)),
                current_level == expected_level,
                tuning,
            )

            status, attempt_payload = post_json(
                "/execution/recovery/attempt",
                {
                    "actor": "objective120-test",
                    "source": "objective120",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "requested_decision": "resume_from_checkpoint",
                    "operator_ack": True,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, attempt_payload)
            attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
            self.assertEqual(str(attempt.get("status", "")), "accepted", attempt)
            attempt_tuning = attempt.get("recovery_policy_tuning", {}) if isinstance(attempt.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(attempt_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", attempt_tuning)
            self.assertEqual(str(attempt_tuning.get("recommended_boundary_level", "")), expected_level, attempt_tuning)

            update_execution_feedback(
                execution_id,
                status_value="running",
                reason="objective120 tuned recovery resumed execution",
            )
            update_execution_feedback(
                execution_id,
                status_value="blocked",
                reason="objective120 operator still required after tuned recovery",
            )
            status, outcome_payload = post_json(
                "/execution/recovery/outcomes/evaluate",
                {
                    "actor": "objective120-test",
                    "source": "objective120",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome_payload)
            outcome = outcome_payload.get("outcome", {}) if isinstance(outcome_payload, dict) else {}
            outcome_tuning = outcome.get("recovery_policy_tuning", {}) if isinstance(outcome.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(outcome.get("outcome_status", "")), "operator_required", outcome)
            self.assertEqual(str(outcome_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", outcome_tuning)
            self.assertEqual(str(outcome_tuning.get("recommended_boundary_level", "")), expected_level, outcome_tuning)

            status, recovery_state_payload = get_json(f"/execution/recovery/{trace_id}")
            self.assertEqual(status, 200, recovery_state_payload)
            recovery_state = recovery_state_payload.get("recovery", {}) if isinstance(recovery_state_payload, dict) else {}
            state_tuning = recovery_state.get("recovery_policy_tuning", {}) if isinstance(recovery_state.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(state_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", state_tuning)

            seed_operator_reasoning_scope(scope, run_id)

            status, ui_state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, ui_state)
            operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
            ui_tuning = (
                operator_reasoning.get("execution_recovery_policy_tuning", {})
                if isinstance(operator_reasoning.get("execution_recovery_policy_tuning", {}), dict)
                else {}
            )
            recommendation = (
                operator_reasoning.get("current_recommendation", {})
                if isinstance(operator_reasoning.get("current_recommendation", {}), dict)
                else {}
            )
            self.assertEqual(str(ui_tuning.get("managed_scope", "")), scope, ui_tuning)
            self.assertEqual(str(ui_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", ui_tuning)
            self.assertEqual(str(ui_tuning.get("recommended_boundary_level", "")), expected_level, ui_tuning)
            self.assertEqual(str(recommendation.get("source", "")), "execution_recovery_policy_tuning", recommendation)
            self.assertEqual(str(recommendation.get("decision", "")), "lower_scope_autonomy_for_recovery", recommendation)

            attempt_journal = self._find_journal_entry(
                action="execution_recovery_attempt_recorded",
                run_id=run_id,
            )
            attempt_metadata = (
                attempt_journal.get("metadata_json", {})
                if isinstance(attempt_journal.get("metadata_json", {}), dict)
                else {}
            )
            attempt_journal_tuning = attempt_metadata.get("recovery_policy_tuning", {}) if isinstance(attempt_metadata.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(attempt_journal_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", attempt_journal_tuning)

            outcome_journal = self._find_journal_entry(
                action="execution_recovery_outcome_evaluated",
                run_id=run_id,
            )
            outcome_metadata = (
                outcome_journal.get("metadata_json", {})
                if isinstance(outcome_journal.get("metadata_json", {}), dict)
                else {}
            )
            outcome_journal_tuning = outcome_metadata.get("recovery_policy_tuning", {}) if isinstance(outcome_metadata.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(outcome_journal_tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", outcome_journal_tuning)
        finally:
            set_autonomy_policy(
                actor="objective120-test",
                reason="objective120 restore baseline",
                autonomy=baseline_autonomy,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)