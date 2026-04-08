import asyncio
import os
import unittest
from pathlib import Path
from uuid import uuid4


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


def objective121_database_url() -> str:
    configured = str(os.getenv("DATABASE_URL", "")).strip()
    if configured:
        return configured
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.startswith("DATABASE_URL="):
                continue
            return line.split("=", 1)[1].strip()
    return "postgresql+asyncpg://postgres:postgres@localhost:5432/mim"


def cleanup_objective121_rows() -> None:
    asyncio.run(_cleanup_objective121_rows_async())


async def _cleanup_objective121_rows_async() -> None:
    import asyncpg

    dsn = objective121_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitment_monitoring_profiles WHERE managed_scope LIKE 'objective121-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitment_outcome_profiles WHERE managed_scope LIKE 'objective121-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE 'objective121-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_stewardship_cycles WHERE stewardship_id IN (SELECT id FROM workspace_stewardship_states WHERE managed_scope LIKE 'objective121-%')"
        )
        await conn.execute(
            "DELETE FROM workspace_stewardship_states WHERE managed_scope LIKE 'objective121-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_autonomy_boundary_profiles WHERE scope LIKE 'objective121-%'"
        )
    finally:
        await conn.close()


class Objective121RecoveryPolicyCommitmentBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 121",
            base_url=BASE_URL or DEFAULT_BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective97_rows()
        cleanup_objective121_rows()
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
        cleanup_objective121_rows()

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

    def test_objective121_applies_recovery_policy_tuning_as_resolution_commitment(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective121-{run_id}"
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
                reason="objective121 tuning trigger",
            )

            status, recovery_payload = post_json(
                "/execution/recovery/evaluate",
                {
                    "actor": "objective121-test",
                    "source": "objective121",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, recovery_payload)
            recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
            tuning = recovery.get("recovery_policy_tuning", {}) if isinstance(recovery.get("recovery_policy_tuning", {}), dict) else {}
            self.assertEqual(str(tuning.get("policy_action", "")), "lower_scope_autonomy_for_recovery", tuning)

            status, apply_payload = post_json(
                "/execution/recovery/policy-tuning/apply",
                {
                    "actor": "objective121-test-operator",
                    "source": "objective121",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "duration_seconds": 1800,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, apply_payload)
            self.assertFalse(bool(apply_payload.get("duplicate_suppressed", False)), apply_payload)
            commitment = apply_payload.get("commitment", {}) if isinstance(apply_payload, dict) else {}
            commitment_id = int(commitment.get("commitment_id", 0) or 0)
            self.assertGreater(commitment_id, 0, apply_payload)
            self.assertEqual(str(commitment.get("managed_scope", "")), scope, commitment)
            self.assertEqual(str(commitment.get("decision_type", "")), "lower_autonomy_for_scope", commitment)
            downstream_effects = commitment.get("downstream_effects_json", {}) if isinstance(commitment.get("downstream_effects_json", {}), dict) else {}
            self.assertEqual(
                str(downstream_effects.get("autonomy_level", "")),
                str(tuning.get("recommended_boundary_level", "")),
                downstream_effects,
            )
            snapshot = commitment.get("recommendation_snapshot_json", {}) if isinstance(commitment.get("recommendation_snapshot_json", {}), dict) else {}
            self.assertEqual(str(snapshot.get("policy_action", "")), "lower_scope_autonomy_for_recovery", snapshot)

            status, duplicate_payload = post_json(
                "/execution/recovery/policy-tuning/apply",
                {
                    "actor": "objective121-test-operator",
                    "source": "objective121",
                    "trace_id": trace_id,
                    "execution_id": execution_id,
                    "managed_scope": scope,
                    "duration_seconds": 1800,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, duplicate_payload)
            self.assertTrue(bool(duplicate_payload.get("duplicate_suppressed", False)), duplicate_payload)
            duplicate_commitment = duplicate_payload.get("commitment", {}) if isinstance(duplicate_payload, dict) else {}
            self.assertEqual(int(duplicate_commitment.get("commitment_id", 0) or 0), commitment_id, duplicate_commitment)

            status, commitments_payload = get_json(
                "/operator/resolution-commitments",
                {"managed_scope": scope, "active_only": "true", "limit": "20"},
            )
            self.assertEqual(status, 200, commitments_payload)
            commitments = commitments_payload.get("commitments", []) if isinstance(commitments_payload, dict) else []
            self.assertTrue(
                any(int(item.get("commitment_id", 0) or 0) == commitment_id for item in commitments if isinstance(item, dict)),
                commitments_payload,
            )

            status, recompute_payload = post_json(
                "/autonomy/boundaries/recompute",
                {
                    "actor": "objective121-test",
                    "source": "objective121-recovery-policy-commitment-bridge",
                    "scope": scope,
                    "lookback_hours": 48,
                    "min_samples": 1,
                    "apply_recommended_boundaries": True,
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
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, recompute_payload)
            recomputed_boundary = recompute_payload.get("boundary", {}) if isinstance(recompute_payload, dict) else {}
            reasoning = recomputed_boundary.get("adaptation_reasoning", {}) if isinstance(recomputed_boundary.get("adaptation_reasoning", {}), dict) else {}
            self.assertTrue(bool(reasoning.get("operator_resolution_commitment_applied", False)), reasoning)
            applied_commitment = reasoning.get("operator_resolution_commitment", {}) if isinstance(reasoning.get("operator_resolution_commitment", {}), dict) else {}
            self.assertEqual(str(applied_commitment.get("managed_scope", "")), scope, applied_commitment)
            self.assertEqual(str(applied_commitment.get("decision_type", "")), "lower_autonomy_for_scope", applied_commitment)

            seed_operator_reasoning_scope(scope, run_id)
            status, ui_state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, ui_state)
            operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
            ui_commitment = operator_reasoning.get("resolution_commitment", {}) if isinstance(operator_reasoning.get("resolution_commitment", {}), dict) else {}
            recommendation = operator_reasoning.get("current_recommendation", {}) if isinstance(operator_reasoning.get("current_recommendation", {}), dict) else {}
            self.assertEqual(str(ui_commitment.get("managed_scope", "")), scope, ui_commitment)
            self.assertEqual(str(ui_commitment.get("decision_type", "")), "lower_autonomy_for_scope", ui_commitment)
            self.assertEqual(str(recommendation.get("source", "")), "governance", recommendation)
            self.assertEqual(str(recommendation.get("decision", "")), "lower_autonomy_for_scope", recommendation)

            apply_journal = self._find_journal_entry(
                action="execution_recovery_policy_tuning_applied",
                run_id=run_id,
            )
            apply_metadata = apply_journal.get("metadata_json", {}) if isinstance(apply_journal.get("metadata_json", {}), dict) else {}
            self.assertEqual(str(apply_metadata.get("managed_scope", "")), scope, apply_metadata)
            self.assertEqual(str(apply_metadata.get("policy_action", "")), "lower_scope_autonomy_for_recovery", apply_metadata)
        finally:
            set_autonomy_policy(
                actor="objective121-test",
                reason="objective121 restore baseline",
                autonomy=baseline_autonomy,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)