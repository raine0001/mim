import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timezone
from uuid import uuid4

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


class Objective81ExecutionTruthGovernanceLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 81",
            base_url=BASE_URL,
            require_governance=True,
        )

    def _register_capability(self, *, capability_name: str) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 81 execution-truth governance probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

    def _seed_execution_truth(
        self,
        *,
        scope: str,
        run_id: str,
        suffix: str,
        truth_confidence: float,
        retry_count: int,
        fallback_used: bool,
        simulation_match_status: str,
        environment_shift_detected: bool,
        actual_duration_ms: int,
    ) -> int:
        capability_name = f"objective81_truth_probe_{run_id}_{suffix}"
        self._register_capability(capability_name=capability_name)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective81 governance probe {run_id} {suffix}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for governance",
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

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "correlation_json": {"managed_scope": scope, "target_scope": scope},
                "feedback_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

        status, payload = post_json(
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
                    "actual_duration_ms": actual_duration_ms,
                    "duration_delta_ratio": round((actual_duration_ms - 900) / 900.0, 6),
                    "retry_count": retry_count,
                    "fallback_used": fallback_used,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": environment_shift_detected,
                    "simulation_match_status": simulation_match_status,
                    "truth_confidence": truth_confidence,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, payload)
        return execution_id

    def _run_stewardship_cycle(self, *, scope: str, run_id: str, auto_execute: bool) -> dict:
        status, payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": auto_execute,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective81-missing-{run_id}"],
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

    def test_objective81_governance_loop_changes_state_and_downstream_behavior(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective81-governance-{run_id}"

        for suffix in ["a", "b", "c"]:
            self._seed_execution_truth(
                scope=scope,
                run_id=run_id,
                suffix=suffix,
                truth_confidence=0.95,
                retry_count=2,
                fallback_used=True,
                simulation_match_status="mismatch",
                environment_shift_detected=True,
                actual_duration_ms=1710,
            )

        first_cycle = self._run_stewardship_cycle(
            scope=scope,
            run_id=run_id,
            auto_execute=False,
        )
        second_cycle = self._run_stewardship_cycle(
            scope=scope,
            run_id=run_id,
            auto_execute=False,
        )
        self.assertEqual(
            int(second_cycle.get("summary", {}).get("execution_truth_signal_count", 0) or 0),
            int(first_cycle.get("summary", {}).get("execution_truth_signal_count", 0) or 0),
        )

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "managed_scope": scope,
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)
        governance = governance_payload.get("governance", {}) if isinstance(governance_payload, dict) else {}
        governance_id = int(governance.get("governance_id", 0) or 0)
        self.assertGreater(governance_id, 0, governance)
        self.assertIn(
            str(governance.get("governance_decision", "")),
            {"lower_autonomy_boundary", "escalate_to_operator"},
            governance,
        )
        self.assertGreaterEqual(int(governance.get("signal_count", 0) or 0), 5, governance)
        self.assertGreaterEqual(
            int(governance.get("trigger_counts", {}).get("correlated_stewardship_cycles", 0) or 0),
            2,
            governance,
        )

        status, governance_list_payload = get_json(
            "/execution-truth/governance",
            {"managed_scope": scope, "limit": 10},
        )
        self.assertEqual(status, 200, governance_list_payload)
        governance_rows = governance_list_payload.get("governance", []) if isinstance(governance_list_payload, dict) else []
        self.assertTrue(
            any(
                isinstance(item, dict)
                and int(item.get("governance_id", 0) or 0) == governance_id
                for item in governance_rows
            ),
            governance_rows,
        )

        status, governance_detail = get_json(f"/execution-truth/governance/{governance_id}")
        self.assertEqual(status, 200, governance_detail)
        detail_governance = governance_detail.get("governance", {}) if isinstance(governance_detail, dict) else {}
        self.assertEqual(int(detail_governance.get("governance_id", 0) or 0), governance_id)

        status, goals_payload = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "lookback_hours": 168,
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
        self.assertEqual(status, 200, goals_payload)
        goals = goals_payload.get("goals", []) if isinstance(goals_payload, dict) else []
        self.assertTrue(goals, goals_payload)
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(
                    (item.get("ranking_factors", {}) if isinstance(item.get("ranking_factors", {}), dict) else {}).get(
                        "execution_truth_governance_decision", ""
                    )
                )
                == str(governance.get("governance_decision", ""))
                for item in goals
            ),
            goals,
        )

        status, backlog_payload = post_json(
            "/improvement/backlog/refresh",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "lookback_hours": 168,
                "min_occurrence_count": 2,
                "max_items": 50,
                "auto_experiment_limit": 3,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, backlog_payload)
        backlog = backlog_payload.get("items", []) if isinstance(backlog_payload, dict) else []
        scoped_backlog = [
            item
            for item in backlog
            if isinstance(item, dict)
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
        ]
        self.assertTrue(scoped_backlog, backlog)
        self.assertTrue(
            any(
                isinstance(item.get("reasoning", {}), dict)
                and str(
                    (item.get("reasoning", {}).get("execution_truth_governance", {})
                    if isinstance(item.get("reasoning", {}).get("execution_truth_governance", {}), dict)
                    else {}).get("governance_decision", "")
                )
                == str(governance.get("governance_decision", ""))
                for item in scoped_backlog
            ),
            scoped_backlog,
        )

        status, boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "scope": scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "success_rate": 0.88,
                    "escalation_rate": 0.08,
                    "retry_rate": 0.1,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.72,
                    "sample_count": 18,
                    "override_rate": 0.02,
                    "replan_rate": 0.08,
                    "environment_stability": 0.76,
                    "development_confidence": 0.78,
                    "constraint_reliability": 0.84,
                    "experiment_confidence": 0.74,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, boundary_payload)
        boundary = boundary_payload.get("boundary", {}) if isinstance(boundary_payload, dict) else {}
        adaptation_reasoning = (
            boundary.get("adaptation_reasoning", {})
            if isinstance(boundary.get("adaptation_reasoning", {}), dict)
            else {}
        )
        self.assertEqual(
            str(
                (adaptation_reasoning.get("execution_truth_governance", {})
                if isinstance(adaptation_reasoning.get("execution_truth_governance", {}), dict)
                else {}).get("governance_decision", "")
            ),
            str(governance.get("governance_decision", "")),
            adaptation_reasoning,
        )
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)

        status, hard_ceiling_boundary_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "scope": scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "success_rate": 0.99,
                    "escalation_rate": 0.0,
                    "retry_rate": 0.0,
                    "interruption_rate": 0.0,
                    "memory_delta_rate": 1.0,
                    "sample_count": 24,
                    "override_rate": 0.0,
                    "replan_rate": 0.0,
                    "environment_stability": 0.98,
                    "development_confidence": 0.95,
                    "constraint_reliability": 0.98,
                    "experiment_confidence": 0.95,
                    "hard_ceiling_violations": {"system_integrity": True},
                },
                "metadata_json": {"run_id": run_id, "phase": "hard-ceiling"},
            },
        )
        self.assertEqual(status, 200, hard_ceiling_boundary_payload)
        hard_ceiling_boundary = (
            hard_ceiling_boundary_payload.get("boundary", {})
            if isinstance(hard_ceiling_boundary_payload, dict)
            else {}
        )
        hard_ceiling_reasoning = (
            hard_ceiling_boundary.get("adaptation_reasoning", {})
            if isinstance(hard_ceiling_boundary.get("adaptation_reasoning", {}), dict)
            else {}
        )
        self.assertEqual(
            str(hard_ceiling_reasoning.get("decision", "")),
            "hard_ceiling_enforced",
            hard_ceiling_reasoning,
        )
        self.assertEqual(
            str(hard_ceiling_boundary.get("current_level", "")),
            "operator_required",
            hard_ceiling_boundary,
        )

        status, maintenance_payload = post_json(
            "/maintenance/cycle",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "stale_after_seconds": 300,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, maintenance_payload)
        maintenance_run = maintenance_payload.get("run", {}) if isinstance(maintenance_payload, dict) else {}
        maintenance_outcomes = (
            maintenance_run.get("maintenance_outcomes", {})
            if isinstance(maintenance_run.get("maintenance_outcomes", {}), dict)
            else {}
        )
        self.assertTrue(bool(maintenance_outcomes.get("governance_auto_execute_blocked", False)), maintenance_outcomes)
        self.assertEqual(
            str(maintenance_outcomes.get("execution_truth_governance_decision", "")),
            str(governance.get("governance_decision", "")),
            maintenance_outcomes,
        )

        status, stewardship_payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "managed_scope": scope,
                "stale_after_seconds": 300,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": True,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 300,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.05,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"objective81-governed-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope, "phase": "post-governance"},
            },
        )
        self.assertEqual(status, 200, stewardship_payload)
        stewardship_cycle = stewardship_payload.get("cycle", {}) if isinstance(stewardship_payload, dict) else {}
        stewardship_verification = (
            stewardship_cycle.get("verification", {})
            if isinstance(stewardship_cycle.get("verification", {}), dict)
            else {}
        )
        selected_actions = (
            stewardship_cycle.get("selected_actions", [])
            if isinstance(stewardship_cycle.get("selected_actions", []), list)
            else []
        )
        self.assertEqual(
            str(
                (stewardship_verification.get("execution_truth_governance", {})
                if isinstance(stewardship_verification.get("execution_truth_governance", {}), dict)
                else {}).get("governance_decision", "")
            ),
            str(governance.get("governance_decision", "")),
            stewardship_verification,
        )
        self.assertTrue(
            any(
                isinstance(item, dict)
                and str(item.get("action_type", "")) == "execution_truth_governance_applied"
                for item in selected_actions
            ),
            selected_actions,
        )

    def test_objective81_low_quality_evidence_stays_monitor_only(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective81-low-quality-{run_id}"

        self._seed_execution_truth(
            scope=scope,
            run_id=run_id,
            suffix="weak",
            truth_confidence=0.24,
            retry_count=0,
            fallback_used=False,
            simulation_match_status="matched",
            environment_shift_detected=False,
            actual_duration_ms=980,
        )

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "managed_scope": scope,
                "lookback_hours": 24,
                "metadata_json": {"run_id": run_id, "phase": "weak-evidence"},
            },
        )
        self.assertEqual(status, 200, governance_payload)
        governance = governance_payload.get("governance", {}) if isinstance(governance_payload, dict) else {}
        self.assertEqual(str(governance.get("governance_decision", "")), "monitor_only", governance)

        status, maintenance_payload = post_json(
            "/maintenance/cycle",
            {
                "actor": "objective81-test",
                "source": "objective81-governance-loop",
                "stale_after_seconds": 300,
                "max_strategies": 3,
                "max_actions": 3,
                "auto_execute": True,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, maintenance_payload)
        maintenance_run = maintenance_payload.get("run", {}) if isinstance(maintenance_payload, dict) else {}
        maintenance_outcomes = (
            maintenance_run.get("maintenance_outcomes", {})
            if isinstance(maintenance_run.get("maintenance_outcomes", {}), dict)
            else {}
        )
        self.assertFalse(bool(maintenance_outcomes.get("governance_auto_execute_blocked", False)), maintenance_outcomes)
        self.assertEqual(
            str(maintenance_outcomes.get("execution_truth_governance_decision", "")),
            "monitor_only",
            maintenance_outcomes,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)