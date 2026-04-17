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


class Objective168SelfEvolutionOperatorActionabilityTest(unittest.TestCase):
    def _create_strategy(self, *, run_id: str, zone_suffix: str) -> int:
        status, payload = post_json(
            "/planning/strategies/generate",
            {
                "actor": "objective168-test",
                "source": "objective168-focused",
                "observed_conditions": [
                    {
                        "condition_type": "routine_zone_pattern",
                        "target_scope": f"front-left-obj168-{run_id}-{zone_suffix}",
                        "severity": 0.87,
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
                "actor": "objective168-test",
                "reason": "objective168 synthetic stall pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def test_objective168_self_evolution_action_is_operator_visible(self) -> None:
        run_id = uuid4().hex[:8]

        strategy_ids = [
            self._create_strategy(run_id=run_id, zone_suffix="a"),
            self._create_strategy(run_id=run_id, zone_suffix="b"),
        ]
        for strategy_id in strategy_ids:
            self._deactivate_strategy(strategy_id=strategy_id, run_id=run_id)

        status, refresh_payload = get_json(
            "/improvement/self-evolution/briefing",
            {
                "refresh": "true",
                "actor": "objective168-test",
                "source": "objective168-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "auto_experiment_limit": 3,
                "limit": 10,
            },
        )
        self.assertEqual(status, 200, refresh_payload)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        runtime_features = ui_state.get("runtime_features", []) if isinstance(ui_state, dict) else []
        self.assertIn("self_evolution_operator_actionability", runtime_features)

        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        self_evolution = (
            operator_reasoning.get("self_evolution", {})
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else {}
        )
        action = self_evolution.get("action", {}) if isinstance(self_evolution.get("action", {}), dict) else {}
        self.assertTrue(str(action.get("summary", "")).strip(), action)
        self.assertIn(str(action.get("method", "")).strip(), {"GET", "POST"})
        self.assertTrue(str(action.get("path", "")).startswith("/improvement/"), action)
        self.assertTrue(isinstance(action.get("payload_keys", []), list), action)

        self.assertEqual(
            str(self_evolution.get("action_summary", "")).strip(),
            str(action.get("summary", "")).strip(),
            self_evolution,
        )
        self.assertEqual(
            str(self_evolution.get("action_method", "")).strip(),
            str(action.get("method", "")).strip(),
            self_evolution,
        )
        self.assertEqual(
            str(self_evolution.get("action_path", "")).strip(),
            str(action.get("path", "")).strip(),
            self_evolution,
        )

        conversation_context = ui_state.get("conversation_context", {}) if isinstance(ui_state, dict) else {}
        self.assertEqual(
            str(conversation_context.get("self_evolution_action_summary", "")).strip(),
            str(action.get("summary", "")).strip(),
            ui_state,
        )
        self.assertEqual(
            str(conversation_context.get("self_evolution_action_method", "")).strip(),
            str(action.get("method", "")).strip(),
            ui_state,
        )
        self.assertEqual(
            str(conversation_context.get("self_evolution_action_path", "")).strip(),
            str(action.get("path", "")).strip(),
            ui_state,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
