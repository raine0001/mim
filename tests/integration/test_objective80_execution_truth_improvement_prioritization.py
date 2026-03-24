import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
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


class Objective80ExecutionTruthImprovementPrioritizationTest(unittest.TestCase):
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
        self._register_workspace_scan()
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective80 stewardship stale scan {run_id}",
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
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, payload)

        status, payload = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": f"obj80-stewardship-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, payload)

    def _create_execution_truth_improvement_proposal(self, *, run_id: str) -> tuple[int, int]:
        capability_name = f"execution_truth_improvement_probe_{run_id}"

        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80 improvement prioritization probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, payload)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"run execution truth improvement prioritization probe {run_id}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for improvement prioritization",
                "metadata_json": {"capability": capability_name, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(
            (
                event.get("execution", {})
                if isinstance(event.get("execution", {}), dict)
                else {}
            ).get("execution_id", 0)
            or 0
        )
        self.assertGreater(execution_id, 0)

        for feedback in [
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "feedback_json": {"run_id": run_id},
            },
            {
                "status": "running",
                "reason": "running",
                "actor": "tod",
                "feedback_json": {"run_id": run_id},
            },
            {
                "status": "succeeded",
                "reason": "execution truth indicates runtime mismatch",
                "actor": "tod",
                "runtime_outcome": "recovered",
                "feedback_json": {"run_id": run_id},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 800,
                    "actual_duration_ms": 1600,
                    "retry_count": 2,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": True,
                    "simulation_match_status": "mismatch",
                    "truth_confidence": 0.92,
                    "published_at": "2026-03-23T23:40:00Z",
                },
            },
        ]:
            status, payload = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                feedback,
            )
            self.assertEqual(status, 200, payload)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-improvement-priority",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        execution_truth_question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "execution_truth_runtime_mismatch"
            ),
            None,
        )
        self.assertIsNotNone(execution_truth_question, questions)

        question_id = int(execution_truth_question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "request_execution_truth_review",
                "answer_json": {"reason": "promote runtime-truth improvement review"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        proposal_id = int(applied_effect.get("improvement_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)
        return proposal_id, execution_id

    def _create_generic_stewardship_improvement_proposal(self, *, run_id: str) -> int:
        scope = f"stewardship-generic-{run_id}"
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": "objective80-improvement-compare",
            },
        )
        self.assertEqual(status, 200, pref)

        status, _ = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective80-test",
                "source": "objective80-improvement-compare",
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "hard_ceiling_overrides": {"human_safety": True, "legality": True, "system_integrity": True},
                "evidence_inputs_override": {
                    "success_rate": 0.9,
                    "escalation_rate": 0.05,
                    "retry_rate": 0.05,
                    "interruption_rate": 0.05,
                    "memory_delta_rate": 0.7,
                    "sample_count": 20,
                    "override_rate": 0.0,
                    "replan_rate": 0.0,
                    "environment_stability": 0.9,
                    "development_confidence": 0.8,
                    "constraint_reliability": 0.9,
                    "experiment_confidence": 0.7,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective80-test",
                "source": "objective80-improvement-compare",
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
                    "key_objects": [f"obj80-generic-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "generic_compare"},
            },
        )
        self.assertEqual(status, 200, cycled)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective80-test",
                "source": "objective80-improvement-compare",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
                and str(
                    (
                        item.get("trigger_evidence", {})
                        if isinstance(item.get("trigger_evidence", {}), dict)
                        else {}
                    ).get("managed_scope", "")
                )
                == scope
            ),
            None,
        )
        self.assertIsNotNone(question, questions)

        question_id = int((question or {}).get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "request_stewardship_improvement",
                "answer_json": {"reason": "capture generic stewardship workflow review"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        proposal_id = int(applied_effect.get("improvement_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)
        return proposal_id

    def _refresh_backlog(self, *, run_id: str) -> list[dict]:
        status, refreshed = post_json(
            "/improvement/backlog/refresh",
            {
                "actor": "objective80-test",
                "source": "objective80-execution-truth-improvement-priority",
                "lookback_hours": 24,
                "min_occurrence_count": 2,
                "max_items": 500,
                "auto_experiment_limit": 0,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, refreshed)
        return refreshed.get("items", []) if isinstance(refreshed, dict) else []

    def test_execution_truth_increases_improvement_priority_with_explicit_reasoning(self) -> None:
        run_id = uuid4().hex[:8]
        proposal_id, execution_id = self._create_execution_truth_improvement_proposal(
            run_id=run_id
        )

        status, proposal_detail = get_json(f"/improvement/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        proposal = proposal_detail.get("proposal", {}) if isinstance(proposal_detail, dict) else {}
        proposal_evidence = proposal.get("evidence", {}) if isinstance(proposal.get("evidence", {}), dict) else {}
        proposal_metadata = proposal.get("metadata_json", {}) if isinstance(proposal.get("metadata_json", {}), dict) else {}
        self.assertEqual(str(proposal.get("affected_component", "")), "execution_truth_bridge")
        self.assertEqual(int(proposal_evidence.get("execution_id", 0) or 0), execution_id)
        self.assertIn("simulation_reality_mismatch", proposal_evidence.get("signal_types", []))
        self.assertTrue(bool(proposal_metadata.get("objective80_execution_truth", False)))

        backlog = self._refresh_backlog(run_id=run_id)
        target = next(
            (
                item
                for item in backlog
                if isinstance(item, dict)
                and int(item.get("proposal_id", 0) or 0) == proposal_id
            ),
            None,
        )
        self.assertIsNotNone(target, backlog)

        improvement_id = int((target or {}).get("improvement_id", 0) or 0)
        self.assertGreater(improvement_id, 0)
        self.assertIn("execution_truth=", str((target or {}).get("why_ranked", "")))
        self.assertGreater(float((target or {}).get("priority_score", 0.0) or 0.0), 0.0)

        status, detail = get_json(f"/improvement/backlog/{improvement_id}")
        self.assertEqual(status, 200, detail)
        backlog_item = detail.get("backlog_item", {}) if isinstance(detail, dict) else {}
        reasoning = backlog_item.get("reasoning", {}) if isinstance(backlog_item.get("reasoning", {}), dict) else {}
        execution_truth_influence = (
            reasoning.get("execution_truth_influence", {})
            if isinstance(reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )
        metadata = backlog_item.get("metadata_json", {}) if isinstance(backlog_item.get("metadata_json", {}), dict) else {}

        self.assertGreater(
            float(execution_truth_influence.get("priority_weight", 0.0) or 0.0),
            0.0,
            execution_truth_influence,
        )
        self.assertGreaterEqual(
            int(execution_truth_influence.get("execution_count", 0) or 0),
            1,
            execution_truth_influence,
        )
        self.assertGreaterEqual(
            int(execution_truth_influence.get("deviation_signal_count", 0) or 0),
            5,
            execution_truth_influence,
        )
        self.assertIn(
            "simulation_reality_mismatch",
            execution_truth_influence.get("signal_types", []),
            execution_truth_influence,
        )
        self.assertIn(
            "workflow",
            str(execution_truth_influence.get("priority_rationale", "")).lower(),
        )
        self.assertTrue(bool(metadata.get("objective80_execution_truth_priority", False)))

    def test_execution_truth_workflow_proposal_outranks_generic_stewardship_workflow_proposal(self) -> None:
        run_id = uuid4().hex[:8]
        truth_proposal_id, _ = self._create_execution_truth_improvement_proposal(run_id=run_id)
        generic_proposal_id = self._create_generic_stewardship_improvement_proposal(run_id=run_id)

        backlog = self._refresh_backlog(run_id=run_id)
        truth_item = next(
            (
                item
                for item in backlog
                if isinstance(item, dict)
                and int(item.get("proposal_id", 0) or 0) == truth_proposal_id
            ),
            None,
        )
        generic_item = next(
            (
                item
                for item in backlog
                if isinstance(item, dict)
                and int(item.get("proposal_id", 0) or 0) == generic_proposal_id
            ),
            None,
        )
        self.assertIsNotNone(truth_item, backlog)
        self.assertIsNotNone(generic_item, backlog)

        truth_status, truth_detail = get_json(
            f"/improvement/backlog/{int((truth_item or {}).get('improvement_id', 0) or 0)}"
        )
        generic_status, generic_detail = get_json(
            f"/improvement/backlog/{int((generic_item or {}).get('improvement_id', 0) or 0)}"
        )
        self.assertEqual(truth_status, 200, truth_detail)
        self.assertEqual(generic_status, 200, generic_detail)
        truth_reasoning = (
            truth_detail.get("backlog_item", {}).get("reasoning", {})
            if isinstance(truth_detail, dict)
            else {}
        )
        generic_reasoning = (
            generic_detail.get("backlog_item", {}).get("reasoning", {})
            if isinstance(generic_detail, dict)
            else {}
        )
        truth_influence = (
            truth_reasoning.get("execution_truth_influence", {})
            if isinstance(truth_reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )
        generic_influence = (
            generic_reasoning.get("execution_truth_influence", {})
            if isinstance(generic_reasoning.get("execution_truth_influence", {}), dict)
            else {}
        )

        self.assertGreater(
            float((truth_item or {}).get("priority_score", 0.0) or 0.0),
            float((generic_item or {}).get("priority_score", 0.0) or 0.0),
            {"truth": truth_item, "generic": generic_item},
        )
        self.assertGreater(
            float(truth_influence.get("priority_weight", 0.0) or 0.0),
            float(generic_influence.get("priority_weight", 0.0) or 0.0),
            {"truth": truth_influence, "generic": generic_influence},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)