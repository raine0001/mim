import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import cleanup_objective87_rows, objective85_database_url
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


def cleanup_objective88_3_rows() -> None:
    cleanup_objective87_rows()
    asyncio.run(_cleanup_objective88_3_rows_async())


async def _cleanup_objective88_3_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE source = 'objective88_3'"
        )
        await conn.execute(
            "DELETE FROM workspace_proposals WHERE source = 'objective88_3'"
        )
    finally:
        await conn.close()


def seed_workspace_proposal(*, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int) -> int:
    return asyncio.run(
        _seed_workspace_proposal_async(
            run_id=run_id,
            proposal_type=proposal_type,
            related_zone=related_zone,
            confidence=confidence,
            age_seconds=age_seconds,
        )
    )


async def _seed_workspace_proposal_async(*, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int) -> int:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        created_at = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(age_seconds)))
        row = await conn.fetchrow(
            """
            INSERT INTO workspace_proposals (
                proposal_type, title, description, status, confidence, priority_score,
                priority_reason, source, related_zone, related_object_id, source_execution_id,
                trigger_json, metadata_json, created_at
            ) VALUES (
                $1, $2, $3, 'pending', $4, 0.0,
                '', 'objective88_3', $5, NULL, NULL,
                $6::jsonb, $7::jsonb, $8
            )
            RETURNING id
            """,
            str(proposal_type),
            f"objective88_3 {proposal_type} {run_id}",
            f"seeded proposal {proposal_type} for objective88_3 {run_id}",
            float(confidence),
            str(related_zone),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id}),
            created_at,
        )
        return int(row["id"])
    finally:
        await conn.close()


class Objective88_3ProposalArbitrationLearningPropagationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 88.3",
            base_url=BASE_URL,
            require_proposal_arbitration_learning=True,
        )
        cleanup_objective88_3_rows()

    def setUp(self) -> None:
        cleanup_objective88_3_rows()

    def tearDown(self) -> None:
        cleanup_objective88_3_rows()

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
                "text": f"objective88_3 stale scan {run_id}",
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
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, payload)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": f"obj88_3-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_strategy_and_boundary(self, *, scope: str, run_id: str, source: str) -> None:
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
                "actor": "objective88_3-test",
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

        status, boundary = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective88_3-test",
                "source": source,
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {"human_safety": "bounded_auto"},
                "evidence_inputs_override": {
                    "success_rate": 0.9,
                    "escalation_rate": 0.05,
                    "retry_rate": 0.05,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.7,
                    "sample_count": 20,
                    "manual_override_count": 0,
                    "replan_count": 0,
                    "constraint_high_risk_count": 0,
                    "stability_signal": 0.9,
                    "human_present_rate": 0.0,
                    "active_experiment_count": 0,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, boundary)

    def _record_arbitration_outcome(self, *, proposal_id: int, proposal_type: str, scope: str, decision: str = "won") -> None:
        status, payload = post_json(
            "/workspace/proposals/arbitration-outcomes",
            {
                "actor": "objective88_3-test",
                "source": "objective88_3",
                "proposal_id": proposal_id,
                "proposal_type": proposal_type,
                "related_zone": scope,
                "arbitration_decision": decision,
                "arbitration_posture": "integrate",
                "trust_chain_status": "verified",
                "downstream_execution_outcome": "succeeded",
                "confidence": 0.94,
                "arbitration_reason": f"{proposal_type} repeatedly wins in this scope",
                "conflict_context_json": {"run": "objective88_3"},
                "commitment_state_json": {"managed_scope": scope},
                "metadata_json": {"objective88_3": True},
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_arbitration_learning(self, *, run_id: str, scope: str) -> None:
        for proposal_type in ["rescan_zone", "confirm_target_ready"]:
            for index in range(4):
                proposal_id = seed_workspace_proposal(
                    run_id=f"{run_id}-{proposal_type}-{index}",
                    proposal_type=proposal_type,
                    related_zone=scope,
                    confidence=0.84,
                    age_seconds=15 + index,
                )
                self._record_arbitration_outcome(
                    proposal_id=proposal_id,
                    proposal_type=proposal_type,
                    scope=scope,
                )

    def _create_commitment(self, *, scope: str, run_id: str) -> int:
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective88_3-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "hold autonomous remediation until the environment is revalidated",
                "recommendation_snapshot_json": {
                    "recommendation": "keep autonomous remediation deferred",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.95,
                "duration_seconds": 7200,
                "provenance_json": {"source": "objective88_3", "run_id": run_id},
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
                "actor": "objective88_3-test",
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
                    "key_objects": [f"objective88_3-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        return payload

    def test_arbitration_learning_influences_stewardship_followup_and_inquiry_weighting(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective88_3-stewardship-{run_id}"
        source = "objective88_3-propagation"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)
        self._seed_strategy_and_boundary(scope=scope, run_id=run_id, source=source)

        status, baseline_cycle = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective88_3-test",
                "source": source,
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective88_3-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope, "phase": "baseline"},
            },
        )
        self.assertEqual(status, 200, baseline_cycle)
        baseline_summary = baseline_cycle.get("summary", {}) if isinstance(baseline_cycle, dict) else {}
        self.assertFalse(bool((baseline_summary.get("proposal_arbitration_followup", {}) if isinstance(baseline_summary.get("proposal_arbitration_followup", {}), dict) else {}).get("applied", False)), baseline_summary)

        self._seed_arbitration_learning(run_id=run_id, scope=scope)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective88_3-test",
                "source": source,
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective88_3-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope, "phase": "biased"},
            },
        )
        self.assertEqual(status, 200, cycled)
        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        self.assertTrue(bool(summary.get("followup_generated", False)), summary)
        self.assertTrue(bool((summary.get("proposal_arbitration_followup", {}) if isinstance(summary.get("proposal_arbitration_followup", {}), dict) else {}).get("applied", False)), summary)
        self.assertIn(
            str(summary.get("preferred_followup_type", "")),
            {
                "stale_zone_detected",
                "persistent_zone_degradation",
                "zone_uncertainty_above_target",
                "key_object_unknown",
                "zone_drift_above_target",
            },
            summary,
        )
        self.assertGreater(float(summary.get("preferred_followup_weight", 0.0) or 0.0), 0.0, summary)
        inquiry_types = list(summary.get("inquiry_candidate_types", []))
        self.assertTrue(inquiry_types, summary)
        self.assertIn(
            inquiry_types[0],
            {
                "stale_zone_detected",
                "persistent_zone_degradation",
                "zone_uncertainty_above_target",
                "key_object_unknown",
                "zone_drift_above_target",
            },
            summary,
        )

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective88_3-test",
                "source": source,
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
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
            ),
            None,
        )
        self.assertIsNotNone(question, questions)
        candidate_paths = question.get("candidate_answer_paths", []) if isinstance(question, dict) else []
        self.assertGreaterEqual(len(candidate_paths), 2, question)
        first_path = candidate_paths[0] if isinstance(candidate_paths[0], dict) else {}
        self.assertEqual(str(first_path.get("path_id", "")), "stabilize_scope_now", candidate_paths)
        self.assertGreater(float(first_path.get("proposal_arbitration_weight", 0.0) or 0.0), 0.0, first_path)
        self.assertTrue(bool((first_path.get("proposal_arbitration_learning", {}) if isinstance(first_path.get("proposal_arbitration_learning", {}), dict) else {}).get("applied", False)), first_path)

    def test_arbitration_learning_influences_commitment_monitoring_expectation(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective88_3-commitment-{run_id}"
        source = "objective88_3-commitment"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)
        self._seed_strategy_and_boundary(scope=scope, run_id=run_id, source=source)
        commitment_id = self._create_commitment(scope=scope, run_id=run_id)
        for _ in range(3):
            self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)

        status, baseline = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective88_3-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id, "phase": "baseline"},
            },
        )
        self.assertEqual(status, 200, baseline)
        baseline_monitoring = baseline.get("monitoring", {}) if isinstance(baseline, dict) else {}
        baseline_reasoning = baseline_monitoring.get("reasoning", {}) if isinstance(baseline_monitoring, dict) else {}
        baseline_expectation = baseline_reasoning.get("proposal_arbitration_expectation", {}) if isinstance(baseline_reasoning.get("proposal_arbitration_expectation", {}), dict) else {}
        self.assertFalse(bool(baseline_expectation.get("applied", False)), baseline_expectation)

        self._seed_arbitration_learning(run_id=run_id, scope=scope)

        status, evaluated = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective88_3-test",
                "source": source,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id, "phase": "biased"},
            },
        )
        self.assertEqual(status, 200, evaluated)
        monitoring = evaluated.get("monitoring", {}) if isinstance(evaluated, dict) else {}
        reasoning = monitoring.get("reasoning", {}) if isinstance(monitoring, dict) else {}
        expectation = reasoning.get("proposal_arbitration_expectation", {}) if isinstance(reasoning.get("proposal_arbitration_expectation", {}), dict) else {}
        self.assertTrue(bool(expectation.get("applied", False)), expectation)
        self.assertGreater(float(expectation.get("expectation_weight", 0.0) or 0.0), 0.0, expectation)
        self.assertIn("rescan_zone", list(expectation.get("proposal_types", [])), expectation)
        self.assertLessEqual(
            float(monitoring.get("drift_score", 1.0) or 1.0),
            float(baseline_monitoring.get("drift_score", 1.0) or 1.0),
            monitoring,
        )
        self.assertGreaterEqual(
            float(monitoring.get("health_score", 0.0) or 0.0),
            float(baseline_monitoring.get("health_score", 0.0) or 0.0),
            monitoring,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)