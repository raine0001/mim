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


class Objective166SelfEvolutionBriefingTest(unittest.TestCase):
    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective166-test",
                "source": "objective166-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj166-{run_id}-{zone_suffix}",
                        "severity": 0.85,
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
                "actor": "objective166-test",
                "reason": "objective166 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_objective166_self_evolution_briefing(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, payload = get_json(
            "/improvement/self-evolution/briefing",
            {
                "refresh": "true",
                "actor": "objective166-test",
                "source": "objective166-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "auto_experiment_limit": 3,
                "limit": 10,
            },
        )
        self.assertEqual(status, 200, payload)
        briefing = payload.get("briefing", {}) if isinstance(payload, dict) else {}

        snapshot = briefing.get("snapshot", {}) if isinstance(briefing.get("snapshot", {}), dict) else {}
        decision = briefing.get("decision", {}) if isinstance(briefing.get("decision", {}), dict) else {}
        target = briefing.get("target", {}) if isinstance(briefing.get("target", {}), dict) else {}
        metadata = briefing.get("metadata_json", {}) if isinstance(briefing.get("metadata_json", {}), dict) else {}

        self.assertTrue(str(snapshot.get("summary", "")).strip())
        self.assertTrue(str(decision.get("decision_type", "")).strip())
        self.assertTrue(bool(metadata.get("objective166_self_evolution_briefing", False)))

        self.assertEqual(str(target.get("target_kind", "")), str(decision.get("target_kind", "")))
        self.assertEqual(target.get("target_id"), decision.get("target_id"))

        target_kind = str(target.get("target_kind", "") or "")
        if target_kind == "recommendation":
            recommendation = target.get("recommendation", {}) if isinstance(target.get("recommendation", {}), dict) else {}
            self.assertGreater(int(recommendation.get("recommendation_id", 0) or 0), 0)
            proposal = target.get("proposal", {}) if isinstance(target.get("proposal", {}), dict) else {}
            self.assertGreater(int(proposal.get("proposal_id", 0) or 0), 0)
        if target_kind == "backlog_item":
            backlog_item = target.get("backlog_item", {}) if isinstance(target.get("backlog_item", {}), dict) else {}
            self.assertGreater(int(backlog_item.get("improvement_id", 0) or 0), 0)
            proposal = target.get("proposal", {}) if isinstance(target.get("proposal", {}), dict) else {}
            self.assertGreater(int(proposal.get("proposal_id", 0) or 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)