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


class Objective164SelfEvolutionCoreTest(unittest.TestCase):
    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective164-test",
                "source": "objective164-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj164-{run_id}-{zone_suffix}",
                        "severity": 0.82,
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
                "actor": "objective164-test",
                "reason": "objective164 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_objective164_self_evolution_snapshot(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, payload = get_json(
            "/improvement/self-evolution",
            {
                "refresh": "true",
                "actor": "objective164-test",
                "source": "objective164-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "auto_experiment_limit": 3,
                "limit": 10,
            },
        )
        self.assertEqual(status, 200, payload)
        snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}

        self.assertIn(str(snapshot.get("status", "")), {"active", "operator_review_required", "quiet"})
        self.assertTrue(str(snapshot.get("summary", "")).strip())

        proposal_counts = snapshot.get("proposal_counts", {}) if isinstance(snapshot.get("proposal_counts", {}), dict) else {}
        recommendation_counts = snapshot.get("recommendation_counts", {}) if isinstance(snapshot.get("recommendation_counts", {}), dict) else {}
        backlog_counts = snapshot.get("backlog_counts", {}) if isinstance(snapshot.get("backlog_counts", {}), dict) else {}
        risk_counts = snapshot.get("risk_counts", {}) if isinstance(snapshot.get("risk_counts", {}), dict) else {}
        decision_counts = snapshot.get("governance_decision_counts", {}) if isinstance(snapshot.get("governance_decision_counts", {}), dict) else {}

        self.assertTrue(isinstance(proposal_counts, dict))
        self.assertTrue(isinstance(recommendation_counts, dict))
        self.assertTrue(isinstance(backlog_counts, dict))
        self.assertTrue(isinstance(risk_counts, dict))
        self.assertTrue(isinstance(decision_counts, dict))

        backlog = snapshot.get("backlog", []) if isinstance(snapshot.get("backlog", []), list) else []
        proposals = snapshot.get("proposals", []) if isinstance(snapshot.get("proposals", []), list) else []
        recommendations = snapshot.get("recommendations", []) if isinstance(snapshot.get("recommendations", []), list) else []

        self.assertGreaterEqual(len(backlog), 1, snapshot)
        self.assertGreaterEqual(len(proposals), 1, snapshot)
        self.assertGreaterEqual(len(recommendations), 1, snapshot)

        top_backlog = backlog[0] if isinstance(backlog[0], dict) else {}
        self.assertGreaterEqual(float(snapshot.get("top_priority_score", 0.0) or 0.0), 0.0)
        self.assertEqual(
            str(snapshot.get("top_priority_type", "")),
            str(top_backlog.get("proposal_type", "")),
        )
        self.assertTrue(str(snapshot.get("top_affected_component", "")).strip())

        metadata = snapshot.get("metadata_json", {}) if isinstance(snapshot.get("metadata_json", {}), dict) else {}
        self.assertTrue(bool(metadata.get("objective164_self_evolution", False)))
        self.assertTrue(bool(metadata.get("refresh_requested", False)))
        self.assertEqual(int(metadata.get("lookback_hours", 0) or 0), 168)


if __name__ == "__main__":
    unittest.main(verbosity=2)