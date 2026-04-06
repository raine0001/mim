import asyncio
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHARED_RUNTIME_DIR = PROJECT_ROOT / "runtime" / "shared"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh_execution_readiness(*, action: str) -> dict:
    return {
        "status": "valid",
        "source": "objective90_test_seed",
        "detail": "Fresh execution readiness artifact seeded by Objective 90 integration tests.",
        "valid": True,
        "execution_allowed": True,
        "authoritative": True,
        "freshness_state": "fresh",
        "signal_name": "execution-readiness",
        "evaluated_action": str(action or "get-state-bus"),
        "policy_outcome": "allow",
        "decision_path": [
            "signal:execution-readiness",
            "status:valid",
            "source:objective90_test_seed",
            f"action:{str(action or 'get-state-bus')}",
            "policy_outcome:allow",
        ],
    }


def _refresh_execution_readiness_artifact(path: Path, *, source: str) -> None:
    now = datetime.now(timezone.utc)
    action = "get-state-bus"
    readiness = _fresh_execution_readiness(action=action)
    payload = {
        "generated_at": _iso_utc(now),
        "source": source,
        "execution_readiness": readiness,
        "execution_trace": {
            "action": action,
            "execution_readiness": readiness,
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def refresh_execution_readiness_artifacts() -> None:
    SHARED_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _refresh_execution_readiness_artifact(
        SHARED_RUNTIME_DIR / "TOD_MIM_TASK_RESULT.latest.json",
        source="tod-mim-task-result-v1",
    )
    _refresh_execution_readiness_artifact(
        SHARED_RUNTIME_DIR / "TOD_MIM_COMMAND_STATUS.latest.json",
        source="tod-mim-command-status-v1",
    )


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
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}


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
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"detail": body}


def cleanup_objective90_rows() -> None:
    asyncio.run(_cleanup_objective90_rows_async())


async def _cleanup_objective90_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_inquiry_questions WHERE source = 'objective90' OR dedupe_key LIKE 'stewardship_persistent_degradation:%objective90-%' OR metadata_json::text LIKE '%objective90-%'"
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.workspace_policy_conflict_resolution_events') IS NOT NULL THEN
                    DELETE FROM workspace_policy_conflict_resolution_events
                    WHERE managed_scope LIKE 'objective90-%';
                END IF;
            END $$
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('public.workspace_policy_conflict_profiles') IS NOT NULL THEN
                    DELETE FROM workspace_policy_conflict_profiles
                    WHERE managed_scope LIKE 'objective90-%';
                END IF;
            END $$
            """
        )
        await conn.execute(
            "DELETE FROM workspace_proposal_policy_preference_profiles WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE source = 'objective90' OR related_zone LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_execution_truth_governance_profiles WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitment_monitoring_profiles WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitment_outcome_profiles WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_operator_resolution_commitments WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_autonomy_boundary_profiles WHERE scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_stewardship_cycles WHERE stewardship_id IN (SELECT id FROM workspace_stewardship_states WHERE managed_scope LIKE 'objective90-%')"
        )
        await conn.execute(
            "DELETE FROM workspace_stewardship_states WHERE managed_scope LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM user_preferences WHERE user_id = 'operator' AND value::text LIKE '%objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_proposals WHERE source = 'objective90' OR related_zone LIKE 'objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_state_bus_events WHERE stream_key LIKE 'execution-readiness:objective90-%'"
        )
        await conn.execute(
            "DELETE FROM workspace_state_bus_snapshots WHERE snapshot_scope LIKE 'execution-readiness:objective90-%'"
        )
    finally:
        await conn.close()


def list_policy_conflict_profiles(*, managed_scope: str, decision_family: str) -> list[dict]:
    return asyncio.run(
        _list_policy_conflict_profiles_async(
            managed_scope=managed_scope,
            decision_family=decision_family,
        )
    )


async def _list_policy_conflict_profiles_async(*, managed_scope: str, decision_family: str) -> list[dict]:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT decision_family, proposal_type, conflict_state, winning_policy_source,
                   losing_policy_sources_json, precedence_rule, policy_effects_json
            FROM workspace_policy_conflict_profiles
            WHERE managed_scope = $1 AND decision_family = $2
            ORDER BY id DESC
            """,
            str(managed_scope),
            str(decision_family),
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


def seed_workspace_proposal(
    *, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int
) -> int:
    return asyncio.run(
        _seed_workspace_proposal_async(
            run_id=run_id,
            proposal_type=proposal_type,
            related_zone=related_zone,
            confidence=confidence,
            age_seconds=age_seconds,
        )
    )


async def _seed_workspace_proposal_async(
    *, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int
) -> int:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        created_at = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(age_seconds)))
        row = await conn.fetchrow(
            """
            INSERT INTO workspace_proposals (
                proposal_type, title, description, status, confidence, priority_score,
                priority_reason, source, related_zone, related_object_id, source_execution_id,
                trigger_json, metadata_json, created_at
            ) VALUES (
                $1, $2, $3, 'pending', $4, 0.0,
                '', 'objective90', $5, NULL, NULL,
                $6::jsonb, $7::jsonb, $8
            )
            RETURNING id
            """,
            str(proposal_type),
            f"objective90 {proposal_type} {run_id}",
            f"seeded proposal {proposal_type} for objective90 {run_id}",
            float(confidence),
            str(related_zone),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id}),
            created_at,
        )
        return int(row["id"])
    finally:
        await conn.close()


def get_workspace_proposal(proposal_id: int) -> dict:
    status, payload = get_json(f"/workspace/proposals/{proposal_id}")
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError({"status": status, "payload": payload})
    return payload


def seed_stewardship_state(*, scope: str, run_id: str) -> None:
    asyncio.run(_seed_stewardship_state_async(scope=scope, run_id=run_id))


