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


class Objective62InquisitiveQuestionLoopTest(unittest.TestCase):
    def _seed_strategy(self, run_id: str, scope: str) -> int:
        status, generated = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective62-test",
                "source": "objective62-focused",
                "observed_conditions": [
                    {
                        "condition_type": "stale_scans",
                        "target_scope": scope,
                        "severity": 0.74,
                        "occurrence_count": 4,
                    }
                ],
                "min_severity": 0.6,
                "max_strategies": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        strategies = generated.get("strategies", []) if isinstance(generated, dict) else []
        self.assertGreaterEqual(len(strategies), 1)
        strategy_id = int(strategies[0].get("strategy_id", 0))
        self.assertGreater(strategy_id, 0)
        return strategy_id

    def _seed_plan(self, run_id: str, scope: str) -> int:
        status, plan = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective62-test",
                "source": "objective62-focused",
                "planning_horizon_minutes": 90,
                "goal_candidates": [
                    {
                        "goal_key": f"refresh:{scope}",
                        "title": "Refresh target scope",
                        "priority": "normal",
                        "goal_type": "workspace_refresh",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.58,
                        "urgency": 0.54,
                        "is_physical": False,
                        "metadata_json": {"scope": scope, "run_id": run_id},
                    },
                    {
                        "goal_key": f"confirm:{scope}",
                        "title": "Confirm target state",
                        "priority": "normal",
                        "goal_type": "target_confirmation",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.56,
                        "urgency": 0.55,
                        "is_physical": False,
                        "metadata_json": {"scope": scope, "run_id": run_id},
                    },
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.8,
                },
                "map_freshness_seconds": 200,
                "object_confidence": 0.8,
                "human_aware_state": {
                    "human_in_workspace": False,
                    "shared_workspace_active": False,
                },
                "operator_preferences": {},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, plan)
        plan_id = int(plan.get("plan_id", 0) if isinstance(plan, dict) else 0)
        self.assertGreater(plan_id, 0)
        return plan_id

    def _seed_low_confidence_friction(self, run_id: str) -> None:
        evaluation_ids: list[int] = []
        for index in range(3):
            status, evaluation = post_json(
                "/constraints/evaluate",
                {
                    "actor": "objective62-test",
                    "source": "objective62-focused",
                    "goal": {
                        "goal_id": f"obj62-goal-{run_id}-{index}",
                        "desired_state": "stable_execution",
                    },
                    "action_plan": {"action_type": "execute_action_plan", "is_physical": True},
                    "workspace_state": {
                        "human_in_workspace": False,
                        "human_near_target_zone": False,
                        "human_near_motion_path": False,
                        "shared_workspace_active": False,
                        "target_confidence": 0.62,
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
            evaluation_ids.append(int(evaluation.get("evaluation_id", 0)))

        for evaluation_id in evaluation_ids:
            status, outcome = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective62-test",
                    "evaluation_id": evaluation_id,
                    "result": "success",
                    "outcome_quality": 0.9,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

    def _seed_conflicting_domain_evidence(self, run_id: str, scope: str) -> None:
        for idx in range(2):
            status, event = post_json(
                "/gateway/intake/text",
                {
                    "text": f"objective62 operator request {run_id}-{idx}",
                    "parsed_intent": "operator_request",
                    "confidence": 0.93,
                    "metadata_json": {"run_id": run_id, "channel": "operator"},
                },
            )
            self.assertEqual(status, 200, event)

        status, memory = post_json(
            "/memory",
            {
                "memory_class": "external_signal",
                "content": f"External signal conflict for {run_id}",
                "summary": "external context diverges",
                "metadata_json": {"run_id": run_id, "kind": "external"},
            },
        )
        self.assertEqual(status, 200, memory)

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-{run_id}",
                "source_type": "camera",
                "session_id": run_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 20,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"run_id": run_id},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": scope,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

    def test_objective62_inquisitive_question_loop(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"front-left-obj62-{run_id}"

        strategy_id = self._seed_strategy(run_id=run_id, scope=scope)
        plan_id = self._seed_plan(run_id=run_id, scope=scope)
        self._seed_low_confidence_friction(run_id=run_id)
        self._seed_conflicting_domain_evidence(run_id=run_id, scope=scope)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective62-test",
                "source": "objective62-focused",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertGreaterEqual(len(questions), 2)

        by_trigger = {
            str(item.get("trigger_type", "")): item
            for item in questions
            if isinstance(item, dict)
        }
        self.assertIn("target_confidence_too_low", by_trigger)
        self.assertIn("conflicting_domain_evidence", by_trigger)

        low_conf_q = by_trigger["target_confidence_too_low"]
        self.assertTrue(bool(low_conf_q.get("why_answer_matters", "")))
        self.assertTrue(bool(low_conf_q.get("waiting_decision", "")))
        self.assertTrue(bool(low_conf_q.get("no_answer_behavior", "")))

        status, strategy_before = get_json(f"/planning/strategies/{strategy_id}")
        self.assertEqual(status, 200, strategy_before)
        before_weight = float((strategy_before.get("strategy", {}) if isinstance(strategy_before, dict) else {}).get("influence_weight", 0.0))

        status, answered = post_json(
            f"/inquiry/questions/{int(low_conf_q.get('question_id', 0))}/answer",
            {
                "actor": "operator",
                "selected_path_id": "shift_strategy_and_unblock",
                "answer_json": {"reason": "favor reobserve then continue"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        self.assertTrue(bool(answered.get("answered", False)))
        applied = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        self.assertTrue(bool(applied.get("strategy_shifted", False) or applied.get("plan_unblocked", False)))

        status, strategy_after = get_json(f"/planning/strategies/{strategy_id}")
        self.assertEqual(status, 200, strategy_after)
        after_weight = float((strategy_after.get("strategy", {}) if isinstance(strategy_after, dict) else {}).get("influence_weight", 0.0))
        self.assertGreaterEqual(after_weight, before_weight)

        status, plan_after = get_json(f"/planning/horizon/plans/{plan_id}")
        self.assertEqual(status, 200, plan_after)
        plan_status = str((plan_after.get("plan", {}) if isinstance(plan_after, dict) else {}).get("status", ""))
        self.assertIn(plan_status, {"active", "planned", "complete", "replanned", "needs_re_evaluation"})

        status, listed_open = get_json("/inquiry/questions?status=open&limit=20")
        self.assertEqual(status, 200, listed_open)
        open_rows = listed_open.get("questions", []) if isinstance(listed_open, dict) else []
        self.assertGreaterEqual(len(open_rows), 1)
        fallback_q = open_rows[0]
        status, fallback_detail = get_json(f"/inquiry/questions/{int(fallback_q.get('question_id', 0))}")
        self.assertEqual(status, 200, fallback_detail)
        fallback = fallback_detail.get("question", {}) if isinstance(fallback_detail, dict) else {}
        self.assertEqual(str(fallback.get("status", "")), "open")
        self.assertTrue(bool(fallback.get("safe_default_if_unanswered", "")))

        noisy_run = uuid4().hex[:8]
        status, _ = post_json(
            "/gateway/perception/mic/events",
            {
                "device_id": f"mic-{noisy_run}",
                "source_type": "microphone",
                "session_id": noisy_run,
                "is_remote": False,
                "transcript": "maybe maybe maybe",
                "confidence": 0.2,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 20,
                "transcript_confidence_floor": 0.45,
                "discard_low_confidence": True,
                "metadata_json": {"run_id": noisy_run},
            },
        )
        self.assertEqual(status, 200)

        status, noisy_generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective62-test",
                "source": "objective62-focused",
                "lookback_hours": 24,
                "max_questions": 5,
                "min_soft_friction_count": 4,
                "metadata_json": {"run_id": noisy_run},
            },
        )
        self.assertEqual(status, 200, noisy_generated)
        self.assertEqual(int(noisy_generated.get("generated", 0) or 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
