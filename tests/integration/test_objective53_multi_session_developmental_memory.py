import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
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


class Objective53MultiSessionDevelopmentalMemoryTest(unittest.TestCase):
    TARGET_COMPONENT = "environment_strategy:preemptive_zone_stabilization"

    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective53-test",
                "source": "objective53-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj53-{run_id}-{zone_suffix}",
                        "severity": 0.82,
                        "occurrence_count": 2,
                        "metadata_json": {"run_id": run_id},
                    }
                ],
                "min_severity": 0.2,
                "max_strategies": 5,
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
                "actor": "objective53-test",
                "reason": "objective53 synthetic stall pattern",
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
                    "actor": "objective53-test",
                    "reason": "cleanup open duplicate before focused gate",
                    "metadata_json": {"run_id": run_id, "cleanup": True},
                },
            )
            self.assertEqual(status, 200, payload)

    def test_objective53_development_patterns_and_feedback(self) -> None:
        run_id = uuid4().hex[:8]

        stalled_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="stall-a"),
            self._create_strategy(run_id=run_id, zone_suffix="stall-b"),
        ]
        for strategy_id in stalled_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, listed = get_json(
            "/memory/development-patterns",
            {
                "refresh": "true",
                "lookback_hours": 168,
                "min_evidence_count": 2,
                "pattern_type": "strategy_underperforming",
                "limit": 100,
            },
        )
        self.assertEqual(status, 200, listed)
        rows = listed.get("development_patterns", []) if isinstance(listed, dict) else []
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

        pattern_id = int((target_pattern or {}).get("pattern_id", 0))
        before_evidence = int((target_pattern or {}).get("evidence_count", 0))
        before_confidence = float((target_pattern or {}).get("confidence", 0.0))
        self.assertGreater(pattern_id, 0)
        self.assertGreaterEqual(before_evidence, 2)

        status, detail = get_json(f"/memory/development-patterns/{pattern_id}")
        self.assertEqual(status, 200, detail)
        detail_pattern = detail.get("development_pattern", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_pattern.get("pattern_id", 0)), pattern_id)

        extra_strategy_id = self._create_strategy(run_id=run_id, zone_suffix="stall-c")
        self._deactivate_strategy(strategy_id=extra_strategy_id, run_id=run_id)

        status, listed2 = get_json(
            "/memory/development-patterns",
            {
                "refresh": "true",
                "lookback_hours": 168,
                "min_evidence_count": 2,
                "pattern_type": "strategy_underperforming",
                "limit": 100,
            },
        )
        self.assertEqual(status, 200, listed2)
        rows2 = listed2.get("development_patterns", []) if isinstance(listed2, dict) else []
        target_pattern2 = next(
            (
                item
                for item in rows2
                if isinstance(item, dict)
                and int(item.get("pattern_id", 0)) == pattern_id
            ),
            None,
        )
        self.assertIsNotNone(target_pattern2, rows2)
        self.assertGreater(int((target_pattern2 or {}).get("evidence_count", 0)), before_evidence)
        self.assertGreaterEqual(float((target_pattern2 or {}).get("confidence", 0.0)), before_confidence)

        active_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="active-a"),
            self._create_strategy(run_id=run_id, zone_suffix="active-b"),
        ]

        self._clear_open_target_component_proposals(run_id=run_id)

        status, generated = post_json(
            "/improvement/proposals/generate",
            {
                "actor": "objective53-test",
                "source": "objective53-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_proposals": 10,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        proposals = generated.get("proposals", []) if isinstance(generated.get("proposals", []), list) else []

        target_proposal = next(
            (
                item
                for item in proposals
                if isinstance(item, dict)
                and str(item.get("affected_component", "")) == self.TARGET_COMPONENT
            ),
            None,
        )
        self.assertIsNotNone(target_proposal, proposals)

        proposal_metadata = target_proposal.get("metadata_json", {}) if isinstance(target_proposal.get("metadata_json", {}), dict) else {}
        pattern_ids = proposal_metadata.get("related_development_pattern_ids", []) if isinstance(proposal_metadata.get("related_development_pattern_ids", []), list) else []
        self.assertGreaterEqual(len(pattern_ids), 1)

        for strategy_id in active_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
