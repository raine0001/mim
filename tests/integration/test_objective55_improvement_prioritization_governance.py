import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
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


class Objective55ImprovementPrioritizationGovernanceTest(unittest.TestCase):
    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective55-test",
                "source": "objective55-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj55-{run_id}-{zone_suffix}",
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
                "actor": "objective55-test",
                "reason": "objective55 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_objective55_prioritization_and_governance_backlog(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, refreshed = post_json(
            "/improvement/backlog/refresh",
            {
                "actor": "objective55-test",
                "source": "objective55-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_items": 100,
                "auto_experiment_limit": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, refreshed)
        backlog = refreshed.get("items", []) if isinstance(refreshed, dict) else []
        self.assertGreaterEqual(len(backlog), 1, refreshed)

        top = backlog[0] if isinstance(backlog[0], dict) else {}
        self.assertGreater(int(top.get("improvement_id", 0) or 0), 0)
        self.assertGreaterEqual(float(top.get("priority_score", 0.0) or 0.0), 0.0)
        self.assertIn("proposal_type", top)
        self.assertIn("evidence_count", top)
        self.assertIn("risk_level", top)
        self.assertIn("status", top)

        scores = [
            float(item.get("priority_score", 0.0) or 0.0)
            for item in backlog
            if isinstance(item, dict)
        ]
        self.assertEqual(scores, sorted(scores, reverse=True), backlog)

        valid_decisions = {
            "auto_experiment",
            "request_operator_review",
            "defer_improvement",
            "reject_improvement",
        }
        valid_statuses = {
            "proposed",
            "queued",
            "experimenting",
            "evaluating",
            "recommended",
            "approved",
            "rejected",
        }

        for row in backlog:
            if not isinstance(row, dict):
                continue
            self.assertIn(str(row.get("governance_decision", "")), valid_decisions)
            self.assertIn(str(row.get("status", "")), valid_statuses)
            self.assertIn("impact_estimate", row)
            self.assertIn("evidence_strength", row)
            self.assertIn("operator_preference_weight", row)
            self.assertTrue(isinstance(row.get("affected_capabilities", []), list))

        status, listed = get_json(
            "/improvement/backlog",
            {
                "status": "",
                "risk_level": "",
                "limit": 100,
            },
        )
        self.assertEqual(status, 200, listed)
        listed_rows = listed.get("backlog", []) if isinstance(listed, dict) else []
        self.assertGreaterEqual(len(listed_rows), 1, listed)

        improvement_id = int(top.get("improvement_id", 0) or 0)
        self.assertGreater(improvement_id, 0)

        status, detail = get_json(f"/improvement/backlog/{improvement_id}")
        self.assertEqual(status, 200, detail)
        item = detail.get("backlog_item", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(item.get("improvement_id", 0) or 0), improvement_id)
        self.assertTrue(str(item.get("why_ranked", "")).strip())
        self.assertTrue(str(item.get("evidence_summary", "")).strip())
        self.assertTrue(str(item.get("risk_summary", "")).strip())

        reasoning = item.get("reasoning", {}) if isinstance(item.get("reasoning", {}), dict) else {}
        self.assertIn("impact_estimate", reasoning)
        self.assertIn("evidence_strength", reasoning)
        self.assertIn("risk_level", reasoning)
        self.assertIn("governance_policy", reasoning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
