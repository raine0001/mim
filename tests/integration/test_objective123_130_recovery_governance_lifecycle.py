import asyncio
import os
import unittest
from pathlib import Path
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url
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
    create_execution,
    get_json,
    post_json,
    refresh_execution_readiness_artifacts,
    update_execution_feedback,
)


SCOPE_PREFIX = "objective123130-"


def cleanup_objective123130_rows() -> None:
    asyncio.run(_cleanup_objective123130_rows_async())


async def _cleanup_objective123130_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        scope_like = f"{SCOPE_PREFIX}%"

        async def _table_exists(table_name: str) -> bool:
            return bool(
                await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL",
                    f"public.{table_name}",
                )
            )

        for table_name in [
            "execution_recovery_learning_profiles",
            "execution_recovery_outcomes",
            "execution_recovery_attempts",
            "execution_stability_profiles",
            "execution_task_orchestrations",
            "execution_intents",
            "execution_overrides",
            "capability_executions",
        ]:
            if not await _table_exists(table_name):
                continue
            scope_column = "managed_scope"
            trace_clause = " OR trace_id LIKE $2" if table_name in {"execution_recovery_outcomes", "execution_recovery_attempts"} else ""
            await conn.execute(
                f"DELETE FROM {table_name} WHERE {scope_column} LIKE $1{trace_clause}",
                scope_like,
                f"trace-{SCOPE_PREFIX}%",
            ) if trace_clause else await conn.execute(
                f"DELETE FROM {table_name} WHERE {scope_column} LIKE $1",
                scope_like,
            )

        if await _table_exists("execution_trace_events") and await _table_exists("execution_traces"):
            await conn.execute(
                "DELETE FROM execution_trace_events WHERE trace_id IN (SELECT trace_id FROM execution_traces WHERE managed_scope LIKE $1)",
                scope_like,
            )
        if await _table_exists("execution_traces"):
            await conn.execute(
                "DELETE FROM execution_traces WHERE managed_scope LIKE $1",
                scope_like,
            )

        if await _table_exists("workspace_policy_conflict_resolution_events"):
            await conn.execute(
                "DELETE FROM workspace_policy_conflict_resolution_events WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_policy_conflict_profiles"):
            await conn.execute(
                "DELETE FROM workspace_policy_conflict_profiles WHERE managed_scope LIKE $1",
                scope_like,
            )

        if await _table_exists("workspace_operator_resolution_commitment_monitoring_profiles"):
            await conn.execute(
                "DELETE FROM workspace_operator_resolution_commitment_monitoring_profiles WHERE managed_scope LIKE $1 OR commitment_id IN (SELECT id FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE $1)",
                scope_like,
            )
        if await _table_exists("workspace_operator_resolution_commitment_outcome_profiles"):
            await conn.execute(
                "DELETE FROM workspace_operator_resolution_commitment_outcome_profiles WHERE managed_scope LIKE $1 OR commitment_id IN (SELECT id FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE $1)",
                scope_like,
            )
        if await _table_exists("workspace_operator_resolution_commitments"):
            await conn.execute(
                "DELETE FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE $1",
                scope_like,
            )

        if await _table_exists("workspace_autonomy_boundary_profiles"):
            await conn.execute(
                "DELETE FROM workspace_autonomy_boundary_profiles WHERE scope LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_execution_truth_governance_profiles"):
            await conn.execute(
                "DELETE FROM workspace_execution_truth_governance_profiles WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_stewardship_cycles") and await _table_exists("workspace_stewardship_states"):
            await conn.execute(
                "DELETE FROM workspace_stewardship_cycles WHERE stewardship_id IN (SELECT id FROM workspace_stewardship_states WHERE managed_scope LIKE $1)",
                scope_like,
            )
        if await _table_exists("workspace_stewardship_states"):
            await conn.execute(
                "DELETE FROM workspace_stewardship_states WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_autonomous_chains"):
            await conn.execute(
                "DELETE FROM workspace_autonomous_chains WHERE metadata_json->>'managed_scope' LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_capability_chains"):
            await conn.execute(
                "DELETE FROM workspace_capability_chains WHERE metadata_json->>'managed_scope' LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_inquiry_questions"):
            await conn.execute(
                "DELETE FROM workspace_inquiry_questions WHERE trigger_evidence_json->>'managed_scope' LIKE $1 OR metadata_json->>'managed_scope' LIKE $1",
                scope_like,
            )
    finally:
        await conn.close()


