import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tests.integration.operator_resolution_test_utils import cleanup_objective85_rows
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


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
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


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
        return exc.code, json.loads(body) if body else {}


class Objective85OperatorGovernedResolutionCommitmentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 85",
            base_url=BASE_URL,
            require_ui_state=True,
            require_governance=True,
        )
        cleanup_objective85_rows()

    def setUp(self) -> None:
        cleanup_objective85_rows()

    def tearDown(self) -> None:
        cleanup_objective85_rows()

    def _register_workspace_scan(self) -> None:
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
        self.assertEqual(status, 200, payload)

    def _create_stale_observation(self, *, zone: str, run_id: str) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective85 reasoning stale scan {run_id}",
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
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

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
            self.assertEqual(status, 200, updated)

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
                            "label": f"obj85-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_stewardship_prereqs(self, *, scope: str, run_id: str, source: str) -> None:
        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": source,
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective85-test",
                "source": source,
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, goals)

    def _run_stewardship_cycle(self, *, scope: str, run_id: str, source: str, auto_execute: bool = False) -> dict:
        status, payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective85-test",
                "source": source,
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": bool(auto_execute),
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective85-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        return payload

    def _register_capability(self, *, capability_name: str) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 85 governance probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_execution_truth(self, *, scope: str, run_id: str) -> None:
        capability_name = f"objective85_truth_probe_{run_id}"
        self._register_capability(capability_name=capability_name)
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective85 governance probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for operator commitment visibility",
                "metadata_json": {
                    "capability": capability_name,
                    "managed_scope": scope,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0)

        status, accepted = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, accepted)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "execution truth recorded",
                "runtime_outcome": "recovered",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
                "execution_truth": {
                    "contract": "execution_truth_v1",
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": 1710,
                    "duration_delta_ratio": round((1710 - 900) / 900.0, 6),
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.95,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, done)

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective85-test",
                "source": "objective85-focused",
                "managed_scope": scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)

    def _generate_questions(self, *, run_id: str, source: str) -> dict:
        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective85-test",
                "source": source,
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        return generated

    def test_create_list_and_get_resolution_commitment(self) -> None:
        scope = f"objective85-zone-{uuid4().hex[:8]}"
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "camera evidence is still stale",
                "recommendation_snapshot_json": {
                    "recommendation": "defer autonomous maintenance",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.92,
                "expires_at": expires_at,
                "provenance_json": {"source": "objective85-focused"},
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                    "maintenance_mode": "deferred",
                },
                "metadata_json": {"run_id": scope},
            },
        )
        self.assertEqual(status, 200, created)
        self.assertFalse(bool(created.get("duplicate_suppressed", False)), created)

        commitment = created.get("commitment", {}) if isinstance(created, dict) else {}
        commitment_id = int(commitment.get("commitment_id", 0) or 0)
        self.assertGreater(commitment_id, 0, created)
        self.assertEqual(str(commitment.get("managed_scope", "")), scope)
        self.assertEqual(str(commitment.get("decision_type", "")), "require_additional_evidence")
        self.assertEqual(str(commitment.get("status", "")), "active")
        self.assertTrue(bool(commitment.get("active", False)), commitment)

        status, listed = get_json(
            "/operator/resolution-commitments",
            {"managed_scope": scope, "active_only": "true", "limit": 20},
        )
        self.assertEqual(status, 200, listed)
        rows = listed.get("commitments", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("commitment_id", 0) or 0) == commitment_id for item in rows if isinstance(item, dict)), listed)

        status, fetched = get_json(f"/operator/resolution-commitments/{commitment_id}")
        self.assertEqual(status, 200, fetched)
        fetched_commitment = fetched.get("commitment", {}) if isinstance(fetched, dict) else {}
        self.assertEqual(int(fetched_commitment.get("commitment_id", 0) or 0), commitment_id)
        self.assertEqual(str(fetched_commitment.get("authority_level", "")), "temporary_safety_hold")

    def test_mim_ui_state_exposes_active_resolution_commitment_for_reasoning_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-ui-{run_id}"
        source = "objective85-operator-resolution-commitments"

        self._seed_stewardship_prereqs(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        generated = self._generate_questions(run_id=run_id, source=source)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertTrue(questions, generated)
        self._seed_execution_truth(scope=scope, run_id=run_id)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "wait for a fresh camera corroboration",
                "recommendation_snapshot_json": {
                    "recommendation": "increase visibility",
                    "governance_decision": "lower_autonomy_boundary",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.91,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "autonomy_level": "operator_required",
                    "suppress_duplicate_inquiry": True,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective85-test",
                "source": source,
                "managed_scope": scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
        self.assertEqual(str(reasoning.get("governance", {}).get("managed_scope", "")), scope, reasoning)
        commitment = reasoning.get("resolution_commitment", {}) if isinstance(reasoning.get("resolution_commitment", {}), dict) else {}
        self.assertEqual(str(commitment.get("managed_scope", "")), scope, commitment)
        self.assertEqual(str(commitment.get("decision_type", "")), "require_additional_evidence", commitment)
        self.assertTrue(bool(commitment.get("active", False)), commitment)
        recommendation = reasoning.get("current_recommendation", {}) if isinstance(reasoning.get("current_recommendation", {}), dict) else {}
        self.assertEqual(str(recommendation.get("source", "")), "governance", recommendation)
        runtime_features = state.get("runtime_features", []) if isinstance(state, dict) else []
        self.assertIn("operator_resolution_commitments", runtime_features, state)
        context = state.get("conversation_context", {}) if isinstance(state, dict) else {}
        self.assertIn("operator_resolution_summary", context, context)

    def test_active_commitment_lowers_autonomy_for_matching_scope(self) -> None:
        scope = f"objective85-autonomy-{uuid4().hex[:8]}"
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "lower_autonomy_for_scope",
                "reason": "manual verification required until drift stabilizes",
                "recommendation_snapshot_json": {"recommendation": "lower autonomy"},
                "authority_level": "operator_required",
                "confidence": 0.95,
                "duration_seconds": 1800,
                "downstream_effects_json": {"autonomy_level": "operator_required"},
                "metadata_json": {"test_case": "autonomy-propagation"},
            },
        )
        self.assertEqual(status, 200, created)

        status, recomputed = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective85-test",
                "source": "objective85-focused",
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
                "metadata_json": {"test_case": "objective85-autonomy-propagation"},
            },
        )
        self.assertEqual(status, 200, recomputed)
        boundary = recomputed.get("boundary", {}) if isinstance(recomputed, dict) else {}
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)
        reasoning = boundary.get("adaptation_reasoning", {}) if isinstance(boundary.get("adaptation_reasoning", {}), dict) else {}
        self.assertTrue(bool(reasoning.get("operator_resolution_commitment_applied", False)), reasoning)
        commitment = reasoning.get("operator_resolution_commitment", {}) if isinstance(reasoning.get("operator_resolution_commitment", {}), dict) else {}
        self.assertEqual(str(commitment.get("managed_scope", "")), scope, commitment)
        self.assertEqual(str(commitment.get("decision_type", "")), "lower_autonomy_for_scope", commitment)

    def test_active_commitment_suppresses_duplicate_inquiry_for_matching_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-inquiry-{run_id}"
        source = "objective85-inquiry-suppression"

        self._seed_stewardship_prereqs(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "hold duplicate inquiry until more evidence arrives",
                "recommendation_snapshot_json": {
                    "recommendation": "keep monitoring",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.93,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                },
                "metadata_json": {"run_id": run_id, "test_case": "inquiry-suppression"},
            },
        )
        self.assertEqual(status, 200, created)

        generated = self._generate_questions(run_id=run_id, source=source)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "stewardship_persistent_degradation"
                for item in questions
            ),
            questions,
        )

        decisions = generated.get("decisions", []) if isinstance(generated, dict) else []
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
        self.assertEqual(
            str((decision or {}).get("suppression_reason", "")),
            "active_operator_resolution_commitment",
        )
        self.assertTrue(bool((decision or {}).get("duplicate_suppressed", False)), decision)
        commitment = (
            (decision or {}).get("operator_resolution_commitment", {})
            if isinstance((decision or {}).get("operator_resolution_commitment", {}), dict)
            else {}
        )
        self.assertEqual(str(commitment.get("managed_scope", "")), scope, commitment)
        self.assertEqual(str(commitment.get("decision_type", "")), "require_additional_evidence", commitment)

    def test_execution_truth_inquiry_uses_scope_based_commitment_suppression(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-execution-truth-{run_id}"
        source = "objective85-execution-truth-suppression"

        self._seed_execution_truth(scope=scope, run_id=run_id)
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "hold follow-up runtime inquiry until scoped evidence accumulates",
                "recommendation_snapshot_json": {
                    "recommendation": "monitor runtime drift",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.94,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                },
                "metadata_json": {"run_id": run_id, "test_case": "execution-truth-suppression"},
            },
        )
        self.assertEqual(status, 200, created)

        generated = self._generate_questions(run_id=run_id, source=source)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "execution_truth_runtime_mismatch"
                for item in questions
            ),
            questions,
        )

        decisions = generated.get("decisions", []) if isinstance(generated, dict) else []
        decision = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "execution_truth_runtime_mismatch"
            ),
            None,
        )
        self.assertIsNotNone(decision, decisions)
        self.assertEqual(str((decision or {}).get("decision_state", "")), "deferred_due_to_operator_commitment")
        commitment = (
            (decision or {}).get("operator_resolution_commitment", {})
            if isinstance((decision or {}).get("operator_resolution_commitment", {}), dict)
            else {}
        )
        self.assertEqual(str(commitment.get("managed_scope", "")), scope)

    def test_active_commitment_shapes_strategy_scoring_for_matching_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-strategy-{run_id}"
        source = "objective85-strategy-shaping"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "prioritize stabilization until the scope is corroborated",
                "recommendation_snapshot_json": {"recommendation": "stabilize before expansion"},
                "authority_level": "temporary_safety_hold",
                "confidence": 0.9,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "strategy_priority_mode": "prefer_stabilization",
                    "strategy_priority_delta": 0.05,
                },
                "metadata_json": {"run_id": run_id, "test_case": "strategy-shaping"},
            },
        )
        self.assertEqual(status, 200, created)

        status, built = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective85-test",
                "source": source,
                "lookback_hours": 48,
                "max_items_per_domain": 50,
                "max_goals": 4,
                "min_context_confidence": 0.0,
                "min_domains_required": 1,
                "min_cross_domain_links": 0,
                "generate_horizon_plans": False,
                "generate_improvement_proposals": False,
                "generate_maintenance_cycles": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, built)
        goals = built.get("goals", []) if isinstance(built, dict) else []
        self.assertTrue(goals, built)
        influenced = [
            item
            for item in goals
            if isinstance(item, dict)
            and float((item.get("ranking_factors", {}) if isinstance(item.get("ranking_factors", {}), dict) else {}).get("operator_resolution_strategy_weight", 0.0) or 0.0) > 0.0
        ]
        self.assertTrue(influenced, goals)
        top = influenced[0]
        reasoning = top.get("reasoning", {}) if isinstance(top.get("reasoning", {}), dict) else {}
        commitment = reasoning.get("operator_resolution_commitment", {}) if isinstance(reasoning.get("operator_resolution_commitment", {}), dict) else {}
        self.assertEqual(str(commitment.get("managed_scope", "")), scope)
        self.assertEqual(str(commitment.get("decision_type", "")), "require_additional_evidence")

    def test_active_commitment_defers_maintenance_auto_execution_for_matching_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-maintenance-{run_id}"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "defer_action",
                "reason": "wait for a fresh confirmation pass before corrective execution",
                "recommendation_snapshot_json": {"recommendation": "defer maintenance execution"},
                "authority_level": "governance_override",
                "confidence": 0.92,
                "duration_seconds": 1800,
                "downstream_effects_json": {"maintenance_mode": "deferred"},
                "metadata_json": {"run_id": run_id, "test_case": "maintenance-shaping"},
            },
        )
        self.assertEqual(status, 200, created)

        status, payload = post_json(
            "/maintenance/cycle",
            {
                "actor": "objective85-test",
                "source": "objective85-maintenance-shaping",
                "stale_after_seconds": 300,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        run = payload.get("run", {}) if isinstance(payload, dict) else {}
        metadata = run.get("metadata_json", {}) if isinstance(run.get("metadata_json", {}), dict) else {}
        outcomes = run.get("maintenance_outcomes", {}) if isinstance(run.get("maintenance_outcomes", {}), dict) else {}
        self.assertFalse(bool(metadata.get("auto_execute", True)), metadata)
        self.assertTrue(bool(metadata.get("requested_auto_execute", False)), metadata)
        self.assertTrue(bool(outcomes.get("operator_resolution_blocked_auto_execution", False)), outcomes)
        commitment = outcomes.get("operator_resolution_commitment", {}) if isinstance(outcomes.get("operator_resolution_commitment", {}), dict) else {}
        self.assertEqual(str(commitment.get("managed_scope", "")), scope)
        self.assertEqual(int(outcomes.get("actions_executed", 0) or 0), 0)

    def test_active_commitment_defers_stewardship_auto_execution_for_matching_scope(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective85-stewardship-{run_id}"
        source = "objective85-stewardship-shaping"

        self._seed_stewardship_prereqs(scope=scope, run_id=run_id, source=source)

        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective85-test-operator",
                "managed_scope": scope,
                "decision_type": "defer_action",
                "reason": "hold corrective stewardship actions until operator review completes",
                "recommendation_snapshot_json": {"recommendation": "defer stewardship actions"},
                "authority_level": "governance_override",
                "confidence": 0.93,
                "duration_seconds": 1800,
                "downstream_effects_json": {
                    "stewardship_defer_actions": True,
                    "maintenance_mode": "deferred",
                },
                "metadata_json": {"run_id": run_id, "test_case": "stewardship-shaping"},
            },
        )
        self.assertEqual(status, 200, created)

        payload = self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source, auto_execute=True)
        cycle = payload.get("cycle", {}) if isinstance(payload, dict) else {}
        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        decision = cycle.get("decision", {}) if isinstance(cycle.get("decision", {}), dict) else {}
        self.assertFalse(bool(summary.get("allow_auto_execution", True)), summary)
        self.assertTrue(bool(summary.get("operator_resolution_blocked_auto_execution", False)), summary)
        commitment = decision.get("operator_resolution_commitment", {}) if isinstance(decision.get("operator_resolution_commitment", {}), dict) else {}
        self.assertEqual(str(commitment.get("managed_scope", "")), scope)
        selected_actions = cycle.get("selected_actions", []) if isinstance(cycle.get("selected_actions", []), list) else []
        applied = next(
            (
                item
                for item in selected_actions
                if isinstance(item, dict)
                and str(item.get("action_type", "")) == "operator_resolution_commitment_applied"
            ),
            None,
        )
        self.assertIsNotNone(applied, selected_actions)
        self.assertTrue(bool((applied or {}).get("auto_execute_blocked", False)), applied)

    def test_duplicate_is_suppressed_and_conflicting_commitment_supersedes_prior_active_commitment(self) -> None:
        scope = f"objective85-zone-{uuid4().hex[:8]}"
        base_payload = {
            "actor": "objective85-test-operator",
            "managed_scope": scope,
            "decision_type": "defer_action",
            "reason": "wait for a fresh camera pass",
            "recommendation_snapshot_json": {"recommendation": "defer autonomous maintenance"},
            "commitment_family": "action_timing",
            "authority_level": "governance_override",
            "confidence": 0.88,
            "duration_seconds": 1800,
            "provenance_json": {"source": "objective85-focused"},
            "downstream_effects_json": {"maintenance_mode": "deferred"},
            "metadata_json": {"test_case": "duplicate-and-supersede"},
        }

        status, first = post_json("/operator/resolution-commitments", base_payload)
        self.assertEqual(status, 200, first)
        first_commitment = first.get("commitment", {}) if isinstance(first, dict) else {}
        first_id = int(first_commitment.get("commitment_id", 0) or 0)
        self.assertGreater(first_id, 0)

        status, duplicate = post_json("/operator/resolution-commitments", base_payload)
        self.assertEqual(status, 200, duplicate)
        self.assertTrue(bool(duplicate.get("duplicate_suppressed", False)), duplicate)
        duplicate_commitment = duplicate.get("commitment", {}) if isinstance(duplicate, dict) else {}
        self.assertEqual(int(duplicate_commitment.get("commitment_id", 0) or 0), first_id)

        status, conflicting = post_json(
            "/operator/resolution-commitments",
            {
                **base_payload,
                "decision_type": "lower_autonomy_for_scope",
                "reason": "operator wants manual verification until recovery stabilizes",
                "commitment_family": "action_timing",
                "downstream_effects_json": {"autonomy_level": "operator_required"},
            },
        )
        self.assertEqual(status, 200, conflicting)
        self.assertFalse(bool(conflicting.get("duplicate_suppressed", False)), conflicting)
        conflicting_commitment = conflicting.get("commitment", {}) if isinstance(conflicting, dict) else {}
        conflicting_id = int(conflicting_commitment.get("commitment_id", 0) or 0)
        self.assertGreater(conflicting_id, first_id)
        self.assertIn(first_id, list(conflicting.get("superseded_commitment_ids", [])))

        status, previous = get_json(f"/operator/resolution-commitments/{first_id}")
        self.assertEqual(status, 200, previous)
        previous_commitment = previous.get("commitment", {}) if isinstance(previous, dict) else {}
        self.assertEqual(str(previous_commitment.get("status", "")), "superseded")
        self.assertEqual(int(previous_commitment.get("superseded_by_commitment_id", 0) or 0), conflicting_id)

        status, active = get_json(
            "/operator/resolution-commitments",
            {"managed_scope": scope, "active_only": "true", "limit": 20},
        )
        self.assertEqual(status, 200, active)
        active_rows = active.get("commitments", []) if isinstance(active, dict) else []
        active_ids = [int(item.get("commitment_id", 0) or 0) for item in active_rows if isinstance(item, dict)]
        self.assertIn(conflicting_id, active_ids)
        self.assertNotIn(first_id, active_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)