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


class Objective54SelfGuidedImprovementLoopTest(unittest.TestCase):
    TARGET_COMPONENT = "environment_strategy:preemptive_zone_stabilization"

    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective54-test",
                "source": "objective54-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj54-{run_id}-{zone_suffix}",
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
        self.assertGreaterEqual(len(strategies), 1)
        strategy_id = int((strategies[0] or {}).get("strategy_id", 0))
        self.assertGreater(strategy_id, 0)
        return strategy_id

    def _deactivate_strategy(self, *, strategy_id: int, run_id: str) -> None:
        status, payload = post_json(
            f"/planning/strategies/{strategy_id}/deactivate",
            {
                "actor": "objective54-test",
                "reason": "objective54 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _clear_open_target_component_proposals(self, *, run_id: str) -> None:
        status, listed = get_json(
            "/improvement/proposals",
            {
                "status": "proposed",
                "limit": 500,
            },
        )
        self.assertEqual(status, 200, listed)
        rows = listed.get("proposals", []) if isinstance(listed, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("affected_component", "")) != self.TARGET_COMPONENT:
                continue
            proposal_id = int(row.get("proposal_id", 0) or 0)
            if proposal_id <= 0:
                continue
            status, payload = post_json(
                f"/improvement/proposals/{proposal_id}/reject",
                {
                    "actor": "objective54-test",
                    "reason": "cleanup open duplicate before focused gate",
                    "metadata_json": {"run_id": run_id, "cleanup": True},
                },
            )
            self.assertEqual(status, 200, payload)

    def test_objective54_self_guided_loop(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, dev_patterns = get_json(
            "/memory/development-patterns",
            {
                "refresh": "true",
                "lookback_hours": 168,
                "min_evidence_count": 2,
                "pattern_type": "strategy_underperforming",
                "limit": 100,
            },
        )
        self.assertEqual(status, 200, dev_patterns)
        rows = dev_patterns.get("development_patterns", []) if isinstance(dev_patterns, dict) else []
        target_pattern = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and str(item.get("pattern_type", "")) == "strategy_underperforming"
                and str(item.get("affected_component", "")) == self.TARGET_COMPONENT
            ),
            None,
        )
        self.assertIsNotNone(target_pattern, rows)

        self._clear_open_target_component_proposals(run_id=run_id)

        status, generated = post_json(
            "/improvement/recommendations/generate",
            {
                "actor": "objective54-test",
                "source": "objective54-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_recommendations": 5,
                "include_existing_open_proposals": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        recommendations = generated.get("recommendations", []) if isinstance(generated.get("recommendations", []), list) else []
        self.assertGreaterEqual(len(recommendations), 1, generated)

        rec = next(
            (
                item
                for item in recommendations
                if isinstance(item, dict)
                and str((item.get("metadata_json", {}) if isinstance(item.get("metadata_json", {}), dict) else {}).get("proposal_trigger_pattern", ""))
                == "development_pattern_trigger"
            ),
            None,
        )
        self.assertIsNotNone(rec, recommendations)

        recommendation_id = int(rec.get("recommendation_id", 0) or 0)
        proposal_id = int(rec.get("proposal_id", 0) or 0)
        experiment_id = int(rec.get("experiment_id", 0) or 0)
        self.assertGreater(recommendation_id, 0)
        self.assertGreater(proposal_id, 0)
        self.assertGreater(experiment_id, 0)

        status, proposal_detail = get_json(f"/improvement/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        proposal = proposal_detail.get("proposal", {}) if isinstance(proposal_detail, dict) else {}
        self.assertEqual(str(proposal.get("trigger_pattern", "")), "development_pattern_trigger")

        baseline = rec.get("baseline_metrics", {}) if isinstance(rec.get("baseline_metrics", {}), dict) else {}
        experimental = rec.get("experimental_metrics", {}) if isinstance(rec.get("experimental_metrics", {}), dict) else {}
        comparison = rec.get("comparison", {}) if isinstance(rec.get("comparison", {}), dict) else {}

        for metrics in [baseline, experimental]:
            self.assertIn("success_rate", metrics)
            self.assertIn("execution_time_ms", metrics)
            self.assertIn("replan_frequency", metrics)
            self.assertIn("operator_override_rate", metrics)

        self.assertIn("success_rate_delta", comparison)
        self.assertIn("execution_time_ms_delta", comparison)
        self.assertIn("replan_frequency_delta", comparison)
        self.assertIn("operator_override_rate_delta", comparison)

        status, listed = get_json("/improvement/recommendations", {"limit": 100})
        self.assertEqual(status, 200, listed)
        listed_rows = listed.get("recommendations", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("recommendation_id", 0)) == recommendation_id for item in listed_rows if isinstance(item, dict)))

        status, detail = get_json(f"/improvement/recommendations/{recommendation_id}")
        self.assertEqual(status, 200, detail)
        detail_row = detail.get("recommendation", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_row.get("recommendation_id", 0)), recommendation_id)

        status, approved = post_json(
            f"/improvement/recommendations/{recommendation_id}/approve",
            {
                "actor": "operator",
                "reason": "objective54 focused recommendation review",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, approved)
        approved_row = approved.get("recommendation", {}) if isinstance(approved, dict) else {}
        self.assertEqual(str(approved_row.get("status", "")), "approved")
        latest_artifact = approved_row.get("latest_artifact", {}) if isinstance(approved_row.get("latest_artifact", {}), dict) else {}
        self.assertEqual(str(latest_artifact.get("artifact_type", "")), "promotion_recommendation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
