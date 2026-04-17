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
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": body}


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
            return exc.code, {"raw_body": body}


class Objective116BoundariesEverywhereTest(unittest.TestCase):
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

    def _recompute_operator_required_boundary(self, *, scope: str, run_id: str) -> dict:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective116-test",
                "source": "objective116-boundaries-everywhere",
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 5,
                "apply_recommended_boundaries": True,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "sample_count": 18,
                    "success_rate": 0.28,
                    "escalation_rate": 0.61,
                    "retry_rate": 0.47,
                    "interruption_rate": 0.36,
                    "memory_delta_rate": 0.22,
                    "override_rate": 0.51,
                    "replan_rate": 0.42,
                    "environment_stability": 0.21,
                    "development_confidence": 0.33,
                    "constraint_reliability": 0.39,
                    "experiment_confidence": 0.25,
                },
                "metadata_json": {"run_id": run_id, "objective": "116"},
            },
        )
        self.assertEqual(status, 200, payload)
        boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)
        return boundary

    def _run_scan(self, *, scope: str, label: str, run_id: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective116 workspace scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scope,
                    "confidence_threshold": 0.6,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0, event)

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
                            "label": label,
                            "zone": scope,
                            "confidence": 0.97,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _resolve_target(self, *, scope: str, label: str) -> dict:
        status, resolved = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": label,
                "preferred_zone": scope,
                "source": "objective116-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolved)
        return resolved

    def _create_and_execute_plan(self, *, scope: str, target_resolution_id: int, run_id: str) -> dict:
        status, created = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": target_resolution_id,
                "action_type": "prepare_reach_plan",
                "source": "objective116-test",
                "notes": "objective116 boundary envelope",
                "motion_plan_overrides": {},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        self.assertEqual(str(created.get("boundary_profile", "")), "operator_required", created)
        self.assertTrue(created.get("approval_required"), created)
        self.assertTrue(created.get("decision_basis", {}).get("why_not_automatic"), created)

        steps = created.get("steps", []) if isinstance(created.get("steps", []), list) else []
        self.assertTrue(steps, created)
        self.assertTrue(all(str(step.get("boundary_profile", "")) == "operator_required" for step in steps), steps)

        plan_id = int(created.get("plan_id", 0) or 0)
        self.assertGreater(plan_id, 0, created)

        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {
                "actor": "operator",
                "reason": "objective116 approval",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, approved)

        status, simulated = post_json(
            f"/workspace/action-plans/{plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "objective116 simulate",
                "collision_risk_threshold": 0.55,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, simulated)
        self.assertEqual(simulated.get("simulation_outcome"), "plan_safe", simulated)

        status, executed = post_json(
            f"/workspace/action-plans/{plan_id}/execute",
            {
                "actor": "operator",
                "reason": "objective116 execute",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.55,
                "target_confidence_minimum": 0.7,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, executed)
        self.assertEqual(str(executed.get("boundary_profile", "")), "operator_required", executed)
        self.assertTrue(executed.get("approval_required"), executed)
        self.assertIn("boundary = operator_required", str(executed.get("decision_basis", {}).get("why_not_automatic", "")), executed)
        return executed

    def _create_failed_execution(self, *, scope: str, run_id: str) -> tuple[int, str]:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective116 recovery check {scope}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "requested_goal": f"inspect {scope}",
                "metadata_json": {
                    "capability": "workspace_check",
                    "managed_scope": scope,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        execution = payload.get("execution", {}) if isinstance(payload, dict) else {}
        execution_id = int(execution.get("execution_id", 0) or 0)
        trace_id = str(execution.get("trace_id") or "")
        if execution_id > 0 and not trace_id:
            detail_status, detail_payload = get_json(f"/gateway/capabilities/executions/{execution_id}")
            self.assertEqual(detail_status, 200, detail_payload)
            trace_id = str(detail_payload.get("trace_id") or "")
        self.assertGreater(execution_id, 0, payload)
        self.assertTrue(trace_id, payload)

        status, feedback = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "actor": "executor",
                "status": "failed",
                "reason": "objective116 simulated failure",
                "feedback_json": {"objective": "116", "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, feedback)
        return execution_id, trace_id

    def _find_journal_entry(self, *, action: str, run_id: str, scope: str | None = None) -> dict:
        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        rows = journal if isinstance(journal, list) else []
        match = next(
            (
                entry
                for entry in rows
                if isinstance(entry, dict)
                and str(entry.get("action", "")) == action
                and (
                    str((entry.get("metadata_json", {}) if isinstance(entry.get("metadata_json", {}), dict) else {}).get("run_id", "")) == run_id
                    or str((entry.get("boundary_profile", {}) if isinstance(entry.get("boundary_profile", {}), dict) else {}).get("scope", "")) == str(scope or "")
                )
            ),
            None,
        )
        self.assertIsNotNone(match, rows[:20])
        return match or {}

    def test_objective116_boundary_envelope_flows_across_planning_execution_recovery_and_ui(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"front-center-objective116-{run_id}"
        label = f"objective116 target {run_id}"

        self._register_workspace_scan()
        boundary = self._recompute_operator_required_boundary(scope=scope, run_id=run_id)
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)

        status, horizon = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective116-test",
                "source": "objective116-boundaries-everywhere",
                "planning_horizon_minutes": 90,
                "goal_candidates": [
                    {
                        "goal_key": f"reach-{run_id}",
                        "title": "Reach target under governed boundary",
                        "priority": "high",
                        "goal_type": "directed_reach",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.8,
                        "urgency": 0.9,
                        "requires_fresh_map": True,
                        "requires_high_confidence": True,
                        "is_physical": True,
                    }
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.75,
                },
                "map_freshness_seconds": 300,
                "object_confidence": 0.92,
                "human_aware_state": {
                    "human_in_workspace": True,
                    "shared_workspace_active": True,
                },
                "operator_preferences": {"directed_reach": 0.6},
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, horizon)
        self.assertEqual(str(horizon.get("boundary_profile", "")), "operator_required", horizon)
        self.assertTrue(horizon.get("approval_required"), horizon)
        self.assertIn(
            "boundary = operator_required",
            str(horizon.get("decision_basis", {}).get("why_not_automatic", "")),
            horizon,
        )
        stages = horizon.get("staged_action_graph", []) if isinstance(horizon.get("staged_action_graph", []), list) else []
        self.assertTrue(stages, horizon)
        self.assertTrue(all(str(stage.get("boundary_profile", "")) == "operator_required" for stage in stages), stages)

        self._run_scan(scope=scope, label=label, run_id=run_id)
        resolved = self._resolve_target(scope=scope, label=label)
        executed = self._create_and_execute_plan(
            scope=scope,
            target_resolution_id=int(resolved.get("target_resolution_id", 0) or 0),
            run_id=run_id,
        )

        execution_id = int(executed.get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0, executed)

        detail_status, execution_detail = get_json(f"/gateway/capabilities/executions/{execution_id}")
        self.assertEqual(detail_status, 200, execution_detail)
        execution_feedback = execution_detail.get("feedback_json", {}) if isinstance(execution_detail.get("feedback_json", {}), dict) else {}
        execution_boundary = execution_feedback.get("boundary_profile", {}) if isinstance(execution_feedback.get("boundary_profile", {}), dict) else {}
        execution_basis = execution_feedback.get("decision_basis", {}) if isinstance(execution_feedback.get("decision_basis", {}), dict) else {}
        self.assertEqual(str(execution_boundary.get("current_level", "")), "operator_required", execution_detail)
        self.assertIn(
            "boundary = operator_required",
            str(execution_basis.get("why_not_automatic", "")),
            execution_detail,
        )

        _, trace_id = self._create_failed_execution(scope=scope, run_id=run_id)

        status, recovery_eval_payload = post_json(
            "/execution/recovery/evaluate",
            {
                "actor": "objective116-test",
                "source": "objective116-boundaries-everywhere",
                "trace_id": trace_id,
            },
        )
        self.assertEqual(status, 200, recovery_eval_payload)
        recovery_eval = recovery_eval_payload.get("recovery", {}) if isinstance(recovery_eval_payload, dict) else {}
        recovery_boundary = recovery_eval.get("boundary_profile", {}) if isinstance(recovery_eval.get("boundary_profile", {}), dict) else {}
        recovery_basis = recovery_eval.get("decision_basis", {}) if isinstance(recovery_eval.get("decision_basis", {}), dict) else {}
        recovery_explanation = " ".join(
            [
                str(recovery_basis.get("why_not_automatic", "")),
                str(recovery_basis.get("why_automatic_was_allowed", "")),
            ]
        )
        self.assertEqual(str(recovery_boundary.get("current_level", "")), "operator_required", recovery_eval)
        self.assertTrue(recovery_eval.get("approval_required"), recovery_eval)
        self.assertIn(
            "boundary = operator_required",
            recovery_explanation,
            recovery_eval,
        )

        status, recovery_attempt_payload = post_json(
            "/execution/recovery/attempt",
            {
                "actor": "objective116-test",
                "source": "objective116-boundaries-everywhere",
                "trace_id": trace_id,
                "requested_decision": str(recovery_eval.get("recovery_decision", "retry_current_step") or "retry_current_step"),
            },
        )
        self.assertEqual(status, 200, recovery_attempt_payload)
        recovery_attempt = recovery_attempt_payload.get("attempt", {}) if isinstance(recovery_attempt_payload, dict) else {}
        attempt_boundary = recovery_attempt.get("boundary_profile", {}) if isinstance(recovery_attempt.get("boundary_profile", {}), dict) else {}
        attempt_basis = recovery_attempt.get("decision_basis", {}) if isinstance(recovery_attempt.get("decision_basis", {}), dict) else {}
        attempt_explanation = " ".join(
            [
                str(attempt_basis.get("why_not_automatic", "")),
                str(attempt_basis.get("why_automatic_was_allowed", "")),
            ]
        )
        self.assertEqual(str(attempt_boundary.get("current_level", "")), "operator_required", recovery_attempt)
        self.assertIn(
            "boundary = operator_required",
            attempt_explanation,
            recovery_attempt,
        )

        horizon_journal = self._find_journal_entry(action="horizon_plan_created", run_id=run_id, scope=scope)
        self.assertEqual(
            str((horizon_journal.get("boundary_profile", {}) if isinstance(horizon_journal.get("boundary_profile", {}), dict) else {}).get("current_level", "")),
            "operator_required",
            horizon_journal,
        )
        self.assertIn(
            "boundary = operator_required",
            str((horizon_journal.get("decision_basis", {}) if isinstance(horizon_journal.get("decision_basis", {}), dict) else {}).get("why_not_automatic", "")),
            horizon_journal,
        )

        execute_journal = self._find_journal_entry(action="workspace_action_plan_execute", run_id=run_id, scope=scope)
        self.assertEqual(
            str((execute_journal.get("boundary_profile", {}) if isinstance(execute_journal.get("boundary_profile", {}), dict) else {}).get("current_level", "")),
            "operator_required",
            execute_journal,
        )
        self.assertIn(
            "boundary = operator_required",
            str((execute_journal.get("decision_basis", {}) if isinstance(execute_journal.get("decision_basis", {}), dict) else {}).get("why_not_automatic", "")),
            execute_journal,
        )

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        operator_reasoning = state.get("operator_reasoning", {}) if isinstance(state, dict) else {}
        autonomy = operator_reasoning.get("autonomy", {}) if isinstance(operator_reasoning.get("autonomy", {}), dict) else {}
        self.assertEqual(str(autonomy.get("current_level", "")), "operator_required", autonomy)
        self.assertEqual(bool(autonomy.get("approval_required", False)), True, autonomy)
        self.assertIn(
            "boundary = operator_required",
            str(autonomy.get("why_not_automatic", "")),
            autonomy,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)