async def _seed_stewardship_state_async(*, scope: str, run_id: str) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO workspace_stewardship_states (
                source, actor, status, managed_scope, maintenance_priority, current_health,
                target_environment_state_json, cycle_count, last_decision_summary,
                linked_strategy_goal_ids_json, linked_maintenance_run_ids_json,
                linked_strategy_types_json, metadata_json, created_at
            ) VALUES (
                'objective90', 'workspace', 'active', $1, 'normal', 0.72,
                '{}'::jsonb, 1, $2, '[]'::jsonb, '[]'::jsonb,
                '[]'::jsonb, $3::jsonb, NOW()
            )
            """,
            str(scope),
            f"objective90 stewardship context for {scope}",
            json.dumps({"run_id": run_id}),
        )
    finally:
        await conn.close()


def register_workspace_scan() -> None:
    status, payload = post_json(
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
    assert status == 200, payload


def create_stale_observation(*, zone: str, run_id: str) -> None:
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    status, event = post_json(
        "/gateway/intake/text",
        {
            "text": f"objective90 inquiry stale scan {run_id}",
            "parsed_intent": "observe_workspace",
            "confidence": 0.95,
            "metadata_json": {
                "scan_mode": "full",
                "scan_area": zone,
                "confidence_threshold": 0.6,
                "run_id": run_id,
            },
        },
    )
    assert status == 200, event
    execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
    assert execution_id > 0, event

    for state in ["accepted", "running"]:
        status, updated = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": state,
                "reason": state,
                "actor": "tod",
                "feedback_json": {"run_id": run_id},
            },
        )
        assert status == 200, updated

    status, done = post_json(
        f"/gateway/capabilities/executions/{execution_id}/feedback",
        {
            "status": "succeeded",
            "reason": "scan complete",
            "actor": "tod",
            "feedback_json": {
                "run_id": run_id,
                "observations": [
                    {
                        "label": f"obj90-stale-{run_id}",
                        "zone": zone,
                        "confidence": 0.91,
                        "observed_at": stale_time,
                    }
                ],
            },
        },
    )
    assert status == 200, done


def seed_objective90_stewardship_followup(*, scope: str, run_id: str) -> None:
    register_workspace_scan()
    create_stale_observation(zone=scope, run_id=run_id)

    status, pref = post_json(
        "/preferences",
        {
            "user_id": "operator",
            "preference_type": "stewardship_priority:default",
            "value": 0.8,
            "confidence": 0.9,
            "source": "objective90",
        },
    )
    assert status == 200, pref

    status, goals = post_json(
        "/strategy/goals/build",
        {
            "actor": "objective90-test",
            "source": "objective90",
            "lookback_hours": 48,
            "max_items_per_domain": 50,
            "max_goals": 4,
            "min_context_confidence": 0.0,
            "min_domains_required": 1,
            "min_cross_domain_links": 0,
            "generate_horizon_plans": False,
            "generate_improvement_proposals": False,
            "generate_maintenance_cycles": False,
            "metadata_json": {"run_id": run_id},
        },
    )
    assert status == 200, goals

    status, boundary = post_json(
        "/autonomy/boundaries/recompute",
        {
            "actor": "objective90-test",
            "source": "objective90",
            "scope": scope,
            "lookback_hours": 72,
            "min_samples": 1,
            "apply_recommended_boundaries": False,
            "hard_ceiling_overrides": {"human_safety": "bounded_auto"},
            "evidence_inputs_override": {
                "success_rate": 0.9,
                "escalation_rate": 0.05,
                "retry_rate": 0.05,
                "interruption_rate": 0.05,
                "memory_delta_rate": 0.7,
                "sample_count": 20,
                "manual_override_count": 0,
                "replan_count": 0,
                "constraint_high_risk_count": 0,
                "stability_signal": 0.9,
                "human_present_rate": 0.0,
                "active_experiment_count": 0,
            },
            "metadata_json": {"run_id": run_id},
        },
    )
    assert status == 200, boundary

    status, cycled = post_json(
        "/stewardship/cycle",
        {
            "actor": "objective90-test",
            "source": "objective90",
            "managed_scope": scope,
            "stale_after_seconds": 300,
            "lookback_hours": 168,
            "max_strategies": 5,
            "max_actions": 5,
            "auto_execute": False,
            "force_degraded": True,
            "target_environment_state": {
                "zone_freshness_seconds": 300,
                "critical_object_confidence": 0.8,
                "max_degraded_zones": 0,
                "max_zone_uncertainty_score": 0.35,
                "max_system_drift_rate": 0.05,
                "max_missing_key_objects": 0,
                "key_objects": [f"obj90-critical-missing-{run_id}"],
                "intervention_policy": {
                    "max_interventions_per_window": 1,
                    "window_minutes": 180,
                    "scope_cooldown_seconds": 3600,
                    "per_strategy_limit": 1,
                },
            },
            "metadata_json": {"run_id": run_id, "phase": "objective90-inquiry"},
        },
    )
    assert status == 200, cycled


def seed_objective90_plan(*, scope: str, run_id: str) -> None:
    status, plan = post_json(
        "/planning/horizon/plans",
        {
            "actor": "objective90-test",
            "source": "objective90-inquiry",
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
                }
            ],
            "priority_policy": {
                "map_freshness_limit_seconds": 900,
                "min_target_confidence": 0.85,
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
    assert status == 200, plan


def seed_objective90_target_confidence_warnings(
    *,
    run_id: str,
    count: int,
    target_confidence: float = 0.62,
) -> None:
    evaluation_ids: list[int] = []
    for index in range(max(1, int(count))):
        status, evaluation = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective90-test",
                "source": "objective90-governed-inquiry",
                "goal": {
                    "goal_id": f"obj90-target-confidence-{run_id}-{index}-{uuid4().hex[:6]}",
                    "desired_state": "stable_execution",
                },
                "action_plan": {
                    "action_type": "execute_action_plan",
                    "is_physical": True,
                },
                "workspace_state": {
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": target_confidence,
                    "map_freshness_seconds": 120,
                },
                "system_state": {
                    "throttle_blocked": False,
                    "integrity_risk": False,
                },
                "policy_state": {
                    "min_target_confidence": 0.85,
                    "map_freshness_limit_seconds": 900,
                    "unlawful_action": False,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        assert status == 200, evaluation
        evaluation_ids.append(int(evaluation.get("evaluation_id", 0) or 0))

    for evaluation_id in evaluation_ids:
        status, outcome = post_json(
            "/constraints/outcomes",
            {
                "actor": "objective90-test",
                "evaluation_id": evaluation_id,
                "result": "success",
                "outcome_quality": 0.85,
                "metadata_json": {"run_id": run_id},
            },
        )
        assert status == 200, outcome


def seed_objective90_low_confidence_friction(*, run_id: str, count: int = 3) -> None:
    evaluation_ids: list[int] = []
    for index in range(max(1, int(count))):
        status, evaluation = post_json(
            "/constraints/evaluate",
            {
                "actor": "objective90-test",
                "source": "objective90-governed-inquiry",
                "goal": {
                    "goal_id": f"obj90-soft-friction-{run_id}-{index}-{uuid4().hex[:6]}",
                    "desired_state": "stable_execution",
                },
                "action_plan": {
                    "action_type": "execute_action_plan",
                    "is_physical": True,
                },
                "workspace_state": {
                    "human_in_workspace": False,
                    "human_near_target_zone": False,
                    "human_near_motion_path": False,
                    "shared_workspace_active": False,
                    "target_confidence": 0.62,
                    "map_freshness_seconds": 120,
                },
                "system_state": {
                    "throttle_blocked": False,
                    "integrity_risk": False,
                },
                "policy_state": {
                    "min_target_confidence": 0.85,
                    "map_freshness_limit_seconds": 900,
                    "unlawful_action": False,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        assert status == 200, evaluation
        evaluation_ids.append(int(evaluation.get("evaluation_id", 0) or 0))

    for evaluation_id in evaluation_ids:
        status, outcome = post_json(
            "/constraints/outcomes",
            {
                "actor": "objective90-test",
                "evaluation_id": evaluation_id,
                "result": "success",
                "outcome_quality": 0.9,
                "metadata_json": {"run_id": run_id},
            },
        )
        assert status == 200, outcome


def generate_inquiry_questions(
    *,
    run_id: str,
    source: str,
    lookback_hours: int = 24,
    extra_metadata: dict | None = None,
) -> dict:
    status, generated = post_json(
        "/inquiry/questions/generate",
        {
            "actor": "objective90-test",
            "source": source,
            "lookback_hours": lookback_hours,
            "max_questions": 10,
            "min_soft_friction_count": 3,
            "metadata_json": {
                "run_id": run_id,
                **(extra_metadata if isinstance(extra_metadata, dict) else {}),
            },
        },
    )
    assert status == 200, generated
    assert isinstance(generated, dict), generated
    return generated


def set_bounded_auto_inquiry_state(*, run_id: str, scope: str) -> None:
    status, payload = post_json(
        "/autonomy/boundaries/recompute",
        {
            "actor": "objective90-test",
            "source": "objective90-governed-inquiry",
            "scope": scope,
            "lookback_hours": 48,
            "min_samples": 5,
            "apply_recommended_boundaries": True,
            "hard_ceiling_overrides": {
                "human_safety": True,
                "legality": True,
                "system_integrity": True,
            },
            "evidence_inputs_override": {
                "sample_count": 20,
                "success_rate": 0.96,
                "escalation_rate": 0.02,
                "retry_rate": 0.04,
                "interruption_rate": 0.02,
                "memory_delta_rate": 0.85,
                "override_rate": 0.02,
                "replan_rate": 0.03,
                "environment_stability": 0.9,
                "development_confidence": 0.82,
                "constraint_reliability": 0.93,
                "experiment_confidence": 0.84,
            },
            "metadata_json": {"run_id": run_id},
        },
    )
    assert status == 200, payload
    boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
    if str(boundary.get("current_level", "")) != "bounded_auto":
        force_scope_autonomy_level(scope=scope, level="bounded_auto")


def force_scope_autonomy_level(*, scope: str, level: str) -> None:
    asyncio.run(_force_scope_autonomy_level_async(scope=scope, level=level))


async def _force_scope_autonomy_level_async(*, scope: str, level: str) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT id
            FROM workspace_autonomy_boundary_profiles
            WHERE scope = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            str(scope),
        )
        if row is None:
            raise AssertionError(f"missing autonomy profile for scope {scope}")
        await conn.execute(
            """
            UPDATE workspace_autonomy_boundary_profiles
            SET current_level = $2,
                profile_status = 'applied',
                adjustment_reason = 'objective90-test-forced-level'
            WHERE id = $1
            """,
            int(row["id"]),
            str(level),
        )
    finally:
        await conn.close()


def set_monitoring_autonomy_state(*, autonomy_state: dict) -> dict:
    return asyncio.run(_set_monitoring_autonomy_state_async(autonomy_state=autonomy_state))


async def _set_monitoring_autonomy_state_async(*, autonomy_state: dict) -> dict:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT id, metadata_json FROM workspace_monitoring_states ORDER BY id ASC LIMIT 1"
        )
        previous_autonomy: dict = {}
        if row is not None:
            metadata = row["metadata_json"] if isinstance(row["metadata_json"], dict) else {}
            previous_autonomy = dict(metadata.get("autonomy", {})) if isinstance(metadata.get("autonomy", {}), dict) else {}
            updated_metadata = {
                **metadata,
                "autonomy": dict(autonomy_state),
            }
            await conn.execute(
                "UPDATE workspace_monitoring_states SET metadata_json = $2::jsonb WHERE id = $1",
                int(row["id"]),
                json.dumps(updated_metadata),
            )
            return previous_autonomy

        await conn.execute(
            """
            INSERT INTO workspace_monitoring_states (
                desired_running, runtime_status, scan_trigger_mode, interval_seconds,
                freshness_threshold_seconds, cooldown_seconds, max_scan_rate,
                priority_zones, last_deltas_json, last_proposal_ids,
                last_snapshot_json, metadata_json
            ) VALUES (
                FALSE, 'stopped', 'interval', 30,
                900, 10, 6,
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                '{}'::jsonb, $1::jsonb
            )
            """,
            json.dumps({"autonomy": dict(autonomy_state)}),
        )
        return previous_autonomy
    finally:
        await conn.close()


def create_resolution_commitment(
    *,
    scope: str,
    run_id: str,
    decision_type: str,
    downstream_effects_json: dict | None = None,
    authority_level: str = "temporary_safety_hold",
) -> int:
    status, created = post_json(
        "/operator/resolution-commitments",
        {
            "actor": "objective90-test-operator",
            "managed_scope": scope,
            "decision_type": decision_type,
            "reason": f"objective90 commitment {decision_type}",
            "recommendation_snapshot_json": {
                "recommendation": "objective90 arbitration probe",
                "governance_decision": "request_operator_review",
            },
            "authority_level": authority_level,
            "confidence": 0.95,
            "duration_seconds": 7200,
            "downstream_effects_json": downstream_effects_json or {},
            "metadata_json": {"run_id": run_id},
        },
    )
    if status != 200:
        raise AssertionError(created)
    commitment = created.get("commitment", {}) if isinstance(created, dict) else {}
    commitment_id = int(commitment.get("commitment_id", 0) or 0)
    if commitment_id <= 0:
        raise AssertionError(created)
    return commitment_id


def resolve_resolution_commitment(
    *, commitment_id: int, source: str, target_status: str, run_id: str
) -> None:
    status, resolved = post_json(
        f"/operator/resolution-commitments/{commitment_id}/resolve",
        {
            "actor": "objective90-test-operator",
            "source": source,
            "target_status": target_status,
            "reason": f"objective90 terminal {target_status}",
            "lookback_hours": 24,
            "metadata_json": {"run_id": run_id},
        },
    )
    if status != 200:
        seed_resolution_commitment_outcome(
            commitment_id=commitment_id,
            source=source,
            target_status=target_status,
            run_id=run_id,
        )


def seed_resolution_commitment_outcome(
    *, commitment_id: int, source: str, target_status: str, run_id: str
) -> None:
    asyncio.run(
        _seed_resolution_commitment_outcome_async(
            commitment_id=commitment_id,
            source=source,
            target_status=target_status,
            run_id=run_id,
        )
    )


async def _seed_resolution_commitment_outcome_async(
    *, commitment_id: int, source: str, target_status: str, run_id: str
) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        commitment = await conn.fetchrow(
            """
            SELECT id, managed_scope, commitment_family, decision_type
            FROM workspace_operator_resolution_commitments
            WHERE id = $1
            """,
            int(commitment_id),
        )
        if commitment is None:
            raise AssertionError(f"missing commitment {commitment_id}")

        await conn.execute(
            """
            UPDATE workspace_operator_resolution_commitments
            SET status = $2
            WHERE id = $1
            """,
            int(commitment_id),
            str(target_status),
        )

        decision_type = str(commitment["decision_type"] or "").strip()
        commitment_family = str(commitment["commitment_family"] or "").strip()
        managed_scope = str(commitment["managed_scope"] or "").strip()
        learning_signals = {
            "repeat_commitment_bias": "repeat",
            "inquiry_bias": "similar_commitment_can_repeat_with_monitoring",
            "strategy_priority_delta": 0.08,
            "backlog_priority_delta": -0.04,
            "autonomy_level_cap": (
                "operator_required"
                if decision_type == "require_additional_evidence"
                else ("trusted_auto" if decision_type == "increase_autonomy_for_scope" else "")
            ),
            "decision_type": decision_type,
            "commitment_family": commitment_family,
            "monitoring_stability": 0.2,
            "retry_pressure_score": 0.0,
        }
        await conn.execute(
            """
            INSERT INTO workspace_operator_resolution_commitment_outcome_profiles (
                source, actor, commitment_id, managed_scope, commitment_family,
                decision_type, status, commitment_status, outcome_status,
                outcome_reason, evaluation_window_hours, evidence_count,
                monitoring_profile_count, stewardship_cycle_count, maintenance_run_count,
                inquiry_question_count, execution_count, retry_count,
                blocked_auto_execution_count, allowed_auto_execution_count,
                potential_violation_count, governance_conflict_count,
                effectiveness_score, stability_score, retry_pressure_score,
                learning_confidence, learning_signals_json, pattern_summary_json,
                recommended_actions_json, reasoning_json, metadata_json
            ) VALUES (
                $1, 'workspace', $2, $3, $4,
                $5, 'evaluated', $6, $7,
                $8, 24, 1,
                0, 0, 0,
                0, 0, 0,
                0, 0,
                0, 0,
                0.92, 0.8, 0.0,
                0.95, $9::jsonb, $10::jsonb,
                '[]'::jsonb, $11::jsonb, $12::jsonb
            )
            """,
            str(source),
            int(commitment_id),
            managed_scope,
            commitment_family,
            decision_type,
            str(target_status),
            str(target_status),
            f"objective90 terminal {target_status}",
            json.dumps(learning_signals),
            json.dumps(
                {
                    "same_decision_type_count": 2,
                    "repeated_successful_commitments": 2 if str(target_status) == "satisfied" else 0,
                    "repeated_ineffective_commitments": 0,
                    "repeated_harmful_commitments": 0,
                    "conflicting_commitments": False,
                    "recent_outcomes": [],
                }
            ),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id, "manual_resolution": True}),
        )
    finally:
        await conn.close()


def converge_scope_preferences(*, scope: str) -> list[dict]:
    status, converged = post_json(
        "/operator/preferences/converge",
        {
            "actor": "objective90-test",
            "source": "objective90",
            "managed_scope": scope,
            "lookback_hours": 168,
            "min_evidence": 3,
        },
    )
    if status != 200:
        raise AssertionError(converged)
    return converged.get("preferences", []) if isinstance(converged, dict) else []


def set_policy_conflict_cooldown(
    *, managed_scope: str, decision_family: str, minutes_offset: int
) -> None:
    asyncio.run(
        _set_policy_conflict_cooldown_async(
            managed_scope=managed_scope,
            decision_family=decision_family,
            minutes_offset=minutes_offset,
        )
    )


async def _set_policy_conflict_cooldown_async(
    *, managed_scope: str, decision_family: str, minutes_offset: int
) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            UPDATE workspace_policy_conflict_profiles
            SET cooldown_until = NOW() + (($3::text || ' minutes')::interval)
            WHERE managed_scope = $1
              AND decision_family = $2
            """,
            str(managed_scope),
            str(decision_family),
            str(int(minutes_offset)),
        )
    finally:
        await conn.close()