def create_recovered_resume(scope: str) -> tuple[int, str]:
    execution_id, trace_id = create_execution(scope)
    update_execution_feedback(
        execution_id,
        status_value="blocked",
        reason="objective123130 blocked execution",
    )
    status, attempt_payload = post_json(
        "/execution/recovery/attempt",
        {
            "actor": "objective123130-test",
            "source": "objective123130",
            "trace_id": trace_id,
            "requested_decision": "resume_from_checkpoint",
            "operator_ack": True,
        },
    )
    if status != 200:
        raise AssertionError(attempt_payload)
    update_execution_feedback(
        execution_id,
        status_value="running",
        reason="objective123130 recovered execution running",
    )
    update_execution_feedback(
        execution_id,
        status_value="succeeded",
        reason="objective123130 recovered execution succeeded",
    )
    status, outcome_payload = get_json(f"/execution/recovery/outcomes/{trace_id}")
    if status != 200:
        raise AssertionError(outcome_payload)
    latest_outcome = (
        outcome_payload.get("latest_outcome", {})
        if isinstance(outcome_payload, dict)
        else {}
    )
    if str(latest_outcome.get("outcome_status") or "") != "recovered":
        raise AssertionError(outcome_payload)
    return execution_id, trace_id


class Objective123130RecoveryGovernanceLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objectives 123-130",
            base_url=BASE_URL or DEFAULT_BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective123130_rows()
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
        cleanup_objective123130_rows()

    def test_objectives_123_through_130_recovery_governance_lifecycle(self) -> None:
        run_id = uuid4().hex[:8]
        parent_scope = f"{SCOPE_PREFIX}{run_id}/parent"
        child_scope = f"{parent_scope}/child"
        baseline_autonomy = get_autonomy_policy()

        try:
            seed_high_autonomy_boundary(parent_scope, run_id)
            create_operator_required_resume(parent_scope)
            create_operator_required_resume(parent_scope)

            execution_id, trace_id = create_execution(parent_scope)
            update_execution_feedback(
                execution_id,
                status_value="blocked",
                reason="objective123130 initial recovery trigger",
            )

            status, apply_payload = post_json(
                "/execution/recovery/policy-tuning/apply",
                {
                    "actor": "objective123130-operator",
                    "source": "objective123130",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": parent_scope,
                    "duration_seconds": 1800,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, apply_payload)
            first_commitment = apply_payload.get("commitment", {}) if isinstance(apply_payload, dict) else {}
            first_commitment_id = int(first_commitment.get("commitment_id", 0) or 0)
            self.assertGreater(first_commitment_id, 0, apply_payload)

            create_recovered_resume(parent_scope)

            status, evaluate_payload = post_json(
                "/execution/recovery/policy-tuning/commitment/evaluate",
                {
                    "actor": "objective123130-test",
                    "source": "objective123130",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": parent_scope,
                    "lookback_hours": 168,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, evaluate_payload)
            monitoring = evaluate_payload.get("monitoring", {}) if isinstance(evaluate_payload, dict) else {}
            expiry_signal = monitoring.get("expiry_signal", {}) if isinstance(monitoring.get("expiry_signal", {}), dict) else {}
            self.assertIn(str(expiry_signal.get("state", "")), {"watch", "ready_to_expire"}, monitoring)

            status, preview_payload = post_json(
                "/execution/recovery/policy-tuning/commitment/preview",
                {
                    "actor": "objective123130-test",
                    "source": "objective123130",
                    "action": "expire",
                    "managed_scope": child_scope,
                    "commitment_id": first_commitment_id,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, preview_payload)
            preview = preview_payload.get("preview", {}) if isinstance(preview_payload, dict) else {}
            preview_commitment = preview.get("commitment", {}) if isinstance(preview.get("commitment", {}), dict) else {}
            scope_application = preview_commitment.get("scope_application", {}) if isinstance(preview_commitment.get("scope_application", {}), dict) else {}
            self.assertEqual(str(scope_application.get("match_type", "")), "inherited", preview_payload)
            self.assertEqual(str(preview.get("expected_transition", "")), "expired", preview)

            child_execution_id, _ = create_execution(child_scope)
            detail_status, child_execution = get_json(f"/gateway/capabilities/executions/{child_execution_id}")
            self.assertEqual(detail_status, 200, child_execution)
            self.assertEqual(str(child_execution.get("status", "")), "pending_confirmation", child_execution)

            status, child_rollup_payload = get_json(
                "/execution/recovery/policy-tuning/governance",
                {"managed_scope": child_scope},
            )
            self.assertEqual(status, 200, child_rollup_payload)
            child_rollup = child_rollup_payload.get("recovery_governance", {}) if isinstance(child_rollup_payload, dict) else {}
            child_commitment = child_rollup.get("commitment", {}) if isinstance(child_rollup.get("commitment", {}), dict) else {}
            self.assertEqual(int(child_commitment.get("commitment_id", 0) or 0), first_commitment_id, child_rollup)
            self.assertEqual(str(child_rollup.get("admission_posture", "")), "operator_required", child_rollup)
            child_scope_application = child_rollup.get("scope_application", {}) if isinstance(child_rollup.get("scope_application", {}), dict) else {}
            self.assertEqual(str(child_scope_application.get("match_type", "")), "inherited", child_rollup)
            conflict = child_rollup.get("conflict", {}) if isinstance(child_rollup.get("conflict", {}), dict) else {}
            self.assertEqual(str(conflict.get("decision_family", "")), "execution_policy_gate", child_rollup)
            conflict_sources = [str(conflict.get("winning_policy_source", ""))] + [str(item) for item in conflict.get("losing_policy_sources", []) if isinstance(item, str)]
            self.assertIn("execution_recovery_commitment", conflict_sources, child_rollup)

            status, expire_payload = post_json(
                f"/operator/resolution-commitments/{first_commitment_id}/expire",
                {
                    "actor": "objective123130-operator",
                    "reason": "objective123130 passive expiry",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, expire_payload)

            expired_execution_id, expired_trace_id = create_operator_required_resume(parent_scope)
            status, expired_evaluate_payload = post_json(
                "/execution/recovery/policy-tuning/commitment/evaluate",
                {
                    "actor": "objective123130-test",
                    "source": "objective123130",
                    "trace_id": expired_trace_id,
                    "execution_id": expired_execution_id,
                    "managed_scope": parent_scope,
                    "lookback_hours": 168,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, expired_evaluate_payload)
            expired_monitoring = expired_evaluate_payload.get("monitoring", {}) if isinstance(expired_evaluate_payload, dict) else {}
            reapply_signal = expired_monitoring.get("reapply_signal", {}) if isinstance(expired_monitoring.get("reapply_signal", {}), dict) else {}
            self.assertIn(str(reapply_signal.get("state", "")), {"watch", "recommended"}, expired_monitoring)

            status, reapply_preview_payload = post_json(
                "/execution/recovery/policy-tuning/commitment/preview",
                {
                    "actor": "objective123130-test",
                    "source": "objective123130",
                    "action": "reapply",
                    "managed_scope": parent_scope,
                    "commitment_id": first_commitment_id,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, reapply_preview_payload)
            reapply_preview = reapply_preview_payload.get("preview", {}) if isinstance(reapply_preview_payload, dict) else {}
            self.assertEqual(str(reapply_preview.get("expected_transition", "")), "reapplied", reapply_preview)

            reapply_execution_id, reapply_trace_id = create_execution(parent_scope)
            update_execution_feedback(
                reapply_execution_id,
                status_value="blocked",
                reason="objective123130 reapply trigger",
            )
            status, reapply_payload = post_json(
                "/execution/recovery/policy-tuning/apply",
                {
                    "actor": "objective123130-operator",
                    "source": "objective123130",
                    "trace_id": reapply_trace_id,
                    "execution_id": reapply_execution_id,
                    "managed_scope": parent_scope,
                    "duration_seconds": 1800,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, reapply_payload)
            self.assertTrue(bool(reapply_payload.get("reapplication", {}).get("reapplied", False)), reapply_payload)
            second_commitment = reapply_payload.get("commitment", {}) if isinstance(reapply_payload, dict) else {}
            second_commitment_id = int(second_commitment.get("commitment_id", 0) or 0)
            self.assertGreater(second_commitment_id, first_commitment_id, reapply_payload)
            self.assertEqual(
                int(second_commitment.get("reapplied_from_commitment_id", 0) or 0),
                first_commitment_id,
                second_commitment,
            )

            child_execution_id_2, _ = create_execution(child_scope)
            detail_status, child_execution_2 = get_json(f"/gateway/capabilities/executions/{child_execution_id_2}")
            self.assertEqual(detail_status, 200, child_execution_2)
            self.assertEqual(str(child_execution_2.get("status", "")), "pending_confirmation", child_execution_2)

            status, reset_payload = post_json(
                f"/operator/resolution-commitments/{second_commitment_id}/reset",
                {
                    "actor": "objective123130-operator",
                    "reason": "objective123130 manual reset",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, reset_payload)
            reset_commitment = reset_payload.get("commitment", {}) if isinstance(reset_payload, dict) else {}
            reset_metadata = reset_commitment.get("metadata_json", {}) if isinstance(reset_commitment.get("metadata_json", {}), dict) else {}
            self.assertTrue(bool(reset_metadata.get("manual_reset", False)), reset_commitment)

            status, parent_rollup_payload = get_json(
                "/execution/recovery/policy-tuning/governance",
                {"managed_scope": parent_scope},
            )
            self.assertEqual(status, 200, parent_rollup_payload)
            parent_rollup = parent_rollup_payload.get("recovery_governance", {}) if isinstance(parent_rollup_payload, dict) else {}
            parent_commitment = parent_rollup.get("commitment", {}) if isinstance(parent_rollup.get("commitment", {}), dict) else {}
            self.assertEqual(str(parent_commitment.get("lifecycle_state", "")), "manually_reset", parent_rollup)

            seed_operator_reasoning_scope(child_scope, run_id)
            status, ui_state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, ui_state)
            operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
            ui_rollup = operator_reasoning.get("execution_recovery_governance_rollup", {}) if isinstance(operator_reasoning.get("execution_recovery_governance_rollup", {}), dict) else {}
            ui_commitment = ui_rollup.get("commitment", {}) if isinstance(ui_rollup.get("commitment", {}), dict) else {}
            ui_conflict = ui_rollup.get("conflict", {}) if isinstance(ui_rollup.get("conflict", {}), dict) else {}
            ui_conflict_items = ui_conflict.get("items", []) if isinstance(ui_conflict.get("items", []), list) else []
            self.assertEqual(int(ui_commitment.get("commitment_id", 0) or 0), second_commitment_id, ui_rollup)
            self.assertEqual(str(ui_commitment.get("lifecycle_state", "")), "manually_reset", ui_rollup)
            self.assertTrue(
                any(
                    isinstance(item, dict)
                    and str(item.get("managed_scope", "")) == child_scope
                    and (
                        str(item.get("winning_policy_source", "")) == "execution_recovery_commitment"
                        or "execution_recovery_commitment" in [
                            str(source) for source in item.get("losing_policy_sources", []) if isinstance(source, str)
                        ]
                    )
                    for item in ui_conflict_items
                ),
                ui_rollup,
            )
            self.assertTrue("recovery_governance_rollup" in (ui_state.get("runtime_features", []) if isinstance(ui_state.get("runtime_features", []), list) else []), ui_state)
        finally:
            set_autonomy_policy(
                actor="objective123130-test",
                reason="restore baseline autonomy policy",
                autonomy=baseline_autonomy,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)