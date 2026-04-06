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
SCOPE_PREFIX = "objective91-95-"


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
        if not body:
            return exc.code, {}
        return exc.code, json.loads(body)


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
        if not body:
            return exc.code, {}
        return exc.code, json.loads(body)


def cleanup_objective91_95_rows() -> None:
    asyncio.run(_cleanup_objective91_95_rows_async())


async def _cleanup_objective91_95_rows_async() -> None:
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

        async def _column_exists(table_name: str, column_name: str) -> bool:
            return bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = $1
                          AND column_name = $2
                    )
                    """,
                    table_name,
                    column_name,
                )
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
        if await _table_exists("execution_overrides"):
            await conn.execute(
                "DELETE FROM execution_overrides WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("workspace_policy_conflict_resolution_events"):
            await conn.execute(
                "DELETE FROM workspace_policy_conflict_resolution_events WHERE managed_scope LIKE $1 AND decision_family = 'execution_policy_gate'",
                scope_like,
            )
        if await _table_exists("workspace_policy_conflict_profiles"):
            await conn.execute(
                "DELETE FROM workspace_policy_conflict_profiles WHERE managed_scope LIKE $1 AND decision_family = 'execution_policy_gate'",
                scope_like,
            )
        if await _table_exists("workspace_state_bus_events"):
            await conn.execute(
                "DELETE FROM workspace_state_bus_events WHERE stream_key LIKE $1",
                f"execution-readiness:{scope_like}",
            )
        if await _table_exists("workspace_state_bus_snapshots"):
            await conn.execute(
                "DELETE FROM workspace_state_bus_snapshots WHERE snapshot_scope LIKE $1",
                f"execution-readiness:{scope_like}",
            )
        if await _column_exists("workspace_execution_truth_governance_profiles", "managed_scope"):
            await conn.execute(
                "DELETE FROM workspace_execution_truth_governance_profiles WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _column_exists("workspace_operator_resolution_commitments", "managed_scope"):
            await conn.execute(
                "DELETE FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _column_exists("capability_executions", "managed_scope"):
            await conn.execute(
                "DELETE FROM capability_executions WHERE managed_scope LIKE $1",
                scope_like,
            )
    finally:
        await conn.close()


class Objective9195ExecutionControlPlaneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 91-95",
            base_url=BASE_URL,
            require_execution_control_plane=True,
        )
        cleanup_objective91_95_rows()

    def setUp(self) -> None:
        cleanup_objective91_95_rows()
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

    def tearDown(self) -> None:
        cleanup_objective91_95_rows()

    def test_execution_policy_gate_creates_trace_intent_and_orchestration(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        status, commitment = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective91-test",
                "managed_scope": scope,
                "decision_type": "lower_autonomy_for_scope",
                "reason": "gate autonomous execution pending operator review",
                "downstream_effects_json": {"autonomy_level": "operator_required"},
                "metadata_json": {"run_scope": scope},
            },
        )
        self.assertEqual(status, 200, commitment)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective91 run workspace check {scope}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "requested_goal": f"inspect {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        execution = payload.get("execution", {})
        self.assertEqual(execution.get("dispatch_decision"), "requires_confirmation", execution)
        self.assertEqual(execution.get("status"), "pending_confirmation", execution)
        self.assertEqual(execution.get("managed_scope"), scope, execution)
        self.assertTrue(str(execution.get("trace_id") or "").startswith("trace-"), execution)

        trace_id = execution["trace_id"]
        status, trace_payload = get_json(f"/execution/traces/{trace_id}")
        self.assertEqual(status, 200, trace_payload)
        trace = trace_payload.get("trace", {})
        self.assertEqual(trace.get("managed_scope"), scope, trace)
        self.assertTrue(bool(trace.get("events")), trace)
        self.assertEqual(trace.get("intent", {}).get("managed_scope"), scope, trace)
        self.assertEqual(trace.get("orchestration", {}).get("current_step_key"), "operator_review", trace)

        status, intents_payload = get_json("/execution/intents", {"managed_scope": scope})
        self.assertEqual(status, 200, intents_payload)
        intents = intents_payload.get("intents", [])
        self.assertEqual(len(intents), 1, intents)
        self.assertEqual(intents[0]["trace_id"], trace_id, intents)

    def test_scope_override_hard_stop_blocks_future_execution_and_marks_stability(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        status, override = post_json(
            "/execution/overrides",
            {
                "actor": "objective94-test",
                "managed_scope": scope,
                "override_type": "hard_stop",
                "reason": "freeze this execution scope",
            },
        )
        self.assertEqual(status, 200, override)
        self.assertEqual(override.get("override", {}).get("override_type"), "hard_stop", override)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective94 run workspace check {scope}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "requested_goal": f"inspect {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        execution = payload.get("execution", {})
        self.assertEqual(execution.get("dispatch_decision"), "blocked", execution)
        self.assertEqual(execution.get("status"), "blocked", execution)
        self.assertEqual(execution.get("reason"), "operator_override_hard_stop", execution)

        status, stability_eval = post_json(
            "/execution/stability/evaluate",
            {
                "actor": "objective95-test",
                "managed_scope": scope,
                "trace_id": execution.get("trace_id", ""),
            },
        )
        self.assertEqual(status, 200, stability_eval)
        stability = stability_eval.get("stability", {})
        self.assertEqual(stability.get("managed_scope"), scope, stability)
        self.assertEqual(stability.get("mitigation_state"), "hard_stop_active", stability)

        status, overrides_payload = get_json("/execution/overrides", {"managed_scope": scope})
        self.assertEqual(status, 200, overrides_payload)
        self.assertEqual(len(overrides_payload.get("overrides", [])), 1, overrides_payload)

    def test_execution_readiness_is_persisted_in_trace_feedback_and_state_bus(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective95 readiness probe {scope}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "requested_goal": f"inspect {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                },
            },
        )
        self.assertEqual(status, 200, payload)

        execution = payload.get("execution", {})
        readiness = (
            execution.get("execution_readiness", {})
            if isinstance(execution.get("execution_readiness", {}), dict)
            else {}
        )
        self.assertEqual(str(readiness.get("signal_name", "")), "execution-readiness", readiness)
        self.assertEqual(str(readiness.get("managed_scope", "")), scope, readiness)
        self.assertIn(str(readiness.get("policy_outcome", "")), {"allow", "degrade", "block"}, readiness)

        trace_id = str(execution.get("trace_id") or "")
        self.assertTrue(trace_id.startswith("trace-"), execution)

        status, trace_payload = get_json(f"/execution/traces/{trace_id}")
        self.assertEqual(status, 200, trace_payload)
        trace = trace_payload.get("trace", {}) if isinstance(trace_payload, dict) else {}
        trace_readiness = (
            trace.get("metadata_json", {}).get("execution_readiness", {})
            if isinstance(trace.get("metadata_json", {}), dict)
            else {}
        )
        self.assertEqual(str(trace_readiness.get("managed_scope", "")), scope, trace_readiness)
        events = trace.get("events", []) if isinstance(trace.get("events", []), list) else []
        self.assertTrue(events, trace)
        last_event = events[-1] if isinstance(events[-1], dict) else {}
        event_readiness = (
            last_event.get("payload_json", {}).get("execution_readiness", {})
            if isinstance(last_event.get("payload_json", {}), dict)
            else {}
        )
        self.assertEqual(str(event_readiness.get("signal_name", "")), "execution-readiness", last_event)

        feedback = execution.get("feedback_json", {}) if isinstance(execution.get("feedback_json", {}), dict) else {}
        feedback_readiness = (
            feedback.get("execution_readiness", {})
            if isinstance(feedback.get("execution_readiness", {}), dict)
            else {}
        )
        self.assertEqual(str(feedback_readiness.get("managed_scope", "")), scope, feedback)

        snapshot_scope = f"execution-readiness:{scope}"
        status, snapshots_payload = get_json(
            "/state-bus/snapshots",
            {"snapshot_scope": snapshot_scope, "limit": 10},
        )
        self.assertEqual(status, 200, snapshots_payload)
        snapshots = snapshots_payload.get("snapshots", []) if isinstance(snapshots_payload, dict) else []
        self.assertEqual(len(snapshots), 1, snapshots)
        snapshot = snapshots[0] if isinstance(snapshots[0], dict) else {}
        snapshot_payload = (
            snapshot.get("state_payload_json", {})
            if isinstance(snapshot.get("state_payload_json", {}), dict)
            else {}
        )
        self.assertEqual(str(snapshot_payload.get("managed_scope", "")), scope, snapshot)

        status, events_payload = get_json(
            "/state-bus/events",
            {"event_domain": "tod.runtime", "stream_key": snapshot_scope, "limit": 20},
        )
        self.assertEqual(status, 200, events_payload)
        bus_events = events_payload.get("events", []) if isinstance(events_payload, dict) else []
        self.assertTrue(bus_events, events_payload)
        latest_event = bus_events[0] if isinstance(bus_events[0], dict) else {}
        self.assertTrue(str(latest_event.get("event_type") or "").startswith("readiness_"), latest_event)


if __name__ == "__main__":
    unittest.main(verbosity=2)