def seed_execution_truth_governance(
    *, managed_scope: str, run_id: str, governance_decision: str
) -> None:
    asyncio.run(
        _seed_execution_truth_governance_async(
            managed_scope=managed_scope,
            run_id=run_id,
            governance_decision=governance_decision,
        )
    )


async def _seed_execution_truth_governance_async(
    *, managed_scope: str, run_id: str, governance_decision: str
) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO workspace_execution_truth_governance_profiles (
                source, actor, managed_scope, status, lookback_hours,
                execution_count, signal_count, confidence, governance_state,
                governance_decision, governance_reason, trigger_counts_json,
                trigger_evidence_json, downstream_actions_json, reasoning_json,
                execution_truth_summary_json, metadata_json
            ) VALUES (
                'objective90', 'workspace', $1, 'active', 24,
                3, 4, 0.92, 'unstable',
                $2, $3, $4::jsonb,
                $5::jsonb, $6::jsonb, $7::jsonb,
                $8::jsonb, $9::jsonb
            )
            """,
            str(managed_scope),
            str(governance_decision),
            f"objective90 fresh contradictory governance for {managed_scope}",
            json.dumps({"fresh_runtime_signals": 4}),
            json.dumps({"run_id": run_id, "fresh_contradictory_evidence": True}),
            json.dumps(
                {
                    "autonomy_level_cap": "operator_required",
                    "stewardship_auto_execute_allowed": False,
                }
            ),
            json.dumps({"run_id": run_id, "freshness": "recent"}),
            json.dumps(
                {
                    "execution_count": 3,
                    "recent_executions": [
                        {
                            "execution_id": 1,
                            "published_at": datetime.now(timezone.utc).isoformat(),
                            "signal_types": ["fresh_contradictory_evidence"],
                            "capability_name": "objective90-governance-probe",
                        }
                    ],
                    "deviation_signal_count": 4,
                }
            ),
            json.dumps({"run_id": run_id}),
        )
    finally:
        await conn.close()


def clear_scope_proposal_arbitration_outcomes(*, managed_scope: str) -> None:
    asyncio.run(_clear_scope_proposal_arbitration_outcomes_async(managed_scope=managed_scope))


async def _clear_scope_proposal_arbitration_outcomes_async(*, managed_scope: str) -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE related_zone = $1",
            str(managed_scope),
        )
    finally:
        await conn.close()


class Objective90CrossPolicyConflictResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 90",
            base_url=BASE_URL,
            require_ui_state=True,
        )

    def setUp(self) -> None:
        cleanup_objective90_rows()
        refresh_execution_readiness_artifacts()

    def test_execution_readiness_shapes_proposal_and_surfaces_conflict_candidate(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-readiness-{run_id}"
        proposal_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="rescan_zone",
            related_zone=scope,
            confidence=0.84,
            age_seconds=5,
        )

        proposal_payload = get_workspace_proposal(proposal_id)
        readiness = (
            proposal_payload.get("execution_readiness", {})
            if isinstance(proposal_payload.get("execution_readiness", {}), dict)
            else {}
        )
        self.assertEqual(str(readiness.get("signal_name", "")), "execution-readiness", readiness)
        self.assertEqual(str(readiness.get("managed_scope", "")), scope, readiness)

        metadata = (
            proposal_payload.get("metadata_json", {})
            if isinstance(proposal_payload.get("metadata_json", {}), dict)
            else {}
        )
        breakdown = (
            metadata.get("priority_breakdown", {})
            if isinstance(metadata.get("priority_breakdown", {}), dict)
            else {}
        )
        readiness_breakdown = (
            breakdown.get("execution_readiness", {})
            if isinstance(breakdown.get("execution_readiness", {}), dict)
            else {}
        )
        readiness_effects = (
            readiness_breakdown.get("policy_effects_json", {})
            if isinstance(readiness_breakdown.get("policy_effects_json", {}), dict)
            else {}
        )
        self.assertIn("priority_delta", readiness_effects, readiness_breakdown)
        self.assertLessEqual(
            float(readiness_effects.get("priority_delta", 0.0) or 0.0),
            0.0,
            readiness_breakdown,
        )

        status, conflict_payload = get_json(
            "/workspace/proposals/policy-conflicts",
            {
                "related_zone": scope,
                "limit": 20,
            },
        )
        self.assertEqual(status, 200, conflict_payload)
        rows = conflict_payload.get("conflicts", []) if isinstance(conflict_payload, dict) else []
        matching = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and str(item.get("proposal_type") or "") == "rescan_zone"
            ),
            {},
        )
        candidates = (
            matching.get("candidate_policies_json", [])
            if isinstance(matching.get("candidate_policies_json", []), list)
            else []
        )
        candidate_sources = {
            str(item.get("policy_source") or "")
            for item in candidates
            if isinstance(item, dict)
        }
        self.assertIn("execution_readiness", candidate_sources, matching)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        runtime_features = ui_state.get("runtime_features", []) if isinstance(ui_state, dict) else []
        self.assertIn("execution_readiness_integration", runtime_features, runtime_features)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        ui_readiness = (
            operator_reasoning.get("execution_readiness", {})
            if isinstance(operator_reasoning.get("execution_readiness", {}), dict)
            else {}
        )
        self.assertIn(
            str(ui_readiness.get("policy_outcome", "")),
            {"allow", "degrade", "block"},
            ui_readiness,
        )

    def test_inquiry_conflict_prefers_evidence_gathering_path_over_proposal_learning(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-inquiry-{run_id}"
        proposal_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="capability_workflow_improvement",
            related_zone=scope,
            confidence=0.86,
            age_seconds=10,
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective90",
                    "proposal_id": proposal_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "improvement proposals repeatedly won arbitration for this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        create_resolution_commitment(
            scope=scope,
            run_id=run_id,
            decision_type="require_additional_evidence",
            downstream_effects_json={"autonomy_level": "operator_required"},
        )
        seed_objective90_stewardship_followup(scope=scope, run_id=run_id)

        generated = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-conflict",
        )
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
            ),
            None,
        )
        self.assertIsNotNone(question, questions)

        conflict = (
            question.get("policy_conflict_resolution", {})
            if isinstance(question.get("policy_conflict_resolution", {}), dict)
            else {}
        )
        candidate_paths = (
            question.get("candidate_answer_paths", [])
            if isinstance(question.get("candidate_answer_paths", []), list)
            else []
        )
        self.assertTrue(candidate_paths, question)
        self.assertEqual(str(conflict.get("decision_family", "")), "governed_inquiry_answer_path", conflict)
        self.assertEqual(str(conflict.get("winning_policy_source", "")), "operator_commitment", conflict)
        self.assertEqual(
            str(conflict.get("precedence_rule", "")),
            "operator_commitment_over_proposal_arbitration_review",
            conflict,
        )
        self.assertIn("proposal_arbitration_review", conflict.get("losing_policy_sources", []), conflict)
        self.assertEqual(str(candidate_paths[0].get("path_id", "")), "stabilize_scope_now", candidate_paths)
        self.assertTrue(bool(candidate_paths[0].get("policy_conflict_preferred", False)), candidate_paths[0])

        improvement_path = next(
            (
                item
                for item in candidate_paths
                if isinstance(item, dict)
                and str(item.get("path_id", "")) == "request_stewardship_improvement"
            ),
            None,
        )
        self.assertIsNotNone(improvement_path, candidate_paths)
        self.assertTrue(bool((improvement_path or {}).get("policy_conflict_masked", False)), improvement_path)

        allowed_effects = list(question.get("allowed_answer_effects", []))
        self.assertIn("rescan", allowed_effects)
        self.assertIn("no_action", allowed_effects)
        self.assertNotIn("propose_improvement", allowed_effects)
        self.assertNotIn("tighten_tracking", allowed_effects)

        conflict_rows = list_policy_conflict_profiles(
            managed_scope=scope,
            decision_family="governed_inquiry_answer_path",
        )
        self.assertTrue(conflict_rows, conflict_rows)
        self.assertEqual(str(conflict_rows[0].get("winning_policy_source", "")), "operator_commitment", conflict_rows)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        conflict_resolution = (
            operator_reasoning.get("conflict_resolution", {})
            if isinstance(operator_reasoning.get("conflict_resolution", {}), dict)
            else {}
        )
        items = conflict_resolution.get("items", []) if isinstance(conflict_resolution.get("items", []), list) else []
        self.assertTrue(
            any(str(item.get("decision_family", "")) == "governed_inquiry_answer_path" for item in items),
            conflict_resolution,
        )

    def test_inquiry_decision_conflict_defers_when_operator_commitment_requires_more_evidence(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-inquiry-decision-{run_id}"

        create_resolution_commitment(
            scope=scope,
            run_id=run_id,
            decision_type="require_additional_evidence",
            downstream_effects_json={
                "autonomy_level": "operator_required",
                "suppress_duplicate_inquiry": True,
            },
        )
        seed_objective90_stewardship_followup(scope=scope, run_id=run_id)

        generated = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-decision-conflict",
        )
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        decisions = generated.get("decisions", []) if isinstance(generated, dict) else []

        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "stewardship_persistent_degradation"
                for item in questions
            ),
            questions,
        )

        decision = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "stewardship_persistent_degradation"
            ),
            None,
        )
        self.assertIsNotNone(decision, decisions)
        self.assertEqual(
            str((decision or {}).get("decision_state", "")),
            "deferred_due_to_operator_commitment",
        )

        conflict = (
            decision.get("decision_policy_conflict_resolution", {})
            if isinstance(decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertEqual(str(conflict.get("decision_family", "")), "governed_inquiry_decision_state", conflict)
        self.assertEqual(str(conflict.get("winning_policy_source", "")), "operator_commitment", conflict)
        self.assertIn("trigger_evidence", conflict.get("losing_policy_sources", []), conflict)

        conflict_rows = list_policy_conflict_profiles(
            managed_scope=scope,
            decision_family="governed_inquiry_decision_state",
        )
        self.assertTrue(conflict_rows, conflict_rows)
        self.assertEqual(str(conflict_rows[0].get("winning_policy_source", "")), "operator_commitment", conflict_rows)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        conflict_resolution = (
            operator_reasoning.get("conflict_resolution", {})
            if isinstance(operator_reasoning.get("conflict_resolution", {}), dict)
            else {}
        )
        items = conflict_resolution.get("items", []) if isinstance(conflict_resolution.get("items", []), list) else []
        self.assertTrue(
            any(str(item.get("decision_family", "")) == "governed_inquiry_decision_state" for item in items),
            conflict_resolution,
        )

    def test_inquiry_decision_conflict_holds_recent_answer_cooldown(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-inquiry-cooldown-{run_id}"

        seed_objective90_plan(scope=scope, run_id=run_id)
        seed_objective90_target_confidence_warnings(run_id=run_id, count=5)

        first_generated = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-cooldown",
        )
        first_questions = first_generated.get("questions", []) if isinstance(first_generated, dict) else []
        first_question = first_questions[0] if first_questions else None
        self.assertIsNotNone(first_question, first_questions)
        trigger_type = str((first_question or {}).get("trigger_type", "") or "")
        metadata_json = (
            first_question.get("metadata_json", {})
            if isinstance(first_question.get("metadata_json", {}), dict)
            else {}
        )
        inquiry_policy = (
            metadata_json.get("inquiry_policy", {})
            if isinstance(metadata_json.get("inquiry_policy", {}), dict)
            else {}
        )
        decision_scope = str(inquiry_policy.get("dedupe_key", "") or "")
        self.assertTrue(trigger_type, first_question)
        self.assertTrue(decision_scope, first_question)

        question_id = int((first_question or {}).get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        candidate_paths = (
            first_question.get("candidate_answer_paths", [])
            if isinstance(first_question.get("candidate_answer_paths", []), list)
            else []
        )
        selected_path_id = str((candidate_paths[0] if candidate_paths else {}).get("path_id", "") or "")
        self.assertTrue(selected_path_id, candidate_paths)

        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": selected_path_id,
                "answer_json": {
                    "reason": "objective90 cooldown reopen baseline"
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)

        cooled = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-cooldown",
        )
        cooled_decisions = cooled.get("decisions", []) if isinstance(cooled, dict) else []
        cooled_decision = next(
            (
                item
                for item in cooled_decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == trigger_type
            ),
            None,
        )
        self.assertIsNotNone(cooled_decision, cooled_decisions)
        self.assertEqual(
            str((cooled_decision or {}).get("decision_state", "")),
            "deferred_due_to_cooldown",
        )
        cooled_conflict = (
            cooled_decision.get("decision_policy_conflict_resolution", {})
            if isinstance(cooled_decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertEqual(str(cooled_conflict.get("decision_family", "")), "governed_inquiry_decision_state", cooled_conflict)
        self.assertEqual(str(cooled_conflict.get("winning_policy_source", "")), "recent_inquiry_cooldown", cooled_conflict)
        self.assertIn("trigger_evidence", cooled_conflict.get("losing_policy_sources", []), cooled_conflict)

        conflict_scope = str(cooled_conflict.get("managed_scope", "") or decision_scope)

        conflict_rows = list_policy_conflict_profiles(
            managed_scope=conflict_scope,
            decision_family="governed_inquiry_decision_state",
        )
        self.assertTrue(conflict_rows, conflict_rows)
        matching_row = next(
            (
                row
                for row in conflict_rows
                if str(row.get("proposal_type", "")) == trigger_type
            ),
            None,
        )
        self.assertIsNotNone(matching_row, conflict_rows)
        self.assertEqual(str((matching_row or {}).get("winning_policy_source", "")), "recent_inquiry_cooldown", matching_row)
        self.assertEqual(
            str((matching_row or {}).get("precedence_rule", "")),
            "higher_precedence_policy_won",
            matching_row,
        )

    def test_inquiry_low_evidence_suppression_reopens_on_stronger_fresh_signal(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-inquiry-low-evidence-{run_id}"

        seed_objective90_plan(scope=scope, run_id=run_id)
        seed_objective90_low_confidence_friction(run_id=run_id, count=3)

        first_generated = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-low-evidence",
        )
        first_decisions = first_generated.get("decisions", []) if isinstance(first_generated, dict) else []
        first_decision = next(
            (
                item
                for item in first_decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "repeated_soft_constraint_friction"
            ),
            None,
        )
        self.assertIsNotNone(first_decision, first_decisions)
        self.assertEqual(
            str((first_decision or {}).get("decision_state", "")),
            "suppressed_low_evidence",
        )
        first_conflict = (
            first_decision.get("decision_policy_conflict_resolution", {})
            if isinstance(first_decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertEqual(str(first_conflict.get("winning_policy_source", "")), "inquiry_evidence_floor", first_conflict)
        decision_scope = str(first_conflict.get("managed_scope", "") or scope)
        self.assertTrue(decision_scope, first_decision)

        set_policy_conflict_cooldown(
            managed_scope=decision_scope,
            decision_family="governed_inquiry_decision_state",
            minutes_offset=30,
        )
        seed_objective90_low_confidence_friction(run_id=run_id, count=8)

        reopened = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-low-evidence",
        )
        reopened_decisions = reopened.get("decisions", []) if isinstance(reopened, dict) else []
        reopened_decision = next(
            (
                item
                for item in reopened_decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "repeated_soft_constraint_friction"
            ),
            None,
        )
        self.assertIsNotNone(reopened_decision, reopened_decisions)
        self.assertEqual(
            str((reopened_decision or {}).get("decision_state", "")),
            "optional_for_refinement",
        )
        reopened_conflict = (
            reopened_decision.get("decision_policy_conflict_resolution", {})
            if isinstance(reopened_decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        reopened_reason = (
            reopened_conflict.get("resolution_reason_json", {})
            if isinstance(reopened_conflict.get("resolution_reason_json", {}), dict)
            else {}
        )
        self.assertEqual(str(reopened_conflict.get("winning_policy_source", "")), "trigger_evidence", reopened_conflict)
        self.assertEqual(
            str(reopened_conflict.get("precedence_rule", "")),
            "contradictory_fresh_evidence_reopened",
            reopened_conflict,
        )
        self.assertFalse(bool(reopened_conflict.get("cooldown_active", True)), reopened_conflict)
        self.assertTrue(
            bool(reopened_reason.get("reopened_by_contradictory_fresh_evidence", False)),
            reopened_reason,
        )

    def test_inquiry_autonomy_suppression_reopens_on_required_strength_signal(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-inquiry-autonomy-{run_id}"

        seed_objective90_plan(scope=scope, run_id=run_id)
        seed_objective90_target_confidence_warnings(run_id=run_id, count=3)
        set_bounded_auto_inquiry_state(run_id=run_id, scope=scope)

        first_generated = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-autonomy",
        )
        first_decisions = first_generated.get("decisions", []) if isinstance(first_generated, dict) else []
        first_decision = next(
            (
                item
                for item in first_decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
            ),
            None,
        )
        self.assertIsNotNone(first_decision, first_decisions)
        self.assertEqual(
            str((first_decision or {}).get("decision_state", "")),
            "suppressed_high_confidence_autonomy",
        )
        first_conflict = (
            first_decision.get("decision_policy_conflict_resolution", {})
            if isinstance(first_decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertEqual(str(first_conflict.get("winning_policy_source", "")), "autonomy_boundary", first_conflict)
        decision_scope = str(first_conflict.get("managed_scope", "") or scope)
        self.assertTrue(decision_scope, first_decision)

        set_policy_conflict_cooldown(
            managed_scope=decision_scope,
            decision_family="governed_inquiry_decision_state",
            minutes_offset=30,
        )
        seed_objective90_target_confidence_warnings(run_id=run_id, count=5)

        reopened = generate_inquiry_questions(
            run_id=run_id,
            source="objective90-inquiry-autonomy",
        )
        reopened_decisions = reopened.get("decisions", []) if isinstance(reopened, dict) else []
        reopened_decision = next(
            (
                item
                for item in reopened_decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
            ),
            None,
        )
        self.assertIsNotNone(reopened_decision, reopened_decisions)
        self.assertEqual(
            str((reopened_decision or {}).get("decision_state", "")),
            "required_for_progress",
        )
        reopened_conflict = (
            reopened_decision.get("decision_policy_conflict_resolution", {})
            if isinstance(reopened_decision.get("decision_policy_conflict_resolution", {}), dict)
            else {}
        )
        reopened_reason = (
            reopened_conflict.get("resolution_reason_json", {})
            if isinstance(reopened_conflict.get("resolution_reason_json", {}), dict)
            else {}
        )
        self.assertEqual(str(reopened_conflict.get("winning_policy_source", "")), "trigger_evidence", reopened_conflict)
        self.assertEqual(
            str(reopened_conflict.get("precedence_rule", "")),
            "contradictory_fresh_evidence_reopened",
            reopened_conflict,
        )
        self.assertFalse(bool(reopened_conflict.get("cooldown_active", True)), reopened_conflict)
        self.assertTrue(
            bool(reopened_reason.get("reopened_by_contradictory_fresh_evidence", False)),
            reopened_reason,
        )

    def test_operator_commitment_overrides_preferred_proposal_policy_in_same_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-proposal-{run_id}"
        proposal_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="confirm_target_ready",
            related_zone=scope,
            confidence=0.84,
            age_seconds=15,
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective90",
                    "proposal_id": proposal_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "confirm target repeatedly won arbitration in this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective90-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "operator needs fresh corroboration before acting on this proposal family",
                "recommendation_snapshot_json": {
                    "recommendation": "wait for corroboration",
                    "governance_decision": "request_operator_review",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.94,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "autonomy_level": "operator_required",
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)

        status, proposal_payload = get_json(f"/workspace/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_payload)
        proposal_policy = (
            proposal_payload.get("proposal_policy_convergence", {})
            if isinstance(proposal_payload.get("proposal_policy_convergence", {}), dict)
            else {}
        )
        conflict = (
            proposal_payload.get("policy_conflict_resolution", {})
            if isinstance(proposal_payload.get("policy_conflict_resolution", {}), dict)
            else {}
        )

        self.assertEqual(str(proposal_policy.get("policy_state", "")), "preferred", proposal_policy)
        self.assertEqual(str(conflict.get("conflict_state", "")), "active_conflict", conflict)
        self.assertEqual(str(conflict.get("winning_policy_source", "")), "operator_commitment", conflict)
        self.assertEqual(
            str(conflict.get("precedence_rule", "")),
            "operator_commitment_over_proposal_policy",
            conflict,
        )
        self.assertIn("proposal_policy_convergence", conflict.get("losing_policy_sources", []), conflict)
        effects = conflict.get("policy_effects_json", {}) if isinstance(conflict.get("policy_effects_json", {}), dict) else {}
        self.assertTrue(bool(effects.get("require_operator_confirmation", False)), effects)
        self.assertLessEqual(float(proposal_payload.get("priority_score", 1.0) or 1.0), 0.42, proposal_payload)
        self.assertIn("policy_conflict_resolution", str(proposal_payload.get("priority_reason", "")), proposal_payload)

        status, conflicts_payload = get_json(
            "/workspace/proposals/policy-conflicts",
            {"related_zone": scope, "proposal_type": "confirm_target_ready"},
        )
        self.assertEqual(status, 200, conflicts_payload)
        conflicts = conflicts_payload.get("conflicts", []) if isinstance(conflicts_payload, dict) else []
        self.assertTrue(conflicts, conflicts_payload)
        self.assertEqual(str(conflicts[0].get("winning_policy_source", "")), "operator_commitment", conflicts)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        conflict_resolution = (
            operator_reasoning.get("conflict_resolution", {})
            if isinstance(operator_reasoning.get("conflict_resolution", {}), dict)
            else {}
        )
        conflict_items = (
            conflict_resolution.get("items", [])
            if isinstance(conflict_resolution.get("items", []), list)
            else []
        )
        self.assertIsInstance(conflict_items, list, conflict_resolution)

    def test_scope_local_conflicts_do_not_bleed_into_other_scopes(self) -> None:
        run_id = uuid4().hex[:8]
        scope_a = f"objective90-zone-a-{run_id}"
        scope_b = f"objective90-zone-b-{run_id}"
        seed_stewardship_state(scope=scope_a, run_id=run_id)
        seed_stewardship_state(scope=scope_b, run_id=run_id)
        proposal_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="confirm_target_ready",
            related_zone=scope_a,
            confidence=0.79,
            age_seconds=10,
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective90",
                    "proposal_id": proposal_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "scope a repeatedly won arbitration",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective90-test-operator",
                "managed_scope": scope_b,
                "decision_type": "require_additional_evidence",
                "reason": "only scope b should be held back",
                "recommendation_snapshot_json": {"recommendation": "scope-b hold"},
                "authority_level": "temporary_safety_hold",
                "confidence": 0.92,
                "duration_seconds": 1800,
                "downstream_effects_json": {"autonomy_level": "operator_required"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)

        status, proposal_payload = get_json(f"/workspace/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_payload)
        conflict = (
            proposal_payload.get("policy_conflict_resolution", {})
            if isinstance(proposal_payload.get("policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertNotEqual(str(conflict.get("winning_policy_source", "")), "operator_commitment", conflict)
        self.assertNotEqual(str(conflict.get("conflict_state", "")), "active_conflict", conflict)

        status, conflicts_payload = get_json(
            "/workspace/proposals/policy-conflicts",
            {"related_zone": scope_a, "proposal_type": "confirm_target_ready"},
        )
        self.assertEqual(status, 200, conflicts_payload)
        conflicts = conflicts_payload.get("conflicts", []) if isinstance(conflicts_payload, dict) else []
        self.assertTrue(conflicts, conflicts_payload)
        self.assertNotEqual(str(conflicts[0].get("winning_policy_source", "")), "operator_commitment", conflicts)

    def test_stewardship_conflict_prefers_active_commitment_over_promoting_learned_preference(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-stewardship-{run_id}"
        seed_stewardship_state(scope=scope, run_id=run_id)

        for _ in range(3):
            commitment_id = create_resolution_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="elevate_remediation_priority",
                downstream_effects_json={"strategy_priority_delta": 0.1},
                authority_level="operator_guidance",
            )
            resolve_resolution_commitment(
                commitment_id=commitment_id,
                source="objective90",
                target_status="satisfied",
                run_id=run_id,
            )

        preferences = converge_scope_preferences(scope=scope)
        self.assertTrue(preferences, preferences)

        create_resolution_commitment(
            scope=scope,
            run_id=run_id,
            decision_type="require_additional_evidence",
            downstream_effects_json={
                "stewardship_defer_actions": True,
                "stewardship_mode": "deferred",
                "autonomy_level": "operator_required",
            },
        )

        status, stewardship_payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective90-test",
                "source": "objective90",
                "managed_scope": scope,
                "lookback_hours": 24,
                "max_strategies": 2,
                "max_actions": 2,
                "auto_execute": True,
                "force_degraded": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, stewardship_payload)
        cycle = stewardship_payload.get("cycle", {}) if isinstance(stewardship_payload, dict) else {}
        decision = cycle.get("decision", {}) if isinstance(cycle.get("decision", {}), dict) else {}
        conflict = (
            decision.get("policy_conflict_resolution", {})
            if isinstance(decision.get("policy_conflict_resolution", {}), dict)
            else {}
        )
        verification = (
            cycle.get("verification", {}) if isinstance(cycle.get("verification", {}), dict) else {}
        )

        self.assertEqual(str(conflict.get("decision_family", "")), "stewardship_auto_execution", conflict)
        self.assertEqual(str(conflict.get("winning_policy_source", "")), "operator_commitment", conflict)
        self.assertEqual(str(conflict.get("conflict_state", "")), "active_conflict", conflict)
        self.assertFalse(bool(decision.get("allow_auto_execution", True)), decision)
        self.assertTrue(bool(verification.get("policy_conflict_blocked_auto_execution", False)), verification)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        conflict_resolution = (
            operator_reasoning.get("conflict_resolution", {})
            if isinstance(operator_reasoning.get("conflict_resolution", {}), dict)
            else {}
        )
        items = conflict_resolution.get("items", []) if isinstance(conflict_resolution.get("items", []), list) else []
        self.assertTrue(
            any(str(item.get("decision_family", "")) == "stewardship_auto_execution" for item in items),
            conflict_resolution,
        )

    def test_autonomy_conflict_prefers_active_commitment_over_cautious_learned_preference(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-autonomy-{run_id}"

        previous_autonomy = set_monitoring_autonomy_state(
            autonomy_state={
                "auto_execution_enabled": True,
                "force_manual_approval": False,
                "max_auto_tasks_per_window": 4,
                "max_auto_actions_per_minute": 3,
                "cooldown_between_actions_seconds": 6,
                "low_risk_score_max": 0.24,
            }
        )

        try:
            for _ in range(3):
                commitment_id = create_resolution_commitment(
                    scope=scope,
                    run_id=run_id,
                    decision_type="require_additional_evidence",
                    downstream_effects_json={"autonomy_level": "operator_required"},
                    authority_level="operator_guidance",
                )
                resolve_resolution_commitment(
                    commitment_id=commitment_id,
                    source="objective90",
                    target_status="satisfied",
                    run_id=run_id,
                )

            preferences = converge_scope_preferences(scope=scope)
            self.assertTrue(preferences, preferences)

            create_resolution_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="increase_autonomy_for_scope",
                downstream_effects_json={"autonomy_level": "trusted_auto"},
                authority_level="operator_guidance",
            )

            status, autonomy_payload = post_json(
                "/autonomy/boundaries/recompute",
                {
                    "actor": "objective90-test",
                    "source": "objective90",
                    "scope": scope,
                    "lookback_hours": 24,
                    "min_samples": 1,
                    "apply_recommended_boundaries": False,
                    "evidence_inputs_override": {
                        "sample_count": 5,
                        "success_rate": 0.92,
                        "escalation_rate": 0.02,
                        "interruption_rate": 0.02,
                        "retry_rate": 0.01,
                        "memory_delta_rate": 0.92,
                        "override_rate": 0.01,
                        "replan_rate": 0.01,
                        "environment_stability": 0.95,
                        "development_confidence": 0.9,
                        "constraint_reliability": 0.9,
                        "experiment_confidence": 0.9,
                    },
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, autonomy_payload)
            boundary = autonomy_payload.get("boundary", {}) if isinstance(autonomy_payload, dict) else {}
            reasoning = (
                boundary.get("adaptation_reasoning", {}) if isinstance(boundary.get("adaptation_reasoning", {}), dict) else {}
            )
            conflict = (
                reasoning.get("policy_conflict_resolution", {})
                if isinstance(reasoning.get("policy_conflict_resolution", {}), dict)
                else {}
            )
            candidates = (
                conflict.get("candidate_policies_json", [])
                if isinstance(conflict.get("candidate_policies_json", []), list)
                else []
            )

            self.assertEqual(str(conflict.get("decision_family", "")), "autonomy_boundary", conflict)
            self.assertTrue(
                any(
                    isinstance(item, dict)
                    and str(item.get("policy_source", "")) == "operator_commitment"
                    and str(item.get("posture", "")) in {"promote", "advisory"}
                    for item in candidates
                ),
                conflict,
            )
            self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)
            self.assertTrue(bool(reasoning.get("policy_conflict_resolution_applied", False)), reasoning)
        finally:
            set_monitoring_autonomy_state(autonomy_state=previous_autonomy)
        scope = f"objective90-stewardship-reopen-{run_id}"
        seed_stewardship_state(scope=scope, run_id=run_id)

        for _ in range(3):
            commitment_id = create_resolution_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="require_additional_evidence",
                downstream_effects_json={"autonomy_level": "operator_required"},
                authority_level="operator_guidance",
            )
            resolve_resolution_commitment(
                commitment_id=commitment_id,
                source="objective90",
                target_status="satisfied",
                run_id=run_id,
            )

        preferences = converge_scope_preferences(scope=scope)
        self.assertTrue(preferences, preferences)

        status, autonomy_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective90-test",
                "source": "objective90",
                "scope": scope,
                "lookback_hours": 24,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "evidence_inputs_override": {
                    "sample_count": 6,
                    "success_rate": 0.95,
                    "escalation_rate": 0.01,
                    "interruption_rate": 0.01,
                    "retry_rate": 0.01,
                    "memory_delta_rate": 0.94,
                    "override_rate": 0.01,
                    "replan_rate": 0.01,
                    "environment_stability": 0.96,
                    "development_confidence": 0.91,
                    "constraint_reliability": 0.91,
                    "experiment_confidence": 0.9,
                },
                "metadata_json": {"run_id": run_id, "phase": "baseline"},
            },
        )
        self.assertEqual(status, 200, autonomy_payload)

        status, first_stewardship_payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective90-test",
                "source": "objective90",
                "managed_scope": scope,
                "lookback_hours": 24,
                "max_strategies": 2,
                "max_actions": 2,
                "auto_execute": True,
                "force_degraded": True,
                "metadata_json": {"run_id": run_id, "phase": "baseline"},
            },
        )
        self.assertEqual(status, 200, first_stewardship_payload)
        first_cycle = (
            first_stewardship_payload.get("cycle", {})
            if isinstance(first_stewardship_payload, dict)
            else {}
        )
        first_decision = (
            first_cycle.get("decision", {})
            if isinstance(first_cycle.get("decision", {}), dict)
            else {}
        )
        first_conflict = (
            first_decision.get("policy_conflict_resolution", {})
            if isinstance(first_decision.get("policy_conflict_resolution", {}), dict)
            else {}
        )
        self.assertEqual(
            str(first_conflict.get("winning_policy_source", "")),
            "autonomy_boundary",
            first_conflict,
        )
        self.assertEqual(str(first_conflict.get("conflict_state", "")), "active_conflict", first_conflict)

        set_policy_conflict_cooldown(
            managed_scope=scope,
            decision_family="stewardship_auto_execution",
            minutes_offset=30,
        )
        seed_execution_truth_governance(
            managed_scope=scope,
            run_id=run_id,
            governance_decision="lower_autonomy_boundary",
        )

        status, second_stewardship_payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective90-test",
                "source": "objective90",
                "managed_scope": scope,
                "lookback_hours": 24,
                "max_strategies": 2,
                "max_actions": 2,
                "auto_execute": True,
                "force_degraded": True,
                "metadata_json": {"run_id": run_id, "phase": "reopen"},
            },
        )
        self.assertEqual(status, 200, second_stewardship_payload)
        second_cycle = (
            second_stewardship_payload.get("cycle", {})
            if isinstance(second_stewardship_payload, dict)
            else {}
        )
        second_decision = (
            second_cycle.get("decision", {})
            if isinstance(second_cycle.get("decision", {}), dict)
            else {}
        )
        second_conflict = (
            second_decision.get("policy_conflict_resolution", {})
            if isinstance(second_decision.get("policy_conflict_resolution", {}), dict)
            else {}
        )
        second_verification = (
            second_cycle.get("verification", {})
            if isinstance(second_cycle.get("verification", {}), dict)
            else {}
        )
        second_reason = (
            second_conflict.get("resolution_reason_json", {})
            if isinstance(second_conflict.get("resolution_reason_json", {}), dict)
            else {}
        )

        self.assertEqual(
            str(second_conflict.get("winning_policy_source", "")),
            "execution_truth_governance",
            second_conflict,
        )
        self.assertEqual(
            str(second_conflict.get("precedence_rule", "")),
            "contradictory_fresh_evidence_reopened",
            second_conflict,
        )
        self.assertEqual(str(second_conflict.get("conflict_state", "")), "active_conflict", second_conflict)
        self.assertFalse(bool(second_conflict.get("cooldown_active", True)), second_conflict)
        self.assertTrue(
            bool(second_reason.get("reopened_by_contradictory_fresh_evidence", False)),
            second_reason,
        )
        self.assertFalse(bool(second_decision.get("allow_auto_execution", True)), second_decision)
        self.assertTrue(
            bool(second_verification.get("policy_conflict_blocked_auto_execution", False)),
            second_verification,
        )

    def test_autonomy_cooldown_expiry_releases_previous_hold(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective90-autonomy-expiry-{run_id}"
        previous_autonomy = set_monitoring_autonomy_state(
            autonomy_state={
                "auto_execution_enabled": True,
                "force_manual_approval": False,
                "max_auto_tasks_per_window": 8,
                "max_auto_actions_per_minute": 6,
                "cooldown_between_actions_seconds": 3,
                "low_risk_score_max": 0.45,
            }
        )

        try:
            for _ in range(3):
                commitment_id = create_resolution_commitment(
                    scope=scope,
                    run_id=run_id,
                    decision_type="require_additional_evidence",
                    downstream_effects_json={"autonomy_level": "operator_required"},
                    authority_level="operator_guidance",
                )
                resolve_resolution_commitment(
                    commitment_id=commitment_id,
                    source="objective90",
                    target_status="satisfied",
                    run_id=run_id,
                )

            preferences = converge_scope_preferences(scope=scope)
            self.assertTrue(preferences, preferences)

            proposal_id = seed_workspace_proposal(
                run_id=run_id,
                proposal_type="rescan_zone",
                related_zone=scope,
                confidence=0.84,
                age_seconds=5,
            )
            for _ in range(4):
                status, outcome = post_json(
                    "/workspace/proposals/arbitration-outcomes",
                    {
                        "actor": "tod",
                        "source": "objective90",
                        "proposal_id": proposal_id,
                        "arbitration_decision": "won",
                        "arbitration_posture": "merge",
                        "trust_chain_status": "verified",
                        "downstream_execution_outcome": "accepted",
                        "reason": "rescan proposals repeatedly won arbitration for this scope",
                        "metadata_json": {"run_id": run_id},
                    },
                )
                self.assertEqual(status, 200, outcome)

            status, first_autonomy_payload = post_json(
                "/autonomy/boundaries/recompute",
                {
                    "actor": "objective90-test",
                    "source": "objective90",
                    "scope": scope,
                    "lookback_hours": 24,
                    "min_samples": 1,
                    "apply_recommended_boundaries": False,
                    "evidence_inputs_override": {
                        "sample_count": 6,
                        "success_rate": 0.95,
                        "escalation_rate": 0.01,
                        "interruption_rate": 0.01,
                        "retry_rate": 0.01,
                        "memory_delta_rate": 0.95,
                        "override_rate": 0.01,
                        "replan_rate": 0.01,
                        "environment_stability": 0.97,
                        "development_confidence": 0.92,
                        "constraint_reliability": 0.92,
                        "experiment_confidence": 0.91,
                    },
                    "metadata_json": {"run_id": run_id, "phase": "baseline"},
                },
            )
            self.assertEqual(status, 200, first_autonomy_payload)
            first_boundary = (
                first_autonomy_payload.get("boundary", {})
                if isinstance(first_autonomy_payload, dict)
                else {}
            )
            first_reasoning = (
                first_boundary.get("adaptation_reasoning", {})
                if isinstance(first_boundary.get("adaptation_reasoning", {}), dict)
                else {}
            )
            first_conflict = (
                first_reasoning.get("policy_conflict_resolution", {})
                if isinstance(first_reasoning.get("policy_conflict_resolution", {}), dict)
                else {}
            )
            self.assertEqual(
                str(first_conflict.get("winning_policy_source", "")),
                "proposal_arbitration_review",
                first_conflict,
            )

            set_policy_conflict_cooldown(
                managed_scope=scope,
                decision_family="autonomy_boundary",
                minutes_offset=-5,
            )
            clear_scope_proposal_arbitration_outcomes(managed_scope=scope)

            status, second_autonomy_payload = post_json(
                "/autonomy/boundaries/recompute",
                {
                    "actor": "objective90-test",
                    "source": "objective90",
                    "scope": scope,
                    "lookback_hours": 24,
                    "min_samples": 1,
                    "apply_recommended_boundaries": False,
                    "evidence_inputs_override": {
                        "sample_count": 6,
                        "success_rate": 0.95,
                        "escalation_rate": 0.01,
                        "interruption_rate": 0.01,
                        "retry_rate": 0.01,
                        "memory_delta_rate": 0.95,
                        "override_rate": 0.01,
                        "replan_rate": 0.01,
                        "environment_stability": 0.97,
                        "development_confidence": 0.92,
                        "constraint_reliability": 0.92,
                        "experiment_confidence": 0.91,
                    },
                    "metadata_json": {"run_id": run_id, "phase": "expired-cooldown"},
                },
            )
            self.assertEqual(status, 200, second_autonomy_payload)
            second_boundary = (
                second_autonomy_payload.get("boundary", {})
                if isinstance(second_autonomy_payload, dict)
                else {}
            )
            second_reasoning = (
                second_boundary.get("adaptation_reasoning", {})
                if isinstance(second_boundary.get("adaptation_reasoning", {}), dict)
                else {}
            )
            second_conflict = (
                second_reasoning.get("policy_conflict_resolution", {})
                if isinstance(second_reasoning.get("policy_conflict_resolution", {}), dict)
                else {}
            )
            second_reason = (
                second_conflict.get("resolution_reason_json", {})
                if isinstance(second_conflict.get("resolution_reason_json", {}), dict)
                else {}
            )

            self.assertEqual(
                str(second_conflict.get("winning_policy_source", "")),
                "learned_preference",
                second_conflict,
            )
            self.assertNotEqual(str(second_conflict.get("precedence_rule", "")), "cooldown_hold_down", second_conflict)
            self.assertNotEqual(str(second_conflict.get("conflict_state", "")), "cooldown_held", second_conflict)
            self.assertFalse(bool(second_conflict.get("cooldown_active", True)), second_conflict)
            self.assertFalse(
                bool(second_reason.get("reopened_by_contradictory_fresh_evidence", False)),
                second_reason,
            )
            self.assertEqual(str(second_boundary.get("current_level", "")), "operator_required", second_boundary)
        finally:
            set_monitoring_autonomy_state(autonomy_state=previous_autonomy)


if __name__ == "__main__":
    unittest.main()