import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from uuid import uuid4


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective165SelfEvolutionNextActionTest(unittest.TestCase):
    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective165-test",
                "source": "objective165-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj165-{run_id}-{zone_suffix}",
                        "severity": 0.84,
                        "occurrence_count": 2,
                        "metadata_json": {"run_id": run_id},
                    }
                ],
                "min_severity": 0.2,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        strategies = payload.get("strategies", []) if isinstance(payload.get("strategies", []), list) else []
        self.assertGreaterEqual(len(strategies), 1, payload)
        strategy_id = int((strategies[0] or {}).get("strategy_id", 0))
        self.assertGreater(strategy_id, 0)
        return strategy_id

    def _deactivate_strategy(self, *, strategy_id: int, run_id: str) -> None:
        status, payload = post_json(
            f"/planning/strategies/{strategy_id}/deactivate",
            {
                "actor": "objective165-test",
                "reason": "objective165 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_objective165_self_evolution_next_action(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, payload = get_json(
            "/improvement/self-evolution/next-action",
            {
                "refresh": "true",
                "actor": "objective165-test",
                "source": "objective165-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "auto_experiment_limit": 3,
                "limit": 10,
            },
        )
        self.assertEqual(status, 200, payload)
        decision = payload.get("decision", {}) if isinstance(payload, dict) else {}

        self.assertIn(
            str(decision.get("decision_type", "")),
            {
                "approve_ranked_recommendation",
                "review_open_recommendation",
                "inspect_ranked_backlog_item",
                "generate_recommendations",
                "refresh_self_evolution_state",
            },
        )
        self.assertIn(str(decision.get("priority", "")), {"high", "medium", "low"})
        self.assertTrue(str(decision.get("rationale", "")).strip())
        self.assertTrue(str(decision.get("summary", "")).strip())
        self.assertIn(str(decision.get("snapshot_status", "")), {"active", "operator_review_required", "quiet"})
        self.assertTrue(str(decision.get("snapshot_summary", "")).strip())

        action = decision.get("action", {}) if isinstance(decision.get("action", {}), dict) else {}
        self.assertIn(str(action.get("method", "")), {"GET", "POST"})
        self.assertTrue(str(action.get("path", "")).startswith("/improvement/"))
        self.assertTrue(isinstance(action.get("payload", {}), dict))

        metadata = decision.get("metadata_json", {}) if isinstance(decision.get("metadata_json", {}), dict) else {}
        self.assertTrue(bool(metadata.get("objective165_self_evolution_next_action", False)))

        if decision.get("target_id") is not None:
            self.assertGreater(int(decision.get("target_id", 0) or 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)