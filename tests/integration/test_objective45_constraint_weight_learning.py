import json
import os
import unittest
import urllib.error
import urllib.request
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


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective45ConstraintWeightLearningTest(unittest.TestCase):
    def test_objective45_records_outcomes_and_generates_soft_adjustment_proposals(self) -> None:
        run_id = uuid4().hex[:8]
        evaluation_ids: list[int] = []

        for index in range(3):
            status, evaluation = post_json(
                "/constraints/evaluate",
                {
                    "actor": "objective45-test",
                    "source": "objective45-focused",
                    "goal": {
                        "goal_id": f"obj45-goal-{run_id}-{index}",
                        "desired_state": "stable_execution",
                    },
                    "action_plan": {"action_type": "execute_action_plan", "is_physical": True},
                    "workspace_state": {
                        "human_in_workspace": False,
                        "human_near_target_zone": False,
                        "human_near_motion_path": False,
                        "shared_workspace_active": False,
                        "target_confidence": 0.75,
                        "map_freshness_seconds": 120,
                    },
                    "system_state": {"throttle_blocked": False, "integrity_risk": False},
                    "policy_state": {
                        "min_target_confidence": 0.85,
                        "map_freshness_limit_seconds": 900,
                        "unlawful_action": False,
                    },
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, evaluation)
            self.assertEqual(evaluation.get("decision"), "requires_replan")
            self.assertIn("evaluation_id", evaluation)
            evaluation_ids.append(int(evaluation.get("evaluation_id")))

        for evaluation_id in evaluation_ids:
            status, outcome = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective45-test",
                    "evaluation_id": evaluation_id,
                    "result": "success",
                    "outcome_quality": 0.92,
                    "metadata_json": {"run_id": run_id, "note": "succeeded despite soft warning"},
                },
            )
            self.assertEqual(status, 200, outcome)
            self.assertTrue(outcome.get("updated"), outcome)
            self.assertEqual(outcome.get("outcome_result"), "success")

        status, stats = get_json("/constraints/learning/stats?constraint_key=target_confidence_threshold&limit=50")
        self.assertEqual(status, 200, stats)
        rows = stats.get("stats", []) if isinstance(stats, dict) else []
        self.assertGreaterEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0].get("observations", 0), 3)
        self.assertGreaterEqual(rows[0].get("warning_observations", 0), 1)

        status, generated = post_json(
            "/constraints/learning/proposals/generate",
            {
                "actor": "objective45-test",
                "source": "objective45-focused",
                "min_samples": 3,
                "success_rate_threshold": 0.7,
                "max_proposals": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        generated_count = int(generated.get("generated", 0) or 0)
        proposals = generated.get("proposals", [])

        if generated_count >= 1:
            target = next((item for item in proposals if item.get("constraint_key") == "target_confidence_threshold"), None)
            self.assertIsNotNone(target, proposals)
            self.assertEqual(target.get("status"), "proposed")
            self.assertFalse(bool(target.get("hard_constraint")))
        else:
            self.assertEqual(generated_count, 0)

        status, listed = get_json("/constraints/learning/proposals?status=proposed&limit=20")
        self.assertEqual(status, 200, listed)
        all_rows = listed.get("proposals", []) if isinstance(listed, dict) else []
        self.assertGreaterEqual(len(all_rows), 1)
        listed_target = next((item for item in all_rows if item.get("constraint_key") == "target_confidence_threshold"), None)
        self.assertIsNotNone(listed_target, all_rows)
        self.assertEqual(listed_target.get("status"), "proposed")
        self.assertFalse(bool(listed_target.get("hard_constraint")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
