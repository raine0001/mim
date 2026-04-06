import asyncio
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url
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


def cleanup_objective88_2_rows() -> None:
    asyncio.run(_cleanup_objective88_2_rows_async())


async def _cleanup_objective88_2_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE source = 'objective88_2'"
        )
        await conn.execute(
            "DELETE FROM workspace_proposals WHERE source = 'objective88_2'"
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
                '', 'objective88_2', $5, NULL, NULL,
                $6::jsonb, $7::jsonb, $8
            )
            RETURNING id
            """,
            str(proposal_type),
            f"objective88_2 {proposal_type} {run_id}",
            f"seeded proposal {proposal_type} for objective88_2 {run_id}",
            float(confidence),
            str(related_zone),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id}),
            created_at,
        )
        return int(row["id"])
    finally:
        await conn.close()


class Objective88_2ProposalArbitrationLearningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 88.2",
            base_url=BASE_URL,
            require_proposal_arbitration_learning=True,
        )
        cleanup_objective88_2_rows()

    def setUp(self) -> None:
        cleanup_objective88_2_rows()

    def tearDown(self) -> None:
        cleanup_objective88_2_rows()

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

    def _seed_workspace_signal(self, *, run_id: str, scope: str) -> None:
        self._register_workspace_scan()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective88_2 strategy workspace check {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scope,
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
                            "label": f"obj88_2-tool-{run_id}",
                            "zone": scope,
                            "confidence": 0.92,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_strategy_goals(self, *, run_id: str, scope: str) -> list[dict]:
        status, built = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective88_2-test",
                "source": "objective88_2-strategy-learning",
                "lookback_hours": 24,
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
        self.assertEqual(status, 200, built)
        goals = built.get("goals", []) if isinstance(built, dict) else []
        self.assertGreaterEqual(len(goals), 1, built)
        return goals

    def test_arbitration_learning_biases_workspace_proposal_scores(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"objective88_2-zone-{run_id}"
        alpha_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="proposal_type_alpha",
            related_zone=zone,
            confidence=0.82,
            age_seconds=10,
        )
        beta_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="proposal_type_beta",
            related_zone=zone,
            confidence=0.72,
            age_seconds=20,
        )

        status, alpha_before = get_json(f"/workspace/proposals/{alpha_id}")
        self.assertEqual(status, 200, alpha_before)
        status, beta_before = get_json(f"/workspace/proposals/{beta_id}")
        self.assertEqual(status, 200, beta_before)
        self.assertGreater(
            float(alpha_before.get("priority_score", 0.0) or 0.0),
            float(beta_before.get("priority_score", 0.0) or 0.0),
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective88_2",
                    "proposal_id": alpha_id,
                    "arbitration_decision": "lost",
                    "arbitration_posture": "isolate",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "rejected",
                    "reason": "alpha repeatedly lost arbitration under this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective88_2",
                    "proposal_id": beta_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "beta repeatedly won arbitration under this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        status, learning = get_json(
            "/workspace/proposals/arbitration-learning",
            {"related_zone": zone},
        )
        self.assertEqual(status, 200, learning)
        rows = learning.get("learning", []) if isinstance(learning, dict) else []
        self.assertGreaterEqual(len(rows), 2, learning)
        by_type = {str(item.get("proposal_type", "")): item for item in rows if isinstance(item, dict)}
        self.assertLess(float((by_type.get("proposal_type_alpha") or {}).get("priority_bias", 0.0) or 0.0), 0.0)
        self.assertGreater(float((by_type.get("proposal_type_beta") or {}).get("priority_bias", 0.0) or 0.0), 0.0)

        status, alpha_after = get_json(f"/workspace/proposals/{alpha_id}")
        self.assertEqual(status, 200, alpha_after)
        status, beta_after = get_json(f"/workspace/proposals/{beta_id}")
        self.assertEqual(status, 200, beta_after)
        self.assertGreater(
            float(beta_after.get("priority_score", 0.0) or 0.0),
            float(alpha_after.get("priority_score", 0.0) or 0.0),
        )
        beta_learning = beta_after.get("arbitration_learning", {}) if isinstance(beta_after.get("arbitration_learning", {}), dict) else {}
        self.assertTrue(beta_learning.get("applied"), beta_after)

    def test_arbitration_outcomes_are_listed_and_merged_results_count_positive(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"objective88_2-zone-{run_id}"
        gamma_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="proposal_type_gamma",
            related_zone=zone,
            confidence=0.7,
            age_seconds=15,
        )

        for decision, posture in [("merged", "merge"), ("won", "isolate")]:
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective88_2",
                    "proposal_id": gamma_id,
                    "arbitration_decision": decision,
                    "arbitration_posture": posture,
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": f"gamma {decision} under arbitration",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        status, listed = get_json(
            "/workspace/proposals/arbitration-outcomes",
            {"proposal_type": "proposal_type_gamma", "related_zone": zone},
        )
        self.assertEqual(status, 200, listed)
        outcomes = listed.get("outcomes", []) if isinstance(listed, dict) else []
        self.assertEqual(len(outcomes), 2, listed)

        status, learning = get_json(
            "/workspace/proposals/arbitration-learning",
            {"proposal_type": "proposal_type_gamma", "related_zone": zone},
        )
        self.assertEqual(status, 200, learning)
        payload = (learning.get("learning", []) if isinstance(learning, dict) else [])[0]
        self.assertEqual(str(payload.get("learned_posture", "")), "favored")
        self.assertGreater(float(payload.get("weighted_success_rate", 0.0) or 0.0), 0.7)

    def test_arbitration_learning_influences_strategy_goal_weighting(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective88_2-strategy-{run_id}"

        self._seed_workspace_signal(run_id=run_id, scope=scope)

        baseline_goals = self._build_strategy_goals(run_id=run_id, scope=scope)
        baseline_stabilize = next(
            (
                item
                for item in baseline_goals
                if isinstance(item, dict)
                and str(item.get("strategy_type", ""))
                == "stabilize_uncertain_zones_before_action"
            ),
            None,
        )
        self.assertIsNotNone(baseline_stabilize, baseline_goals)
        baseline_ranking = (
            baseline_stabilize.get("ranking_factors", {})
            if isinstance(baseline_stabilize.get("ranking_factors", {}), dict)
            else {}
        )
        self.assertEqual(
            float(baseline_ranking.get("proposal_arbitration_strategy_weight", 0.0) or 0.0),
            0.0,
            baseline_stabilize,
        )

        for proposal_type in ["rescan_zone", "confirm_target_ready"]:
            for _ in range(4):
                status, outcome = post_json(
                    "/workspace/proposals/arbitration-outcomes",
                    {
                        "actor": "tod",
                        "source": "objective88_2",
                        "proposal_type": proposal_type,
                        "related_zone": scope,
                        "arbitration_decision": "won",
                        "arbitration_posture": "merge",
                        "trust_chain_status": "verified",
                        "downstream_execution_outcome": "accepted",
                        "reason": f"{proposal_type} repeatedly wins arbitration in this scope",
                        "metadata_json": {"run_id": run_id},
                    },
                )
                self.assertEqual(status, 200, outcome)

        goals = self._build_strategy_goals(run_id=run_id, scope=scope)
        stabilize = next(
            (
                item
                for item in goals
                if isinstance(item, dict)
                and str(item.get("strategy_type", ""))
                == "stabilize_uncertain_zones_before_action"
            ),
            None,
        )
        self.assertIsNotNone(stabilize, goals)

        ranking_factors = (
            stabilize.get("ranking_factors", {})
            if isinstance(stabilize.get("ranking_factors", {}), dict)
            else {}
        )
        reasoning = (
            stabilize.get("reasoning", {})
            if isinstance(stabilize.get("reasoning", {}), dict)
            else {}
        )
        arbitration_learning = (
            reasoning.get("proposal_arbitration_learning", {})
            if isinstance(reasoning.get("proposal_arbitration_learning", {}), dict)
            else {}
        )

        self.assertGreater(
            float(ranking_factors.get("proposal_arbitration_strategy_weight", 0.0) or 0.0),
            0.0,
            ranking_factors,
        )
        self.assertGreaterEqual(
            int(ranking_factors.get("proposal_arbitration_sample_count", 0) or 0),
            8,
            ranking_factors,
        )
        self.assertEqual(
            sorted(ranking_factors.get("proposal_arbitration_proposal_types", [])),
            [
                "confirm_target_ready",
                "monitor_search_adjacent_zone",
                "rescan_zone",
                "verify_moved_object",
            ],
            ranking_factors,
        )
        self.assertTrue(arbitration_learning.get("applied"), arbitration_learning)
        self.assertEqual(str(arbitration_learning.get("related_zone", "")), scope)
        self.assertIn(
            "Proposal arbitration outcomes boosted this strategy",
            str(stabilize.get("reasoning_summary", "")),
        )
        self.assertGreater(
            float(stabilize.get("priority_score", 0.0) or 0.0),
            float(baseline_stabilize.get("priority_score", 0.0) or 0.0),
            {"before": baseline_stabilize, "after": stabilize},
        )


if __name__ == "__main__":
    unittest.main()