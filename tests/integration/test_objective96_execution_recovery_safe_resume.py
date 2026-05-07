import asyncio
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
SCOPE_PREFIX = "objective96-"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHARED_RUNTIME_DIR = PROJECT_ROOT / "runtime" / "shared"


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


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def refresh_execution_readiness_artifacts() -> None:
    SHARED_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    now = _iso_utc(datetime.now(timezone.utc))
    readiness = {
        "status": "valid",
        "source": "objective96_test_seed",
        "detail": "Fresh execution readiness artifact seeded by Objective 96 integration tests.",
        "valid": True,
        "execution_allowed": True,
        "authoritative": True,
        "freshness_state": "fresh",
        "signal_name": "execution-readiness",
        "evaluated_action": "get-state-bus",
        "policy_outcome": "allow",
        "decision_path": [
            "signal:execution-readiness",
            "status:valid",
            "source:objective96_test_seed",
            "action:get-state-bus",
            "policy_outcome:allow",
        ],
    }
    for file_name, source in (
        ("TOD_MIM_TASK_RESULT.latest.json", "tod-mim-task-result-v1"),
        ("TOD_MIM_COMMAND_STATUS.latest.json", "tod-mim-command-status-v1"),
    ):
        path = SHARED_RUNTIME_DIR / file_name
        payload = {
            "generated_at": now,
            "source": source,
            "execution_readiness": readiness,
            "execution_trace": {
                "action": "get-state-bus",
                "execution_readiness": readiness,
            },
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cleanup_objective96_rows() -> None:
    asyncio.run(_cleanup_objective96_rows_async())


async def _cleanup_objective96_rows_async() -> None:
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

        if await _table_exists("execution_recovery_attempts"):
            await conn.execute(
                "DELETE FROM execution_recovery_attempts WHERE managed_scope LIKE $1 OR trace_id LIKE $2",
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
        if await _table_exists("execution_overrides"):
            await conn.execute(
                "DELETE FROM execution_overrides WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("capability_executions"):
            await conn.execute(
                "DELETE FROM capability_executions WHERE managed_scope LIKE $1",
                scope_like,
            )
    finally:
        await conn.close()


def create_execution(scope: str) -> tuple[int, str]:
    status, payload = post_json(
        "/gateway/intake/text",
        {
            "text": f"objective96 run workspace check {scope}",
            "parsed_intent": "observe_workspace",
            "confidence": 0.97,
            "requested_goal": f"inspect {scope}",
            "metadata_json": {
                "capability": "workspace_check",
                "managed_scope": scope,
            },
        },
    )
    if status != 200:
        raise AssertionError(payload)
    execution = payload.get("execution", {}) if isinstance(payload, dict) else {}
    trace_id = str(execution.get("trace_id") or "")
    execution_id = int(execution.get("execution_id", 0) or 0)
    if execution_id > 0 and trace_id:
        return execution_id, trace_id
    if not trace_id:
        raise AssertionError(payload)
    status, trace_payload = get_json(f"/execution/traces/{trace_id}")
    if status != 200 or not isinstance(trace_payload, dict):
        raise AssertionError(trace_payload)
    trace = trace_payload.get("trace", {}) if isinstance(trace_payload, dict) else {}
    execution_id = int(trace.get("root_execution_id", 0) or 0)
    if execution_id <= 0:
        raise AssertionError(trace_payload)
    return execution_id, trace_id


def update_execution_feedback(execution_id: int, *, status_value: str, reason: str) -> dict:
    refresh_execution_readiness_artifacts()
    status, payload = post_json(
        f"/gateway/capabilities/executions/{execution_id}/feedback",
        {
            "actor": "executor",
            "status": status_value,
            "reason": reason,
            "feedback_json": {"objective": "96"},
        },
    )
    if status != 200:
        raise AssertionError(payload)
    return payload


def recovery_snapshot_scope(scope: str, trace_id: str) -> str:
    return f"execution-recovery:{scope}:{trace_id}"


class Objective96ExecutionRecoverySafeResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        cleanup_objective96_rows()
        refresh_execution_readiness_artifacts()
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
        cleanup_objective96_rows()

    def test_failed_execution_evaluates_bounded_retry_and_records_trace_event(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective96 simulated task failure",
        )

        status, eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(status, 200, eval_payload)
        recovery = eval_payload.get("recovery", {}) if isinstance(eval_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "retry_current_step", recovery)
        self.assertTrue(bool(recovery.get("recovery_allowed", False)), recovery)

        status, attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "retry_current_step",
            },
        )
        self.assertEqual(status, 200, attempt_payload)
        attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
        self.assertEqual(str(attempt.get("status", "")), "accepted", attempt)
        self.assertEqual(str(attempt.get("recovery_decision", "")), "retry_current_step", attempt)

        status, trace_payload = get_json(f"/execution/traces/{trace_id}")
        self.assertEqual(status, 200, trace_payload)
        trace = trace_payload.get("trace", {}) if isinstance(trace_payload, dict) else {}
        self.assertEqual(str(trace.get("current_stage", "")), "recovery", trace)
        orchestration = trace.get("orchestration", {}) if isinstance(trace.get("orchestration", {}), dict) else {}
        self.assertEqual(str(orchestration.get("current_step_key", "")), "recovery", orchestration)
        self.assertEqual(int(orchestration.get("retry_count", 0) or 0), 1, orchestration)
        events = trace.get("events", []) if isinstance(trace.get("events", []), list) else []
        self.assertTrue(
            any(str(item.get("event_type", "")) == "recovery_attempted" for item in events if isinstance(item, dict)),
            events,
        )

    def test_blocked_execution_can_resume_from_checkpoint(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="blocked",
            reason="objective96 transient guardrail block",
        )

        status, eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(status, 200, eval_payload)
        recovery = eval_payload.get("recovery", {}) if isinstance(eval_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "resume_from_checkpoint", recovery)
        self.assertTrue(bool(recovery.get("recovery_allowed", False)), recovery)

        status, attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "resume_from_checkpoint",
            },
        )
        self.assertEqual(status, 200, attempt_payload)
        attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
        self.assertEqual(str(attempt.get("status", "")), "accepted", attempt)

        status, recovery_payload = get_json(f"/execution/recovery/{trace_id}")
        self.assertEqual(status, 200, recovery_payload)
        recovery_state = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        attempts = recovery_state.get("attempts", []) if isinstance(recovery_state.get("attempts", []), list) else []
        self.assertEqual(len(attempts), 1, attempts)
        self.assertEqual(str(attempts[0].get("recovery_decision", "")), "resume_from_checkpoint", attempts)

    def test_pause_override_requires_operator_resume(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)

        status, override_payload = post_json(
            "/execution/overrides",
            {
                "actor": "objective96-test",
                "managed_scope": scope,
                "execution_id": execution_id,
                "trace_id": trace_id,
                "override_type": "pause",
                "reason": "objective96 explicit pause",
            },
        )
        self.assertEqual(status, 200, override_payload)

        status, eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(status, 200, eval_payload)
        recovery = eval_payload.get("recovery", {}) if isinstance(eval_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "require_operator_resume", recovery)
        self.assertTrue(bool(recovery.get("operator_action_required", False)), recovery)

        status, blocked_attempt = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "resume_from_checkpoint",
            },
        )
        self.assertEqual(status, 200, blocked_attempt)
        blocked = blocked_attempt.get("attempt", {}) if isinstance(blocked_attempt, dict) else {}
        self.assertEqual(str(blocked.get("status", "")), "blocked_by_policy", blocked)

        status, allowed_attempt = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "resume_from_checkpoint",
                "operator_ack": True,
            },
        )
        self.assertEqual(status, 200, allowed_attempt)
        allowed = allowed_attempt.get("attempt", {}) if isinstance(allowed_attempt, dict) else {}
        self.assertEqual(str(allowed.get("status", "")), "accepted", allowed)

    def test_blocked_feedback_publishes_recovery_snapshot_and_conflict(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)

        update_execution_feedback(
            execution_id,
            status_value="blocked",
            reason="objective96 blocked state for snapshot publication",
        )

        status, recovery_payload = get_json(f"/execution/recovery/{trace_id}")
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "resume_from_checkpoint", recovery)
        conflict = recovery.get("conflict_resolution", {}) if isinstance(recovery.get("conflict_resolution", {}), dict) else {}
        self.assertEqual(str(conflict.get("decision_family", "")), "execution_recovery", conflict)
        self.assertTrue(str(conflict.get("winning_policy_source", "")).strip(), conflict)

        status, snapshot_payload = get_json(
            "/state-bus/snapshots",
            {"snapshot_scope": recovery_snapshot_scope(scope, trace_id)},
        )
        self.assertEqual(status, 200, snapshot_payload)
        snapshots = snapshot_payload.get("snapshots", []) if isinstance(snapshot_payload, dict) else []
        self.assertEqual(len(snapshots), 1, snapshots)
        snapshot = snapshots[0] if isinstance(snapshots[0], dict) else {}
        self.assertEqual(str(snapshot.get("last_event_domain", "")), "tod.runtime", snapshot)
        state_payload = snapshot.get("state_payload_json", {}) if isinstance(snapshot.get("state_payload_json", {}), dict) else {}
        self.assertEqual(str(state_payload.get("recovery_decision", "")), "resume_from_checkpoint", state_payload)

    def test_recovery_attempt_records_recovered_outcome_after_resume(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="blocked",
            reason="objective96 transient recovery gate",
        )

        status, attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "resume_from_checkpoint",
            },
        )
        self.assertEqual(status, 200, attempt_payload)
        attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
        self.assertEqual(str(attempt.get("status", "")), "accepted", attempt)

        update_execution_feedback(
            execution_id,
            status_value="running",
            reason="objective96 resumed execution running",
        )
        update_execution_feedback(
            execution_id,
            status_value="succeeded",
            reason="objective96 recovery succeeded",
        )

        status, outcomes_payload = get_json(f"/execution/recovery/outcomes/{trace_id}")
        self.assertEqual(status, 200, outcomes_payload)
        latest_outcome = outcomes_payload.get("latest_outcome", {}) if isinstance(outcomes_payload, dict) else {}
        self.assertEqual(str(latest_outcome.get("outcome_status", "")), "recovered", latest_outcome)
        learning_bias = latest_outcome.get("learning_bias_json", {}) if isinstance(latest_outcome.get("learning_bias_json", {}), dict) else {}
        self.assertEqual(str(learning_bias.get("prefer_decision", "")), "resume_from_checkpoint", learning_bias)

        status, recovery_payload = get_json(f"/execution/recovery/{trace_id}")
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        latest_recovery_outcome = recovery.get("latest_outcome", {}) if isinstance(recovery.get("latest_outcome", {}), dict) else {}
        self.assertEqual(str(latest_recovery_outcome.get("outcome_status", "")), "recovered", latest_recovery_outcome)

    def test_recovery_tables_exist_after_bootstrap(self) -> None:
        async def _assert_tables() -> tuple[bool, bool]:
            dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
            conn = await asyncpg.connect(dsn)
            try:
                attempts_exists = bool(
                    await conn.fetchval(
                        "SELECT to_regclass($1) IS NOT NULL",
                        "public.execution_recovery_attempts",
                    )
                )
                outcomes_exists = bool(
                    await conn.fetchval(
                        "SELECT to_regclass($1) IS NOT NULL",
                        "public.execution_recovery_outcomes",
                    )
                )
                return attempts_exists, outcomes_exists
            finally:
                await conn.close()

        attempts_exists, outcomes_exists = asyncio.run(_assert_tables())
        self.assertTrue(attempts_exists)
        self.assertTrue(outcomes_exists)

    def test_hard_stop_blocks_recovery_path(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        execution_id, trace_id = create_execution(scope)

        status, override_payload = post_json(
            "/execution/overrides",
            {
                "actor": "objective96-test",
                "managed_scope": scope,
                "execution_id": execution_id,
                "trace_id": trace_id,
                "override_type": "hard_stop",
                "reason": "objective96 hard stop",
            },
        )
        self.assertEqual(status, 200, override_payload)

        status, eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(status, 200, eval_payload)
        recovery = eval_payload.get("recovery", {}) if isinstance(eval_payload, dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "hard_stop_persisted", recovery)
        self.assertTrue(bool(recovery.get("operator_action_required", False)), recovery)

        status, attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective96-test",
                "source": "objective96",
                "trace_id": trace_id,
                "requested_decision": "resume_from_checkpoint",
                "operator_ack": True,
            },
        )
        self.assertEqual(status, 200, attempt_payload)
        attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
        self.assertEqual(str(attempt.get("status", "")), "blocked_by_policy", attempt)