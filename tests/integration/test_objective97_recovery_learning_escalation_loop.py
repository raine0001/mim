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
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
SCOPE_PREFIX = "objective97-"
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
        "source": "objective97_test_seed",
        "detail": "Fresh execution readiness artifact seeded by Objective 97 integration tests.",
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
            "source:objective97_test_seed",
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


def cleanup_objective97_rows() -> None:
    asyncio.run(_cleanup_objective97_rows_async())


async def _cleanup_objective97_rows_async() -> None:
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

        if await _table_exists("execution_recovery_learning_profiles"):
            await conn.execute(
                "DELETE FROM execution_recovery_learning_profiles WHERE managed_scope LIKE $1",
                scope_like,
            )
        if await _table_exists("execution_recovery_outcomes"):
            await conn.execute(
                "DELETE FROM execution_recovery_outcomes WHERE managed_scope LIKE $1 OR trace_id LIKE $2",
                scope_like,
                f"trace-{SCOPE_PREFIX}%",
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
            "text": f"objective97 run workspace check {scope}",
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
    if execution_id > 0:
        detail_status, detail_payload = get_json(f"/gateway/capabilities/executions/{execution_id}")
        if detail_status == 200 and isinstance(detail_payload, dict):
            detail_trace = str(detail_payload.get("trace_id") or "")
            if detail_trace:
                return execution_id, detail_trace
    raise AssertionError(payload)


def update_execution_feedback(
    execution_id: int,
    *,
    status_value: str,
    reason: str,
    execution_truth: dict | None = None,
) -> dict:
    refresh_execution_readiness_artifacts()
    payload = {
        "actor": "executor",
        "status": status_value,
        "reason": reason,
        "feedback_json": {"objective": "97"},
    }
    if isinstance(execution_truth, dict):
        payload["execution_truth"] = execution_truth
    status, payload = post_json(
        f"/gateway/capabilities/executions/{execution_id}/feedback",
        payload,
    )
    if status != 200:
        raise AssertionError(payload)
    return payload


def create_failed_retry_recovery(scope: str) -> tuple[int, str]:
    execution_id, trace_id = create_execution(scope)
    update_execution_feedback(
        execution_id,
        status_value="failed",
        reason="objective97 simulated task failure",
    )
    status, attempt_payload = post_json(
        "/execution/recovery/attempt",
        {
            "actor": "objective97-test",
            "source": "objective97",
            "trace_id": trace_id,
            "requested_decision": "retry_current_step",
        },
    )
    if status != 200:
        raise AssertionError(attempt_payload)
    attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
    if str(attempt.get("status") or "") != "accepted":
        raise AssertionError(attempt_payload)
    update_execution_feedback(
        execution_id,
        status_value="failed",
        reason="objective97 failed again after retry",
    )
    status, outcomes_payload = get_json(f"/execution/recovery/outcomes/{trace_id}")
    if status != 200:
        raise AssertionError(outcomes_payload)
    latest_outcome = outcomes_payload.get("latest_outcome", {}) if isinstance(outcomes_payload, dict) else {}
    if str(latest_outcome.get("outcome_status") or "") != "failed_again":
        raise AssertionError(outcomes_payload)
    return execution_id, trace_id


def create_recovered_resume(scope: str) -> tuple[int, str]:
    execution_id, trace_id = create_execution(scope)
    update_execution_feedback(
        execution_id,
        status_value="blocked",
        reason="objective97 transient recovery gate",
    )
    status, attempt_payload = post_json(
        "/execution/recovery/attempt",
        {
            "actor": "objective97-test",
            "source": "objective97",
            "trace_id": trace_id,
            "requested_decision": "resume_from_checkpoint",
        },
    )
    if status != 200:
        raise AssertionError(attempt_payload)
    attempt = attempt_payload.get("attempt", {}) if isinstance(attempt_payload, dict) else {}
    if str(attempt.get("status") or "") != "accepted":
        raise AssertionError(attempt_payload)
    update_execution_feedback(
        execution_id,
        status_value="running",
        reason="objective97 resumed execution running",
    )
    update_execution_feedback(
        execution_id,
        status_value="succeeded",
        reason="objective97 recovery succeeded",
    )
    status, outcomes_payload = get_json(f"/execution/recovery/outcomes/{trace_id}")
    if status != 200:
        raise AssertionError(outcomes_payload)
    latest_outcome = outcomes_payload.get("latest_outcome", {}) if isinstance(outcomes_payload, dict) else {}
    if str(latest_outcome.get("outcome_status") or "") != "recovered":
        raise AssertionError(outcomes_payload)
    return execution_id, trace_id


class Objective97RecoveryLearningEscalationLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 97",
            base_url=BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective97_rows()
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
        cleanup_objective97_rows()

    def test_repeated_failed_retry_escalates_next_recovery(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 next failure after repeated bad retries",
        )

        status, recovery_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
            },
        )
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "require_operator_resume", recovery)
        self.assertEqual(str(learning.get("escalation_decision", "")), "require_operator_takeover", learning)
        self.assertGreaterEqual(int(learning.get("failed_again_count", 0) or 0), 2, learning)
        self.assertIn("failed again", str(recovery.get("why_recovery_escalated_before_retry", "")).lower(), recovery)

        status, profiles_payload = get_json(
            "/execution/recovery/learning/profiles",
            {"managed_scope": scope, "recovery_decision": "retry_current_step"},
        )
        self.assertEqual(status, 200, profiles_payload)
        latest_profile = profiles_payload.get("latest_profile", {}) if isinstance(profiles_payload, dict) else {}
        self.assertEqual(str(latest_profile.get("escalation_decision", "")), "require_operator_takeover", latest_profile)

    def test_repeated_successful_resume_stays_bounded_and_inspectable(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_recovered_resume(scope)
        create_recovered_resume(scope)

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="blocked",
            reason="objective97 blocked run after successful resumes",
        )

        status, recovery_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
            },
        )
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "resume_from_checkpoint", recovery)
        self.assertTrue(bool(recovery.get("recovery_allowed", False)), recovery)
        self.assertEqual(str(learning.get("learning_state", "")), "reinforced_recovery_path", learning)
        self.assertEqual(str(learning.get("escalation_decision", "")), "continue_bounded_recovery", learning)
        self.assertGreaterEqual(int(learning.get("recovered_count", 0) or 0), 2, learning)

    def test_mixed_history_keeps_retry_escalation_specific_to_failed_pattern(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_recovered_resume(scope)
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 mixed-history failure after earlier success",
        )

        status, recovery_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
            },
        )
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        learning = (
            recovery.get("recovery_learning", {})
            if isinstance(recovery.get("recovery_learning", {}), dict)
            else {}
        )
        self.assertEqual(str(recovery.get("recovery_decision", "")), "require_operator_resume", recovery)
        self.assertEqual(str(learning.get("recovery_decision", "")), "retry_current_step", learning)
        self.assertEqual(str(learning.get("escalation_decision", "")), "require_operator_takeover", learning)
        self.assertGreaterEqual(int(learning.get("failed_again_count", 0) or 0), 2, learning)

        status, resume_profiles_payload = get_json(
            "/execution/recovery/learning/profiles",
            {"managed_scope": scope, "recovery_decision": "resume_from_checkpoint"},
        )
        self.assertEqual(status, 200, resume_profiles_payload)
        resume_profile = (
            resume_profiles_payload.get("latest_profile", {})
            if isinstance(resume_profiles_payload, dict)
            else {}
        )
        self.assertEqual(str(resume_profile.get("escalation_decision", "")), "continue_bounded_recovery", resume_profile)

    def test_recovery_learning_remains_scope_local(self) -> None:
        scope_a = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        scope_b = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope_a)
        create_failed_retry_recovery(scope_a)

        execution_id, trace_id = create_execution(scope_b)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 unrelated scope failure",
        )

        status, recovery_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope_b,
            },
        )
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
        self.assertEqual(str(recovery.get("recovery_decision", "")), "retry_current_step", recovery)
        self.assertEqual(str(learning.get("escalation_decision", "")), "continue_bounded_recovery", learning)
        self.assertEqual(int(learning.get("failed_again_count", 0) or 0), 0, learning)

    def test_operator_ui_explains_recovery_escalation(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        execution_id, _trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 ui escalation trigger",
        )

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        learning = (
            operator_reasoning.get("execution_recovery_learning", {})
            if isinstance(operator_reasoning.get("execution_recovery_learning", {}), dict)
            else {}
        )
        recovery = (
            operator_reasoning.get("execution_recovery", {})
            if isinstance(operator_reasoning.get("execution_recovery", {}), dict)
            else {}
        )
        self.assertEqual(str(learning.get("managed_scope", "")), scope, learning)
        self.assertEqual(str(learning.get("escalation_decision", "")), "require_operator_takeover", learning)
        self.assertIn("failed again", str(learning.get("summary", "")).lower(), learning)
        self.assertIn(
            "failed again",
            str(recovery.get("why_recovery_escalated_before_retry", "")).lower(),
            recovery,
        )

    def test_learning_reset_endpoint_clears_scope_profiles(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        status, reset_payload = post_json(
            "/execution/recovery/learning/reset",
            {
                "actor": "objective97-test",
                "managed_scope": scope,
                "reason": "objective97 integration reset",
            },
        )
        self.assertEqual(status, 200, reset_payload)
        self.assertGreaterEqual(int(reset_payload.get("updated", 0) or 0), 1, reset_payload)

        status, profiles_payload = get_json(
            "/execution/recovery/learning/profiles",
            {"managed_scope": scope},
        )
        self.assertEqual(status, 200, profiles_payload)
        latest_profile = profiles_payload.get("latest_profile", {}) if isinstance(profiles_payload, dict) else {}
        self.assertEqual(str(latest_profile.get("learning_state", "")), "manual_reset", latest_profile)
        self.assertEqual(int(latest_profile.get("sample_count", 0) or 0), 0, latest_profile)
        self.assertEqual(int(latest_profile.get("failed_again_count", 0) or 0), 0, latest_profile)

    def test_environment_shift_invalidates_learning_profile(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 environment shift simulated",
            execution_truth={
                "execution_id": execution_id,
                "capability_name": "workspace_check",
                "runtime_outcome": "failed",
                "environment_shift_detected": True,
                "truth_confidence": 0.92,
                "published_at": _iso_utc(datetime.now(timezone.utc)),
            },
        )

        status, recovery_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "trace_id": trace_id,
                "execution_id": execution_id,
                "managed_scope": scope,
                "metadata_json": {"environment_shift_detected": True},
            },
        )
        self.assertEqual(status, 200, recovery_payload)
        recovery = recovery_payload.get("recovery", {}) if isinstance(recovery_payload, dict) else {}
        learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
        self.assertEqual(str(learning.get("learning_state", "")), "invalidated_environment_shift", learning)
        self.assertEqual(int(learning.get("sample_count", 0) or 0), 0, learning)
        metadata_json = learning.get("metadata_json", {}) if isinstance(learning.get("metadata_json", {}), dict) else {}
        self.assertTrue(bool(metadata_json.get("environment_shift_detected", False)), metadata_json)

    def test_recovery_learning_telemetry_contract(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        status, telemetry_payload = get_json(
            "/execution/recovery/learning/telemetry",
            {"managed_scope": scope, "limit": 200},
        )
        self.assertEqual(status, 200, telemetry_payload)
        self.assertIn("window", telemetry_payload)
        self.assertIn("metrics", telemetry_payload)
        self.assertIn("alerts", telemetry_payload)
        self.assertIsInstance(telemetry_payload.get("alerts", {}), dict)
        self.assertIn("escalation_rate_high", telemetry_payload.get("alerts", {}))

    def test_state_bus_snapshot_contains_recovery_learning_contract(self) -> None:
        scope = f"{SCOPE_PREFIX}{uuid4().hex[:10]}"
        create_failed_retry_recovery(scope)
        create_failed_retry_recovery(scope)

        execution_id, trace_id = create_execution(scope)
        update_execution_feedback(
            execution_id,
            status_value="failed",
            reason="objective97 state bus contract trigger",
        )

        status, recovery_payload = get_json(f"/execution/recovery/{trace_id}")
        self.assertEqual(status, 200, recovery_payload)

        snapshot_scope = f"execution-recovery:{scope}:{trace_id}"
        encoded_scope = urllib.parse.quote(snapshot_scope, safe="")
        status, snapshot_payload = get_json(f"/state-bus/snapshots/{encoded_scope}")
        self.assertEqual(status, 200, snapshot_payload)
        snapshot = snapshot_payload.get("snapshot", {}) if isinstance(snapshot_payload, dict) else {}
        state_payload = snapshot.get("state_payload_json", {}) if isinstance(snapshot.get("state_payload_json", {}), dict) else {}

        for key in ("trace_id", "managed_scope", "recovery_decision", "summary", "recovery_learning"):
            self.assertIn(key, state_payload, state_payload)
        learning = state_payload.get("recovery_learning", {}) if isinstance(state_payload.get("recovery_learning", {}), dict) else {}
        for key in ("recovery_decision", "learning_state", "escalation_decision", "sample_count"):
            self.assertIn(key, learning, learning)

    def test_recovery_learning_table_exists_after_bootstrap(self) -> None:
        async def _assert_table() -> bool:
            dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
            conn = await asyncpg.connect(dsn)
            try:
                return bool(
                    await conn.fetchval(
                        "SELECT to_regclass($1) IS NOT NULL",
                        "public.execution_recovery_learning_profiles",
                    )
                )
            finally:
                await conn.close()

        self.assertTrue(asyncio.run(_assert_table()))