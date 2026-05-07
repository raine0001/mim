import json
import os
import unittest
import urllib.error
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


class Objective51PolicyExperimentSandboxTest(unittest.TestCase):
    def _create_soft_constraint_friction(self, run_id: str, count: int) -> None:
        for index in range(count):
            status, evaluation = post_json(
                "/constraints/evaluate",
                {
                    "actor": "objective51-test",
                    "source": "objective51-focused",
                    "goal": {
                        "goal_key": f"goal-{run_id}-{index}",
                        "goal_type": "workspace_refresh",
                    },
                    "action_plan": {
                        "action_type": "observe",
                        "is_physical": False,
                    },
                    "workspace_state": {
                        "target_confidence": 0.93,
                        "map_freshness_seconds": 120,
                        "human_near_motion_path": False,
                        "human_near_target_zone": False,
                        "human_in_workspace": False,
                        "shared_workspace_active": False,
                    },
                    "system_state": {
                        "throttle_blocked": True,
                        "integrity_risk": False,
                    },
                    "policy_state": {
                        "min_target_confidence": 0.7,
                        "map_freshness_limit_seconds": 900,
                        "unlawful_action": False,
                    },
                    "metadata_json": {
                        "run_id": run_id,
                        "phase": "objective51-friction",
                    },
                },
            )
            self.assertEqual(status, 200, evaluation)
            eval_id = int(evaluation.get("evaluation_id", 0))
            self.assertGreater(eval_id, 0)

            status, recorded = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective51-test",
                    "evaluation_id": eval_id,
                    "result": "succeeded",
                    "outcome_quality": 0.9,
                    "metadata_json": {
                        "run_id": run_id,
                        "phase": "objective51-outcome",
                    },
                },
            )
            self.assertEqual(status, 200, recorded)

    def test_objective51_policy_experiment_recommends_next_action(self) -> None:
        run_id = uuid4().hex[:8]
        self._create_soft_constraint_friction(run_id=run_id, count=3)

        status, generated = post_json(
            "/improvement/proposals/generate",
            {
                "actor": "objective51-test",
                "source": "objective51-focused",
                "lookback_hours": 24,
                "min_occurrence_count": 2,
                "max_proposals": 10,
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, generated)

        proposals = generated.get("proposals", []) if isinstance(generated.get("proposals", []), list) else []
        soft = next(
            (
                item
                for item in proposals
                if isinstance(item, dict) and item.get("proposal_type") == "soft_constraint_weight_adjustment"
            ),
            None,
        )
        self.assertIsNotNone(soft, proposals)
        proposal_id = int((soft or {}).get("proposal_id", 0))
        self.assertGreater(proposal_id, 0)

        status, ran = post_json(
            "/improvement/experiments/run",
            {
                "actor": "objective51-test",
                "source": "objective51-focused",
                "proposal_id": proposal_id,
                "experiment_type": "soft_constraint_sandbox",
                "lookback_hours": 24,
                "sandbox_mode": "shadow_evaluation",
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, ran)

        experiment = ran.get("experiment", {}) if isinstance(ran, dict) else {}
        experiment_id = int(experiment.get("experiment_id", 0))
        self.assertGreater(experiment_id, 0)
        self.assertEqual(str(experiment.get("status", "")), "completed")
        self.assertIn(str(experiment.get("recommendation", "")), {"promote", "revise", "reject"})

        baseline = experiment.get("baseline_metrics", {}) if isinstance(experiment.get("baseline_metrics", {}), dict) else {}
        trial = experiment.get("experimental_metrics", {}) if isinstance(experiment.get("experimental_metrics", {}), dict) else {}
        comparison = experiment.get("comparison", {}) if isinstance(experiment.get("comparison", {}), dict) else {}

        self.assertIn("friction_events", baseline)
        self.assertIn("friction_events", trial)
        self.assertIn("improvement_score", comparison)

        status, listed = get_json("/improvement/experiments?limit=20")
        self.assertEqual(status, 200, listed)
        rows = listed.get("experiments", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("experiment_id", 0)) == experiment_id for item in rows if isinstance(item, dict)))

        status, detail = get_json(f"/improvement/experiments/{experiment_id}")
        self.assertEqual(status, 200, detail)
        detailed = detail.get("experiment", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detailed.get("experiment_id", 0)), experiment_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
