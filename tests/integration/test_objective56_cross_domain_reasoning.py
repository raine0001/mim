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


class Objective56CrossDomainReasoningTest(unittest.TestCase):
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
                "text": f"objective56 workspace check {run_id}",
                "parsed_intent": "observe_workspace",
                "requested_goal": "collect workspace state",
                "metadata_json": {"run_id": run_id, "source": "objective56"},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int((event.get("execution", {}) if isinstance(event.get("execution", {}), dict) else {}).get("execution_id", 0))
        self.assertGreater(execution_id, 0)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {"status": "accepted", "reason": "accepted", "actor": "tod", "feedback_json": {}},
        )
        self.assertEqual(status, 200, accepted)

        status, running = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {"status": "running", "reason": "running", "actor": "tod", "feedback_json": {}},
        )
        self.assertEqual(status, 200, running)

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {"label": "toolbox", "zone": f"bench-{run_id}", "confidence": 0.91},
                        {"label": "screwdriver", "zone": f"bench-{run_id}", "confidence": 0.87},
                    ],
                },
            },
        )
        self.assertEqual(status, 200, succeeded)

    def _seed_communication_signal(self, *, run_id: str) -> None:
        status, text_event = post_json(
            "/gateway/intake/text",
            {
                "text": f"mim assist: summarize current workspace status {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "summarize workspace",
                "metadata_json": {"channel": "mim_assist", "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, text_event)

        status, voice_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": f"what changed in the environment {run_id}",
                "parsed_intent": "workspace_change_query",
                "confidence": 0.83,
                "requested_goal": "reason across channels",
            },
        )
        self.assertEqual(status, 200, voice_event)

        status, spoken = post_json(
            "/gateway/voice/output",
            {
                "message": f"Cross-domain context requested for run {run_id}",
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
                "content": f"Weather API indicates reduced daylight for run {run_id}",
                "summary": "External daylight reduction signal",
                "metadata_json": {"provider": "weather_api", "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _seed_development_signal(self, *, run_id: str) -> None:
        strategy_ids: list[int] = []
        for suffix in ["a", "b"]:
            status, payload = post_json(
                "/planning/strategies/generate",
                {
                    "actor": "objective56-test",
                    "source": "objective56-focused",
                    "observed_conditions": [
                        {
                            "condition_type": "routine_zone_pattern",
                            "target_scope": f"front-left-obj56-{run_id}-{suffix}",
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
                    "actor": "objective56-test",
                    "reason": "objective56 development pattern signal",
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

    def test_objective56_cross_domain_context(self) -> None:
        run_id = uuid4().hex[:8]

        self._seed_workspace_signal(run_id=run_id)
        self._seed_communication_signal(run_id=run_id)
        self._seed_external_signal(run_id=run_id)
        self._seed_development_signal(run_id=run_id)

        status, refreshed = post_json(
            "/improvement/backlog/refresh",
            {
                "actor": "objective56-test",
                "source": "objective56-focused",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_items": 30,
                "auto_experiment_limit": 1,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, refreshed)

        status, built = post_json(
            "/reasoning/context/build",
            {
                "actor": "objective56-test",
                "source": "objective56-focused",
                "lookback_hours": 168,
                "max_items_per_domain": 50,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, built)
        context = built.get("context", {}) if isinstance(built, dict) else {}

        context_id = int(context.get("context_id", 0) or 0)
        self.assertGreater(context_id, 0)

        workspace_state = context.get("workspace_state", {}) if isinstance(context.get("workspace_state", {}), dict) else {}
        communication_state = context.get("communication_state", {}) if isinstance(context.get("communication_state", {}), dict) else {}
        external_information = context.get("external_information", {}) if isinstance(context.get("external_information", {}), dict) else {}
        development_state = context.get("development_state", {}) if isinstance(context.get("development_state", {}), dict) else {}
        self_improvement_state = context.get("self_improvement_state", {}) if isinstance(context.get("self_improvement_state", {}), dict) else {}
        reasoning = context.get("reasoning", {}) if isinstance(context.get("reasoning", {}), dict) else {}

        self.assertGreaterEqual(int(workspace_state.get("observation_count", 0) or 0), 1)
        self.assertGreaterEqual(int(communication_state.get("input_event_count", 0) or 0), 1)
        self.assertGreaterEqual(int(external_information.get("external_item_count", 0) or 0), 1)
        self.assertGreaterEqual(int(development_state.get("pattern_count", 0) or 0), 1)
        self.assertGreaterEqual(int(self_improvement_state.get("backlog_item_count", 0) or 0), 1)
        self.assertTrue(str(context.get("reasoning_summary", "")).strip())
        self.assertTrue(isinstance(reasoning.get("cross_domain_links", []), list))
        self.assertGreaterEqual(float(context.get("confidence", 0.0) or 0.0), 0.6)

        status, listed = get_json("/reasoning/context", {"limit": 20})
        self.assertEqual(status, 200, listed)
        rows = listed.get("contexts", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("context_id", 0) or 0) == context_id for item in rows if isinstance(item, dict)))

        status, detail = get_json(f"/reasoning/context/{context_id}")
        self.assertEqual(status, 200, detail)
        detail_context = detail.get("context", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_context.get("context_id", 0) or 0), context_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
