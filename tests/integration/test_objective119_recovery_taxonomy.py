import unittest
from uuid import uuid4


from tests.integration.test_objective96_execution_recovery_safe_resume import (
    cleanup_objective96_rows,
    create_execution,
    get_json,
    post_json,
    refresh_execution_readiness_artifacts,
    update_execution_feedback,
)


class Objective119RecoveryTaxonomyTest(unittest.TestCase):
    def setUp(self) -> None:
        cleanup_objective96_rows()
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
        cleanup_objective96_rows()

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

    def test_objective119_recovery_taxonomy_flows_through_evaluate_attempt_outcome_and_ui(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective119-{run_id}"

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective119 initial failure",
        )

        status, eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective119-test",
                "source": "objective119",
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, eval_payload)
        recovery = eval_payload.get("recovery", {}) if isinstance(eval_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "retry_current_step", recovery)
        self.assertEqual(str(recovery.get("recovery_classification", "")), "bounded_retry", recovery)
        taxonomy = recovery.get("recovery_taxonomy", {}) if isinstance(recovery.get("recovery_taxonomy", {}), dict) else {}
        self.assertEqual(str(taxonomy.get("family", "")), "retry", taxonomy)
        self.assertEqual(str(taxonomy.get("checkpoint_strategy", "")), "current_step", taxonomy)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        ui_recovery = (
            operator_reasoning.get("execution_recovery", {})
            if isinstance(operator_reasoning.get("execution_recovery", {}), dict)
            else {}
        )
        self.assertTrue(str(ui_recovery.get("recovery_classification", "")).strip(), ui_recovery)
        ui_taxonomy = (
            ui_recovery.get("recovery_taxonomy", {})
            if isinstance(ui_recovery.get("recovery_taxonomy", {}), dict)
            else {}
        )
        self.assertEqual(
            str(ui_taxonomy.get("classification", "")),
            str(ui_recovery.get("recovery_classification", "")),
            ui_recovery,
        )
        self.assertTrue(str(ui_taxonomy.get("family", "")).strip(), ui_taxonomy)

        status, attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective119-test",
                "source": "objective119",
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
                "requested_decision": "retry_current_step",
                "reason": "objective119 bounded retry",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, attempt_payload)
        attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
        self.assertEqual(str(attempt.get("status", "")), "accepted", attempt)
        self.assertEqual(str(attempt.get("recovery_classification", "")), "bounded_retry", attempt)
        attempt_taxonomy = attempt.get("recovery_taxonomy", {}) if isinstance(attempt.get("recovery_taxonomy", {}), dict) else {}
        self.assertEqual(str(attempt_taxonomy.get("family", "")), "retry", attempt_taxonomy)

        update_execution_feedback(
            execution_id,
            status_value="succeeded",
            reason="objective119 recovered after retry",
        )

        status, outcome_payload = post_json(
            "/execution/recovery/outcomes/evaluate",
            {
                "actor": "objective119-test",
                "source": "objective119",
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, outcome_payload)
        outcome = outcome_payload.get("outcome", {}) if isinstance(outcome_payload, dict) else {}
        self.assertEqual(str(outcome.get("outcome_status", "")), "recovered", outcome)
        self.assertEqual(str(outcome.get("recovery_classification", "")), "bounded_retry", outcome)
        self.assertEqual(
            str(outcome.get("recovery_outcome_classification", "")),
            "recovered_after_recovery",
            outcome,
        )
        outcome_taxonomy = outcome.get("recovery_outcome_taxonomy", {}) if isinstance(outcome.get("recovery_outcome_taxonomy", {}), dict) else {}
        self.assertEqual(str(outcome_taxonomy.get("terminality", "")), "successful", outcome_taxonomy)

        status, recovery_state_payload = get_json(f"/execution/recovery/{trace_id}")
        self.assertEqual(status, 200, recovery_state_payload)
        recovery_state = recovery_state_payload.get("recovery", {}) if isinstance(recovery_state_payload, dict) else {}
        latest_outcome = recovery_state.get("latest_outcome", {}) if isinstance(recovery_state.get("latest_outcome", {}), dict) else {}
        self.assertEqual(
            str(latest_outcome.get("recovery_outcome_classification", "")),
            "recovered_after_recovery",
            latest_outcome,
        )

        attempt_journal = self._find_journal_entry(
            action="execution_recovery_attempt_recorded",
            run_id=run_id,
        )
        self.assertEqual(
            str(
                (
                    attempt_journal.get("metadata_json", {})
                    if isinstance(attempt_journal.get("metadata_json", {}), dict)
                    else {}
                ).get("recovery_classification", "")
            ),
            "bounded_retry",
            attempt_journal,
        )

        outcome_journal = self._find_journal_entry(
            action="execution_recovery_outcome_evaluated",
            run_id=run_id,
        )
        outcome_journal_metadata = (
            outcome_journal.get("metadata_json", {})
            if isinstance(outcome_journal.get("metadata_json", {}), dict)
            else {}
        )
        self.assertEqual(
            str(outcome_journal_metadata.get("recovery_outcome_classification", "")),
            "recovered_after_recovery",
            outcome_journal,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)