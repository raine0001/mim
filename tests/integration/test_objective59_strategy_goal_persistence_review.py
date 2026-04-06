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


class Objective59StrategyGoalPersistenceReviewTest(unittest.TestCase):
    def test_objective59_strategy_goal_persistence_review(self) -> None:
        run_id = uuid4().hex[:8]

        status, built = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective59-test",
                "source": "objective59-focused",
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 3,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, built)
        self.assertGreater(int(built.get("generated", 0) or 0), 0)
        goals = built.get("goals", []) if isinstance(built, dict) else []
        self.assertTrue(goals and isinstance(goals[0], dict))
        goal_id = int(goals[0].get("strategy_goal_id", 0) or 0)
        self.assertGreater(goal_id, 0)

        status, recompute = post_json(
            "/strategy/persistence/goals/recompute",
            {
                "actor": "objective59-test",
                "source": "objective59-focused",
                "lookback_hours": 168,
                "min_support_count": 1,
                "min_persistence_confidence": 0.0,
                "limit": 500,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, recompute)
        recomputed_goals = recompute.get("goals", []) if isinstance(recompute, dict) else []
        matched = [item for item in recomputed_goals if isinstance(item, dict) and int(item.get("strategy_goal_id", 0) or 0) == goal_id]
        self.assertTrue(matched)
        self.assertEqual(str(matched[0].get("persistence_state", "")), "persistent")
        self.assertIn(str(matched[0].get("review_status", "")), {"needs_review", "approved", "deferred", "archived", "unreviewed"})

        status, reviewed = post_json(
            f"/strategy/goals/{goal_id}/review",
            {
                "actor": "objective59-test-operator",
                "decision": "carry_forward",
                "reason": "keep across sessions",
                "evidence_json": {"run_id": run_id, "signal": "repeat_strategy_type"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, reviewed)
        reviewed_goal = reviewed.get("goal", {}) if isinstance(reviewed, dict) else {}
        review = reviewed.get("review", {}) if isinstance(reviewed, dict) else {}
        self.assertEqual(int(reviewed_goal.get("strategy_goal_id", 0) or 0), goal_id)
        self.assertEqual(str(reviewed_goal.get("persistence_state", "")), "persistent")
        self.assertEqual(str(reviewed_goal.get("review_status", "")), "approved")
        self.assertEqual(str(review.get("decision", "")), "carry_forward")

        status, listed = get_json(
            "/strategy/persistence/goals",
            {
                "persistence_state": "persistent",
                "review_status": "approved",
                "limit": 200,
            },
        )
        self.assertEqual(status, 200, listed)
        listed_goals = listed.get("goals", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("strategy_goal_id", 0) or 0) == goal_id for item in listed_goals if isinstance(item, dict)))

        status, reviews = get_json(f"/strategy/goals/{goal_id}/reviews", {"limit": 50})
        self.assertEqual(status, 200, reviews)
        rows = reviews.get("reviews", []) if isinstance(reviews, dict) else []
        self.assertTrue(any(str(item.get("decision", "")) == "carry_forward" for item in rows if isinstance(item, dict)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
