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


class Objective57GoalStrategyEngineTest(unittest.TestCase):
    def _seed_workspace_signal(self, *, run_id: str) -> None:
        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective57 workspace check {run_id}",
                "parsed_intent": "observe_workspace",
                "requested_goal": "collect workspace state",
                "metadata_json": {"run_id": run_id, "source": "objective57"},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int((event.get("execution", {}) if isinstance(event.get("execution", {}), dict) else {}).get("execution_id", 0))
        self.assertGreater(execution_id, 0)

        for step in ["accepted", "running", "succeeded"]:
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": step,
                    "reason": step,
                    "actor": "tod",
                    "feedback_json": {
                        "observations": [
                            {"label": "toolbox", "zone": f"bench-{run_id}", "confidence": 0.91},
                            {"label": "screwdriver", "zone": f"bench-{run_id}", "confidence": 0.87},
                        ],
                    } if step == "succeeded" else {},
                },
            )
            self.assertEqual(status, 200, payload)

    def _seed_communication_signal(self, *, run_id: str) -> None:
        status, text_event = post_json(
            "/gateway/intake/text",
            {
                "text": f"mim assist: strategic status summary {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "summarize workspace",
                "metadata_json": {"channel": "mim_assist", "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, text_event)

        status, voice_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": f"what should we prioritize next {run_id}",
                "parsed_intent": "priority_query",
                "confidence": 0.85,
                "requested_goal": "choose strategic objective",
            },
        )
        self.assertEqual(status, 200, voice_event)

        status, spoken = post_json(
            "/gateway/voice/output",
            {
                "message": f"Strategic synthesis requested for run {run_id}",
                "voice_profile": "status",
                "channel": "mim_assist",
            },
        )
        self.assertEqual(status, 200, spoken)

    def _seed_external_signal(self, *, run_id: str) -> None:
        status, memory = post_json(
            "/memory",
            {
                "memory_class": "external_information",
                "content": f"Calendar indicates upcoming collaboration window for run {run_id}",
                "summary": "External collaboration timing signal",
                "metadata_json": {"provider": "calendar", "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _seed_development_signal(self, *, run_id: str) -> None:
        strategy_ids: list[int] = []
        for suffix in ["a", "b"]:
            status, payload = post_json(
                "/planning/strategies/generate",
                {
                    "actor": "objective57-test",
                    "source": "objective57-focused",
                    "observed_conditions": [
                        {
                            "condition_type": "routine_zone_pattern",
                            "target_scope": f"front-left-obj57-{run_id}-{suffix}",
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
            strategy_ids.append(strategy_id)

        for strategy_id in strategy_ids:
            status, payload = post_json(
                f"/planning/strategies/{strategy_id}/deactivate",
                {
                    "actor": "objective57-test",
                    "reason": "objective57 development pattern signal",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, payload)

        status, patterns = get_json(
            "/memory/development-patterns",
            {
                "refresh": "true",
                "lookback_hours": 168,
                "min_evidence_count": 2,
                "limit": 50,
            },
        )
        self.assertEqual(status, 200, patterns)

    def test_objective57_goal_strategy_engine(self) -> None:
        run_id = uuid4().hex[:8]

        self._seed_workspace_signal(run_id=run_id)
        self._seed_communication_signal(run_id=run_id)
        self._seed_external_signal(run_id=run_id)
        self._seed_development_signal(run_id=run_id)

        status, refreshed = post_json(
            "/improvement/backlog/refresh",
            {
                "actor": "objective57-test",
                "source": "objective57-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_items": 30,
                "auto_experiment_limit": 1,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, refreshed)

        status, built = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective57-test",
                "source": "objective57-focused",
                "lookback_hours": 168,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.4,
                "min_domains_required": 3,
                "min_cross_domain_links": 1,
                "generate_horizon_plans": True,
                "generate_improvement_proposals": True,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, built)

        generated = int(built.get("generated", 0) or 0)
        goals = built.get("goals", []) if isinstance(built.get("goals", []), list) else []
        synthesis = built.get("synthesis", {}) if isinstance(built.get("synthesis", {}), dict) else {}
        context = built.get("origin_context", {}) if isinstance(built.get("origin_context", {}), dict) else {}

        self.assertGreaterEqual(generated, 1, built)
        self.assertGreaterEqual(len(goals), 1, built)
        self.assertGreaterEqual(float(synthesis.get("context_confidence", 0.0) or 0.0), 0.4)
        self.assertGreater(int(context.get("context_id", 0) or 0), 0)

        ranked_pairs = [
            (float(item.get("priority_score", 0.0) or 0.0), str(item.get("strategy_type", "")))
            for item in goals
            if isinstance(item, dict)
        ]
        self.assertEqual(
            ranked_pairs,
            sorted(ranked_pairs, key=lambda value: (-value[0], value[1])),
            goals,
        )

        top = goals[0] if isinstance(goals[0], dict) else {}
        strategy_goal_id = int(top.get("strategy_goal_id", 0) or 0)
        self.assertGreater(strategy_goal_id, 0)
        self.assertGreaterEqual(len(top.get("contributing_domains", [])), 3)
        self.assertTrue(str(top.get("reasoning_summary", "")).strip())

        reasoning = top.get("reasoning", {}) if isinstance(top.get("reasoning", {}), dict) else {}
        self.assertGreaterEqual(len(reasoning.get("cross_domain_links", [])), 1)

        influenced = [
            item
            for item in goals
            if isinstance(item, dict)
            and isinstance(item.get("linked_horizon_plan_ids", []), list)
            and len(item.get("linked_horizon_plan_ids", [])) > 0
        ]
        self.assertGreaterEqual(len(influenced), 1, goals)

        status, listed = get_json(
            "/strategy/goals",
            {
                "status": "proposed",
                "limit": 50,
            },
        )
        self.assertEqual(status, 200, listed)
        listed_goals = listed.get("goals", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("strategy_goal_id", 0) or 0) == strategy_goal_id for item in listed_goals if isinstance(item, dict)))

        status, detail = get_json(f"/strategy/goals/{strategy_goal_id}")
        self.assertEqual(status, 200, detail)
        detail_goal = detail.get("goal", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_goal.get("strategy_goal_id", 0) or 0), strategy_goal_id)
        self.assertTrue(str(detail_goal.get("evidence_summary", "")).strip())
        self.assertTrue(str(detail_goal.get("success_criteria", "")).strip())

        status, low_quality = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective57-test",
                "source": "objective57-low-quality",
                "lookback_hours": 1,
                "max_items_per_domain": 5,
                "max_goals": 3,
                "min_context_confidence": 0.95,
                "min_domains_required": 6,
                "min_cross_domain_links": 10,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "quality": "low"},
            },
        )
        self.assertEqual(status, 200, low_quality)
        self.assertEqual(int(low_quality.get("generated", 0) or 0), 0, low_quality)
        low_synthesis = low_quality.get("synthesis", {}) if isinstance(low_quality.get("synthesis", {}), dict) else {}
        self.assertGreaterEqual(len(low_synthesis.get("gating_reasons", [])), 1, low_synthesis)


if __name__ == "__main__":
    unittest.main(verbosity=2)