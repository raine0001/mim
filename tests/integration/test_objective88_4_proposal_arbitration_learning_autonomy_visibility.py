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


def cleanup_objective88_4_rows() -> None:
    cleanup_objective87_rows()
    asyncio.run(_cleanup_objective88_4_rows_async())


async def _cleanup_objective88_4_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE source = 'objective88_4'"
        )
        await conn.execute(
            "DELETE FROM workspace_proposals WHERE source = 'objective88_4'"
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
                '', 'objective88_4', $5, NULL, NULL,
                $6::jsonb, $7::jsonb, $8
            )
            RETURNING id
            """,
            str(proposal_type),
            f"objective88_4 {proposal_type} {run_id}",
            f"seeded proposal {proposal_type} for objective88_4 {run_id}",
            float(confidence),
            str(related_zone),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id}),
            created_at,
        )
        return int(row["id"])
    finally:
        await conn.close()


class Objective88_4ProposalArbitrationLearningAutonomyVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 88.4",
            base_url=BASE_URL,
            require_proposal_arbitration_learning=True,
            require_ui_state=True,
        )
        cleanup_objective88_4_rows()

    def setUp(self) -> None:
        cleanup_objective88_4_rows()

    def tearDown(self) -> None:
        cleanup_objective88_4_rows()

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
                "text": f"objective88_4 stale scan {run_id}",
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
                            "label": f"obj88_4-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_stewardship_scope(self, *, scope: str, run_id: str) -> None:
        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)
        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.82,
                "confidence": 0.9,
                "source": "objective88_4-autonomy-review",
            },
        )
        self.assertEqual(status, 200, pref)
        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective88_4-test",
                "source": "objective88_4-autonomy-review",
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
        status, cycle = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective88_4-test",
                "source": "objective88_4-autonomy-review",
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
                    "key_objects": [f"objective88_4-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, cycle)

    def _record_arbitration_outcome(self, *, proposal_id: int, proposal_type: str, scope: str) -> None:
        status, payload = post_json(
            "/workspace/proposals/arbitration-outcomes",
            {
                "actor": "objective88_4-test",
                "source": "objective88_4",
                "proposal_id": proposal_id,
                "proposal_type": proposal_type,
                "related_zone": scope,
                "arbitration_decision": "won",
                "arbitration_posture": "integrate",
                "trust_chain_status": "verified",
                "downstream_execution_outcome": "succeeded",
                "confidence": 0.94,
                "arbitration_reason": f"{proposal_type} repeatedly wins in this scope",
                "conflict_context_json": {"run": "objective88_4"},
                "commitment_state_json": {"managed_scope": scope},
                "metadata_json": {"objective88_4": True},
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

    def _recompute(self, *, run_id: str, scope: str) -> dict:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective88_4-test",
                "source": "objective88_4-autonomy-review",
                "scope": scope,
                "lookback_hours": 48,
                "min_samples": 5,
                "apply_recommended_boundaries": True,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "sample_count": 20,
                    "success_rate": 0.98,
                    "escalation_rate": 0.0,
                    "retry_rate": 0.01,
                    "interruption_rate": 0.0,
                    "memory_delta_rate": 0.94,
                    "override_rate": 0.0,
                    "replan_rate": 0.0,
                    "environment_stability": 0.96,
                    "development_confidence": 0.93,
                    "constraint_reliability": 0.95,
                    "experiment_confidence": 0.94,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        return payload.get("boundary", {}) if isinstance(payload, dict) else {}

    def test_arbitration_learning_caps_autonomy_review_and_surfaces_in_ui(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective88_4-autonomy-{run_id}"

        self._seed_stewardship_scope(scope=scope, run_id=run_id)

        baseline = self._recompute(run_id=run_id, scope=scope)
        baseline_review = (
            baseline.get("proposal_arbitration_autonomy_review", {})
            if isinstance(baseline.get("proposal_arbitration_autonomy_review", {}), dict)
            else {}
        )
        self.assertFalse(bool(baseline_review.get("applied", False)), baseline_review)
        baseline_level = str(baseline.get("current_level", "")).strip()
        self.assertIn(
            baseline_level,
            {"bounded_auto", "trusted_auto"},
            baseline,
        )

        self._seed_arbitration_learning(run_id=run_id, scope=scope)

        biased = self._recompute(run_id=run_id, scope=scope)
        self.assertEqual(str(biased.get("current_level", "")), "bounded_auto", biased)
        review = (
            biased.get("proposal_arbitration_autonomy_review", {})
            if isinstance(biased.get("proposal_arbitration_autonomy_review", {}), dict)
            else {}
        )
        self.assertTrue(bool(review.get("applied", False)), review)
        self.assertGreater(float(review.get("review_weight", 0.0) or 0.0), 0.0, review)
        self.assertEqual(str(review.get("target_level_cap", "")), "bounded_auto", review)
        self.assertIn("rescan_zone", list(review.get("proposal_types", [])), review)

        reasoning = (
            biased.get("adaptation_reasoning", {})
            if isinstance(biased.get("adaptation_reasoning", {}), dict)
            else {}
        )
        self.assertTrue(bool(reasoning.get("proposal_arbitration_autonomy_review_applied", False)), reasoning)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
        autonomy = (
            operator_reasoning.get("autonomy", {})
            if isinstance(operator_reasoning.get("autonomy", {}), dict)
            else {}
        )
        self.assertEqual(str(autonomy.get("scope", "")), scope, autonomy)
        self.assertEqual(str(autonomy.get("current_level", "")), "bounded_auto", autonomy)
        ui_review = (
            autonomy.get("proposal_arbitration_review", {})
            if isinstance(autonomy.get("proposal_arbitration_review", {}), dict)
            else {}
        )
        self.assertTrue(bool(ui_review.get("applied", False)), ui_review)
        self.assertEqual(str(ui_review.get("target_level_cap", "")), "bounded_auto", ui_review)
        self.assertGreater(float(ui_review.get("review_weight", 0.0) or 0.0), 0.0, ui_review)


if __name__ == "__main__":
    unittest.main(verbosity=2)