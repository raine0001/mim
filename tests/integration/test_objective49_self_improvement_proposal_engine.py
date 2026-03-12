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


class Objective49SelfImprovementProposalEngineTest(unittest.TestCase):
    def _create_soft_constraint_friction(self, run_id: str, count: int) -> None:
        for index in range(count):
            status, evaluation = post_json(
                "/constraints/evaluate",
                {
                    "actor": "objective49-test",
                    "source": "objective49-focused",
                    "goal": {
                        "goal_key": f"goal-{run_id}-{index}",
                        "goal_type": "workspace_refresh",
                    },
                    "action_plan": {
                        "action_type": "observe",
                        "is_physical": False,
                    },
                    "workspace_state": {
                        "target_confidence": 0.92,
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
                        "phase": "soft-constraint-friction",
                    },
                },
            )
            self.assertEqual(status, 200, evaluation)
            eval_id = int(evaluation.get("evaluation_id", 0))
            self.assertGreater(eval_id, 0)

            status, recorded = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective49-test",
                    "evaluation_id": eval_id,
                    "result": "succeeded",
                    "outcome_quality": 0.93,
                    "metadata_json": {
                        "run_id": run_id,
                        "phase": "soft-constraint-success",
                    },
                },
            )
            self.assertEqual(status, 200, recorded)
            self.assertTrue(bool(recorded.get("updated", False)))

    def _create_manual_override_pattern(self, run_id: str, count: int) -> None:
        for index in range(count):
            scope = f"front-left-obj49-override-{run_id}-{index}"
            status, generated = post_json(
                "/planning/strategies/generate",
                {
                    "actor": "objective49-test",
                    "source": "objective49-focused",
                    "observed_conditions": [
                        {
                            "condition_type": "stale_scans",
                            "target_scope": scope,
                            "severity": 0.72,
                            "occurrence_count": 5,
                        }
                    ],
                    "min_severity": 0.6,
                    "max_strategies": 3,
                    "metadata_json": {
                        "run_id": run_id,
                        "phase": "manual-override-pattern",
                    },
                },
            )
            self.assertEqual(status, 200, generated)
            strategy = generated.get("strategies", [])[0] if isinstance(generated.get("strategies", []), list) and generated.get("strategies", []) else {}
            strategy_id = int(strategy.get("strategy_id", 0))
            self.assertGreater(strategy_id, 0)

            status, resolved = post_json(
                f"/planning/strategies/{strategy_id}/resolve",
                {
                    "actor": "operator",
                    "reason": "manual_override_tuning",
                    "status": "stable",
                    "metadata_json": {
                        "run_id": run_id,
                        "phase": "manual-override-pattern",
                    },
                },
            )
            self.assertEqual(status, 200, resolved)

    def test_objective49_rule_based_improvement_proposals(self) -> None:
        run_id = uuid4().hex[:8]

        self._create_soft_constraint_friction(run_id=run_id, count=3)
        self._create_manual_override_pattern(run_id=run_id, count=2)

        status, generated = post_json(
            "/improvement/proposals/generate",
            {
                "actor": "objective49-test",
                "source": "objective49-focused",
                "lookback_hours": 24,
                "min_occurrence_count": 2,
                "max_proposals": 10,
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, generated)
        generated_rows = generated.get("proposals", []) if isinstance(generated.get("proposals", []), list) else []

        status, listed = get_json("/improvement/proposals?limit=100")
        self.assertEqual(status, 200, listed)
        listed_rows = listed.get("proposals", []) if isinstance(listed, dict) and isinstance(listed.get("proposals", []), list) else []

        proposals = [*generated_rows, *listed_rows]
        proposal_types = {str(item.get("proposal_type", "")) for item in proposals if isinstance(item, dict)}
        self.assertIn("soft_constraint_weight_adjustment", proposal_types)
        self.assertIn("operator_preference_suggestion", proposal_types)

        soft = next(
            (
                item
                for item in proposals
                if isinstance(item, dict)
                and item.get("proposal_type") == "soft_constraint_weight_adjustment"
                and item.get("status") == "proposed"
            ),
            None,
        )
        if soft is None:
            soft = next((item for item in proposals if isinstance(item, dict) and item.get("proposal_type") == "soft_constraint_weight_adjustment"), None)
        self.assertIsNotNone(soft, proposals)
        self.assertTrue(bool((soft or {}).get("trigger_pattern", "")))
        self.assertTrue(bool((soft or {}).get("evidence_summary", "")))
        self.assertTrue(bool((soft or {}).get("suggested_change", "")))
        self.assertTrue(bool((soft or {}).get("risk_summary", "")))
        self.assertTrue(bool((soft or {}).get("test_recommendation", "")))

        soft_id = int((soft or {}).get("proposal_id", 0))
        self.assertGreater(soft_id, 0)

        status, detail = get_json(f"/improvement/proposals/{soft_id}")
        self.assertEqual(status, 200, detail)
        proposal_detail = detail.get("proposal", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(proposal_detail.get("proposal_id", 0)), soft_id)
        self.assertIn("evidence", proposal_detail)

        status, accepted = post_json(
            f"/improvement/proposals/{soft_id}/accept",
            {
                "actor": "operator",
                "reason": "accept_for_review",
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(str(accepted.get("proposal", {}).get("status", "")), "accepted")
        artifact = accepted.get("artifact", {}) if isinstance(accepted.get("artifact", {}), dict) else {}
        self.assertIn(str(artifact.get("artifact_type", "")), {"policy_change_candidate", "test_candidate", "gated_workflow_item"})
        self.assertEqual(str(artifact.get("status", "")), "pending_review")

        reject_candidate = next(
            (
                item
                for item in proposals
                if isinstance(item, dict)
                and int(item.get("proposal_id", 0)) != soft_id
                and item.get("status") == "proposed"
            ),
            None,
        )
        self.assertIsNotNone(reject_candidate, proposals)
        reject_id = int((reject_candidate or {}).get("proposal_id", 0))

        status, rejected = post_json(
            f"/improvement/proposals/{reject_id}/reject",
            {
                "actor": "operator",
                "reason": "not_now",
                "metadata_json": {
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, rejected)
        self.assertEqual(str(rejected.get("proposal", {}).get("status", "")), "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
