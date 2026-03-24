import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


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


class Objective80ExecutionTruthStrategyScoringTest(unittest.TestCase):
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
                "text": f"objective80 strategy workspace check {run_id}",
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
                            "label": f"obj80-tool-{run_id}",
                            "zone": scope,
                            "confidence": 0.92,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_communication_signal(self, *, run_id: str) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"what should we prioritize next {run_id}",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_execution_truth_signal(
        self,
        *,
        run_id: str,
        scope: str,
        published_at: str | None = None,
    ) -> int:
        capability_name = f"execution_truth_strategy_probe_{run_id}"
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80 strategy scoring probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run execution truth strategy probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for strategy scoring",
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
                "feedback_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)

        effective_published_at = published_at or datetime.now(timezone.utc).isoformat()
        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "runtime mismatch recorded",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"run_id": run_id, "managed_scope": scope},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": 1680,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.93,
                    "published_at": effective_published_at,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        return execution_id

    def _build_strategy_goals(self, *, run_id: str, scope: str = "") -> list[dict]:
        status, built = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-strategy",
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.4,
                "min_domains_required": 2,
                "min_cross_domain_links": 1,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {
                    "run_id": run_id,
                    **({"managed_scope": scope} if scope else {}),
                },
            },
        )
        self.assertEqual(status, 200, built)
        goals = built.get("goals", []) if isinstance(built, dict) else []
        self.assertGreaterEqual(len(goals), 1, built)
        return goals

    def test_execution_truth_influences_strategy_scoring(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"strategy-scope-{run_id}"

        self._seed_workspace_signal(run_id=run_id, scope=scope)
        self._seed_communication_signal(run_id=run_id)
        execution_id = self._seed_execution_truth_signal(run_id=run_id, scope=scope)

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
        execution_truth_influence = (
            reasoning.get("execution_truth_influence", {})
            if isinstance(reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )
        supporting_evidence = (
            stabilize.get("supporting_evidence", {})
            if isinstance(stabilize.get("supporting_evidence", {}), dict)
            else {}
        )

        self.assertGreaterEqual(
            int(ranking_factors.get("execution_truth_signal_count", 0) or 0),
            5,
            ranking_factors,
        )
        self.assertGreater(
            float(ranking_factors.get("execution_truth_strategy_weight", 0.0) or 0.0),
            0.0,
            ranking_factors,
        )
        self.assertIn(
            "simulation_reality_mismatch",
            ranking_factors.get("execution_truth_signal_types", []),
            ranking_factors,
        )

        self.assertGreaterEqual(
            int(execution_truth_influence.get("execution_count", 0) or 0),
            1,
            execution_truth_influence,
        )
        self.assertGreaterEqual(
            int(execution_truth_influence.get("deviation_signal_count", 0) or 0),
            5,
            execution_truth_influence,
        )
        self.assertIn(
            "simulation_reality_mismatch",
            execution_truth_influence.get("signal_types", []),
            execution_truth_influence,
        )
        self.assertIn(
            "reconfirm",
            str(execution_truth_influence.get("strategy_rationale", "")).lower(),
        )

        self.assertGreaterEqual(
            int(supporting_evidence.get("execution_truth_signal_count", 0) or 0),
            5,
            supporting_evidence,
        )
        self.assertIn(
            "environment_shift_during_execution",
            supporting_evidence.get("execution_truth_signal_types", []),
            supporting_evidence,
        )
        self.assertIn(
            "runtime mismatch",
            str(stabilize.get("reasoning_summary", "")).lower(),
        )

        status, detail = get_json(
            f"/strategy/goals/{int(stabilize.get('strategy_goal_id', 0) or 0)}"
        )
        self.assertEqual(status, 200, detail)
        goal = detail.get("goal", {}) if isinstance(detail, dict) else {}
        goal_reasoning = (
            goal.get("reasoning", {}) if isinstance(goal.get("reasoning", {}), dict) else {}
        )
        goal_execution_truth = (
            goal_reasoning.get("execution_truth_influence", {})
            if isinstance(goal_reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )
        self.assertIn(
            execution_id,
            [
                int(item.get("execution_id", 0) or 0)
                for item in (
                    goal_execution_truth.get("recent_executions", [])
                    if isinstance(goal_execution_truth.get("recent_executions", []), list)
                    else []
                )
                if isinstance(item, dict)
            ],
            goal_execution_truth,
        )

    def test_fresh_execution_truth_elevates_stabilization_above_readiness(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"strategy-compare-{run_id}"

        self._seed_workspace_signal(run_id=run_id, scope=scope)
        self._seed_communication_signal(run_id=run_id)
        self._seed_execution_truth_signal(run_id=run_id, scope=scope)

        goals = self._build_strategy_goals(run_id=run_id, scope=scope)
        by_type = {
            str(item.get("strategy_type", "")): item
            for item in goals
            if isinstance(item, dict)
        }
        stabilize = by_type.get("stabilize_uncertain_zones_before_action")
        readiness = by_type.get("maintain_workspace_readiness")
        self.assertIsNotNone(stabilize, goals)
        self.assertIsNotNone(readiness, goals)

        stabilize_factors = (
            stabilize.get("ranking_factors", {})
            if isinstance(stabilize.get("ranking_factors", {}), dict)
            else {}
        )
        readiness_factors = (
            readiness.get("ranking_factors", {})
            if isinstance(readiness.get("ranking_factors", {}), dict)
            else {}
        )
        self.assertGreater(
            float(stabilize_factors.get("execution_truth_strategy_weight", 0.0) or 0.0),
            float(readiness_factors.get("execution_truth_strategy_weight", 0.0) or 0.0),
            {"stabilize": stabilize, "readiness": readiness},
        )
        self.assertGreater(
            float(
                (
                    stabilize_factors.get("execution_truth_freshness", {})
                    if isinstance(stabilize_factors.get("execution_truth_freshness", {}), dict)
                    else {}
                ).get("freshness_weight", 0.0)
                or 0.0
            ),
            0.0,
        )
        self.assertIn(
            "runtime mismatch",
            str((stabilize or {}).get("reasoning_summary", "")).lower(),
        )

    def test_stale_execution_truth_decays_strategy_weight(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"strategy-stale-{run_id}"

        self._seed_workspace_signal(run_id=run_id, scope=scope)
        self._seed_communication_signal(run_id=run_id)
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        self._seed_execution_truth_signal(
            run_id=run_id,
            scope=scope,
            published_at=stale_time,
        )

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
        factors = (
            stabilize.get("ranking_factors", {})
            if isinstance(stabilize.get("ranking_factors", {}), dict)
            else {}
        )
        freshness = (
            factors.get("execution_truth_freshness", {})
            if isinstance(factors.get("execution_truth_freshness", {}), dict)
            else {}
        )
        self.assertEqual(
            float(freshness.get("freshness_weight", 0.0) or 0.0),
            0.0,
            factors,
        )
        self.assertEqual(
            float(factors.get("execution_truth_strategy_weight", 0.0) or 0.0),
            0.0,
            factors,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)