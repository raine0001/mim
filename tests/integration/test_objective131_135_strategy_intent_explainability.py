import asyncio
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
SCOPE_PREFIX = "objective131135-"


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
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else {})


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else {})


def cleanup_objective131_135_rows() -> None:
    asyncio.run(_cleanup_objective131_135_rows_async())


async def _cleanup_objective131_135_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        scope_like = f"{SCOPE_PREFIX}%"

        async def _table_exists(table_name: str) -> bool:
            return bool(
                await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL",
                    f"public.{table_name}",
                )
            )

        if await _table_exists("execution_strategy_plans"):
            await conn.execute(
                "DELETE FROM execution_strategy_plans WHERE managed_scope LIKE $1 OR trace_id LIKE $2",
                scope_like,
                f"trace-{SCOPE_PREFIX}%",
            )
        if await _table_exists("execution_stability_profiles"):
            await conn.execute(
                "DELETE FROM execution_stability_profiles WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("execution_task_orchestrations"):
            await conn.execute(
                "DELETE FROM execution_task_orchestrations WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("execution_intents"):
            await conn.execute(
                "DELETE FROM execution_intents WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("execution_trace_events") and await _table_exists("execution_traces"):
            await conn.execute(
                "DELETE FROM execution_trace_events WHERE trace_id IN (SELECT trace_id FROM execution_traces WHERE managed_scope LIKE $1)",
                scope_like,
            )
        if await _table_exists("execution_traces"):
            await conn.execute(
                "DELETE FROM execution_traces WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("capability_executions"):
            await conn.execute(
                "DELETE FROM capability_executions WHERE managed_scope LIKE $1",
                scope_like,
            )
    finally:
        await conn.close()


class Objective131135StrategyIntentExplainabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objectives 131-135",
            base_url=BASE_URL,
            require_ui_state=True,
            require_execution_control_plane=True,
        )
        cleanup_objective131_135_rows()

    def setUp(self) -> None:
        cleanup_objective131_135_rows()
        for capability_name, requires_confirmation in [
            ("workspace_check", False),
            ("capture_frame", False),
        ]:
            status, payload = post_json(
                "/gateway/capabilities",
                {
                    "capability_name": capability_name,
                    "category": "diagnostic",
                    "description": f"{capability_name} capability",
                    "requires_confirmation": requires_confirmation,
                    "enabled": True,
                },
            )
            self.assertEqual(status, 200, payload)

    def tearDown(self) -> None:
        cleanup_objective131_135_rows()

    def test_strategy_plan_is_created_from_intent_understanding_and_exposed_in_trace(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan the workspace and capture a photo of the object in {scope}",
                "parsed_intent": "execute_capability",
                "confidence": 0.97,
                "requested_goal": f"inspect object in {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                },
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {})
        resolution_metadata = resolution.get("metadata_json", {}) if isinstance(resolution.get("metadata_json", {}), dict) else {}
        intent_understanding = resolution_metadata.get("intent_understanding", {}) if isinstance(resolution_metadata.get("intent_understanding", {}), dict) else {}
        self.assertEqual(intent_understanding.get("canonical_intent"), "inspect_object", resolution)
        self.assertIn("robot", intent_understanding.get("suggested_domains", []), intent_understanding)
        proposed_actions = resolution.get("proposed_actions", [])
        self.assertGreaterEqual(len(proposed_actions), 3, proposed_actions)

        execution = payload.get("execution", {})
        trace_id = str(execution.get("trace_id") or "")
        self.assertTrue(trace_id.startswith("trace-"), execution)
        strategy_plan = execution.get("strategy_plan", {}) if isinstance(execution.get("strategy_plan", {}), dict) else {}
        self.assertTrue(strategy_plan, execution)
        self.assertEqual(strategy_plan.get("managed_scope"), scope, strategy_plan)
        self.assertEqual(strategy_plan.get("canonical_intent"), "inspect_object", strategy_plan)
        self.assertIn("robot", strategy_plan.get("coordination_domains", []), strategy_plan)
        confidence_assessment = strategy_plan.get("confidence_assessment", {}) if isinstance(strategy_plan.get("confidence_assessment", {}), dict) else {}
        environment_awareness = strategy_plan.get("environment_awareness", {}) if isinstance(strategy_plan.get("environment_awareness", {}), dict) else {}
        coordination_state = strategy_plan.get("coordination_state", {}) if isinstance(strategy_plan.get("coordination_state", {}), dict) else {}
        safety_envelope = strategy_plan.get("safety_envelope", {}) if isinstance(strategy_plan.get("safety_envelope", {}), dict) else {}
        self.assertTrue(str(confidence_assessment.get("tier") or "").strip(), confidence_assessment)
        self.assertEqual(environment_awareness.get("managed_scope"), scope, environment_awareness)
        self.assertEqual(coordination_state.get("mode"), "multi_agent", coordination_state)
        self.assertIn("robot", coordination_state.get("domains", []), coordination_state)
        self.assertIn("operator_review_required", safety_envelope, safety_envelope)

        status, trace_payload = get_json(f"/execution/traces/{trace_id}")
        self.assertEqual(status, 200, trace_payload)
        trace = trace_payload.get("trace", {}) if isinstance(trace_payload, dict) else {}
        trace_strategy_plan = trace.get("strategy_plan", {}) if isinstance(trace.get("strategy_plan", {}), dict) else {}
        self.assertEqual(trace_strategy_plan.get("strategy_plan_id"), strategy_plan.get("strategy_plan_id"), trace)
        self.assertTrue(bool(trace_strategy_plan.get("continuation_state", {}).get("recommended_next_step")), trace_strategy_plan)

        status, plans_payload = get_json("/execution/strategy-plans", {"trace_id": trace_id})
        self.assertEqual(status, 200, plans_payload)
        plans = plans_payload.get("strategy_plans", []) if isinstance(plans_payload, dict) else []
        self.assertEqual(len(plans), 1, plans_payload)
        self.assertEqual(plans[0].get("trace_id"), trace_id, plans)

    def test_strategy_plan_can_advance_and_ui_exposes_trust_explainability(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan and capture the object for explainability in {scope}",
                "parsed_intent": "execute_capability",
                "confidence": 0.96,
                "requested_goal": f"inspect object in {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        strategy_plan = payload.get("execution", {}).get("strategy_plan", {}) if isinstance(payload.get("execution", {}).get("strategy_plan", {}), dict) else {}
        plan_id = int(strategy_plan.get("strategy_plan_id", 0) or 0)
        self.assertGreater(plan_id, 0, payload)
        first_step = strategy_plan.get("primary_plan", [])[0]
        first_step_key = str(first_step.get("step_key") or "")
        self.assertTrue(first_step_key, strategy_plan)

        status, advanced_payload = post_json(
            f"/execution/strategy-plans/{plan_id}/advance",
            {
                "actor": "objective134-test",
                "source": "objective134",
                "completed_step_key": first_step_key,
                "outcome": "completed",
                "observed_confidence": 0.91,
                "metadata_json": {"managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, advanced_payload)
        advanced_plan = advanced_payload.get("strategy_plan", {}) if isinstance(advanced_payload.get("strategy_plan", {}), dict) else {}
        continuation = advanced_plan.get("continuation_state", {}) if isinstance(advanced_plan.get("continuation_state", {}), dict) else {}
        confidence_assessment = advanced_plan.get("confidence_assessment", {}) if isinstance(advanced_plan.get("confidence_assessment", {}), dict) else {}
        refinement_state = advanced_plan.get("refinement_state", {}) if isinstance(advanced_plan.get("refinement_state", {}), dict) else {}
        context_persistence = advanced_plan.get("context_persistence", {}) if isinstance(advanced_plan.get("context_persistence", {}), dict) else {}
        self.assertIn(first_step_key, continuation.get("completed_steps", []), continuation)
        self.assertTrue(bool(continuation.get("recommended_next_step")), continuation)
        self.assertGreaterEqual(float(confidence_assessment.get("score", 0.0) or 0.0), 0.0, confidence_assessment)
        self.assertIn("needs_refinement", refinement_state, refinement_state)
        self.assertEqual(context_persistence.get("managed_scope"), scope, context_persistence)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state.get("operator_reasoning", {}), dict) else {}
        ui_strategy = operator_reasoning.get("strategy_plan", {}) if isinstance(operator_reasoning.get("strategy_plan", {}), dict) else {}
        trust = operator_reasoning.get("trust_explainability", {}) if isinstance(operator_reasoning.get("trust_explainability", {}), dict) else {}
        self.assertEqual(ui_strategy.get("strategy_plan_id"), plan_id, operator_reasoning)
        self.assertEqual(ui_strategy.get("managed_scope"), scope, ui_strategy)
        self.assertTrue(str(trust.get("what_it_did") or "").strip(), trust)
        self.assertTrue(str(trust.get("why_it_did_it") or "").strip(), trust)
        self.assertTrue(str(trust.get("what_it_will_do_next") or "").strip(), trust)
        self.assertTrue(str(trust.get("confidence_tier") or "").strip(), trust)
        self.assertIn("safe_to_continue", trust, trust)


if __name__ == "__main__":
    unittest.main(verbosity=2)