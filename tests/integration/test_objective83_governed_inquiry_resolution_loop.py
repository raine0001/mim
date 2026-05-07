import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
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


class Objective83GovernedInquiryResolutionLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 83",
            base_url=BASE_URL,
            require_governed_inquiry_contract=True,
        )

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
                "text": f"objective83 inquiry stale scan {run_id}",
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
                            "label": f"obj83-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_stewardship_followup(self, *, scope: str, run_id: str, source: str) -> None:
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
                "actor": "objective83-test",
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
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, goals)

        status, boundary = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective83-test",
                "source": source,
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
        self.assertEqual(status, 200, boundary)

        status, cycled = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective83-test",
                "source": source,
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
                    "key_objects": [f"obj83-critical-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "objective83"},
            },
        )
        self.assertEqual(status, 200, cycled)

    def _seed_plan(self, *, run_id: str, scope: str) -> None:
        status, plan = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective83-test",
                "source": "objective83-governed-inquiry",
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
                    },
                    {
                        "goal_key": f"confirm:{scope}",
                        "title": "Confirm target state",
                        "priority": "normal",
                        "goal_type": "target_confirmation",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.56,
                        "urgency": 0.55,
                        "is_physical": False,
                        "metadata_json": {"scope": scope, "run_id": run_id},
                    },
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
        self.assertEqual(status, 200, plan)

    def _seed_low_confidence_friction(self, *, run_id: str) -> None:
        evaluation_ids: list[int] = []
        for index in range(3):
            status, evaluation = post_json(
                "/constraints/evaluate",
                {
                    "actor": "objective83-test",
                    "source": "objective83-governed-inquiry",
                    "goal": {
                        "goal_id": f"obj83-goal-{run_id}-{index}",
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
            self.assertEqual(status, 200, evaluation)
            evaluation_ids.append(int(evaluation.get("evaluation_id", 0) or 0))

        for evaluation_id in evaluation_ids:
            status, outcome = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective83-test",
                    "evaluation_id": evaluation_id,
                    "result": "success",
                    "outcome_quality": 0.9,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

    def _seed_target_confidence_warnings(
        self,
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
                    "actor": "objective83-test",
                    "source": "objective83-governed-inquiry",
                    "goal": {
                        "goal_id": f"obj83-target-confidence-{run_id}-{index}",
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
            self.assertEqual(status, 200, evaluation)
            evaluation_ids.append(int(evaluation.get("evaluation_id", 0) or 0))

        for evaluation_id in evaluation_ids:
            status, outcome = post_json(
                "/constraints/outcomes",
                {
                    "actor": "objective83-test",
                    "evaluation_id": evaluation_id,
                    "result": "success",
                    "outcome_quality": 0.85,
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

    def _age_initial_target_confidence_batch(
        self,
        *,
        run_id: str,
        question_id: int,
    ) -> None:
        script = """
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from core.db import SessionLocal
from core.models import ConstraintEvaluation, WorkspaceInquiryQuestion

run_id = sys.argv[1]
question_id = int(sys.argv[2])
aged_at = datetime.now(timezone.utc) - timedelta(hours=2)

async def main() -> None:
    async with SessionLocal() as db:
        inquiry = await db.get(WorkspaceInquiryQuestion, question_id)
        if inquiry is None:
            raise SystemExit("missing inquiry row")
        inquiry.answered_at = aged_at
        inquiry.created_at = aged_at

        rows = (
            (
                await db.execute(
                    select(ConstraintEvaluation)
                    .order_by(ConstraintEvaluation.id.desc())
                    .limit(300)
                )
            )
            .scalars()
            .all()
        )
        matched = 0
        for row in rows:
            explanation = row.explanation_json if isinstance(row.explanation_json, dict) else {}
            metadata = explanation.get("metadata_json", {})
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("run_id", "")).strip() != run_id:
                continue
            row.created_at = aged_at
            matched += 1

        if matched <= 0:
            raise SystemExit("no constraint rows matched run_id")
        await db.commit()

asyncio.run(main())
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, run_id, str(question_id)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def _force_latest_autonomy_level(self, *, level: str) -> None:
        script = """
import asyncio
import sys
from sqlalchemy import select
from core.db import SessionLocal
from core.models import WorkspaceAutonomyBoundaryProfile

level = sys.argv[1]

async def main() -> None:
    async with SessionLocal() as db:
        profile = (
            (
                await db.execute(
                    select(WorkspaceAutonomyBoundaryProfile)
                    .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if profile is None:
            raise SystemExit("missing autonomy profile")
        profile.current_level = level
        profile.profile_status = "applied"
        profile.adjustment_reason = "objective83-test-forced-level"
        await db.commit()

asyncio.run(main())
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(level).strip()],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def _set_bounded_auto(self, *, run_id: str, scope: str) -> None:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective83-test",
                "source": "objective83-governed-inquiry",
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
        self.assertEqual(status, 200, payload)
        boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
        if str(boundary.get("current_level", "")) != "bounded_auto":
            self._force_latest_autonomy_level(level="bounded_auto")

    def _generate_questions(
        self,
        *,
        run_id: str,
        source: str,
        lookback_hours: int = 24,
        extra_metadata: dict | None = None,
    ) -> dict:
        metadata = {"run_id": run_id}
        if isinstance(extra_metadata, dict):
            metadata.update(extra_metadata)
        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective83-test",
                "source": source,
                "lookback_hours": lookback_hours,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": metadata,
            },
        )
        self.assertEqual(status, 200, generated)
        self.assertTrue(isinstance(generated, dict), generated)
        return generated

    def test_required_inquiry_records_policy_and_effect_contract(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective83-stewardship-{run_id}"

        self._seed_stewardship_followup(
            scope=scope,
            run_id=run_id,
            source="objective83-required-policy",
        )
        generated = self._generate_questions(
            run_id=run_id,
            source="objective83-required-policy",
        )

        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        decisions = generated.get("decisions", []) if isinstance(generated, dict) else []
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
        self.assertEqual(str((question or {}).get("decision_state", "")), "required_for_progress")
        self.assertEqual(
            str((question or {}).get("decision_reason", "")),
            "persistent_degradation_with_actionable_uncertainty",
        )
        self.assertIn("rescan", list((question or {}).get("allowed_answer_effects", [])))
        self.assertIn("tighten_tracking", list((question or {}).get("allowed_answer_effects", [])))
        self.assertIn("propose_improvement", list((question or {}).get("allowed_answer_effects", [])))

        decision = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
            ),
            None,
        )
        self.assertIsNotNone(decision, decisions)
        self.assertEqual(str((decision or {}).get("decision_state", "")), "required_for_progress")

        question_id = int((question or {}).get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "stabilize_scope_now",
                "answer_json": {"reason": "collect fresh evidence before policy changes"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        self.assertEqual(str(applied_effect.get("decision_state", "")), "required_for_progress")
        self.assertTrue(bool(applied_effect.get("material_state_change", False)), applied_effect)
        self.assertIn("workspace_proposal_created", list(applied_effect.get("state_delta_summary", [])))
        self.assertIn("rescan", list(applied_effect.get("allowed_answer_effects", [])))

    def test_recent_answer_reuse_defers_duplicate_inquiry(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective83-reuse-{run_id}"

        self._seed_stewardship_followup(
            scope=scope,
            run_id=run_id,
            source="objective83-reuse-policy",
        )
        first_generated = self._generate_questions(
            run_id=run_id,
            source="objective83-reuse-policy",
        )
        first_questions = (
            first_generated.get("questions", []) if isinstance(first_generated, dict) else []
        )
        question = next(
            (
                item
                for item in first_questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
            ),
            None,
        )
        self.assertIsNotNone(question, first_questions)

        question_id = int((question or {}).get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "keep_monitoring",
                "answer_json": {"reason": "unchanged condition, reuse safe answer"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)

        second_generated = self._generate_questions(
            run_id=run_id,
            source="objective83-reuse-policy",
        )
        second_questions = (
            second_generated.get("questions", []) if isinstance(second_generated, dict) else []
        )
        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
                for item in second_questions
            ),
            second_questions,
        )
        decisions = (
            second_generated.get("decisions", []) if isinstance(second_generated, dict) else []
        )
        decision = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "stewardship_persistent_degradation"
            ),
            None,
        )
        self.assertIsNotNone(decision, decisions)
        self.assertEqual(str((decision or {}).get("decision_state", "")), "deferred_due_to_cooldown")
        self.assertTrue(bool((decision or {}).get("recent_answer_reused", False)), decision)
        self.assertTrue(bool((decision or {}).get("duplicate_suppressed", False)), decision)
        self.assertGreater(int((decision or {}).get("cooldown_remaining_seconds", 0) or 0), 0)
        self.assertEqual(
            str((decision or {}).get("suppression_reason", "")),
            "recent_answer_still_valid",
        )

    def test_low_value_inquiry_is_suppressed_before_row_creation(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective83-low-value-{run_id}"

        self._seed_plan(run_id=run_id, scope=scope)
        self._seed_low_confidence_friction(run_id=run_id)
        self._set_bounded_auto(run_id=run_id, scope=scope)

        generated = self._generate_questions(
            run_id=run_id,
            source="objective83-suppression-policy",
        )
        decisions = generated.get("decisions", []) if isinstance(generated, dict) else []
        questions = generated.get("questions", []) if isinstance(generated, dict) else []

        target_confidence = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
            ),
            None,
        )
        self.assertIsNotNone(target_confidence, decisions)
        self.assertEqual(
            str((target_confidence or {}).get("decision_state", "")),
            "suppressed_high_confidence_autonomy",
        )
        self.assertEqual(
            str((target_confidence or {}).get("suppression_reason", "")),
            "high_confidence_autonomy",
        )

        soft_friction = next(
            (
                item
                for item in decisions
                if isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                == "repeated_soft_constraint_friction"
            ),
            None,
        )
        self.assertIsNotNone(soft_friction, decisions)
        self.assertEqual(
            str((soft_friction or {}).get("decision_state", "")),
            "suppressed_low_evidence",
        )
        self.assertEqual(
            str((soft_friction or {}).get("suppression_reason", "")),
            "low_evidence",
        )

        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", ""))
                in {
                    "target_confidence_too_low",
                    "repeated_soft_constraint_friction",
                }
                for item in questions
            ),
            questions,
        )

    def test_partial_improvement_after_required_inquiry_does_not_retrigger(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective83-partial-improvement-{run_id}"

        self._seed_plan(run_id=run_id, scope=scope)
        self._seed_target_confidence_warnings(run_id=run_id, count=5)

        first_generated = self._generate_questions(
            run_id=run_id,
            source="objective83-partial-improvement",
            extra_metadata={"inquiry_policy_inputs": {"cooldown_seconds": 1}},
        )
        first_questions = (
            first_generated.get("questions", []) if isinstance(first_generated, dict) else []
        )
        question = next(
            (
                item
                for item in first_questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
            ),
            None,
        )
        self.assertIsNotNone(question, first_questions)
        self.assertEqual(
            str((question or {}).get("decision_state", "")),
            "required_for_progress",
        )

        question_id = int((question or {}).get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "trigger_rescan",
                "answer_json": {
                    "reason": "collect fresh evidence after confidence degradation"
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        self.assertTrue(bool(applied_effect.get("workspace_proposal_created", False)), applied_effect)

        proposal_id = int(applied_effect.get("workspace_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)
        status, proposal_detail = get_json(f"/workspace/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        self.assertEqual(str((proposal_detail or {}).get("status", "")), "pending")

        self._age_initial_target_confidence_batch(
            run_id=run_id,
            question_id=question_id,
        )
        self._seed_target_confidence_warnings(
            run_id=run_id,
            count=1,
            target_confidence=0.8,
        )

        second_generated = self._generate_questions(
            run_id=run_id,
            source="objective83-partial-improvement",
            lookback_hours=1,
            extra_metadata={"inquiry_policy_inputs": {"cooldown_seconds": 1}},
        )
        second_questions = (
            second_generated.get("questions", []) if isinstance(second_generated, dict) else []
        )
        second_decisions = (
            second_generated.get("decisions", []) if isinstance(second_generated, dict) else []
        )

        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
                for item in second_questions
            ),
            second_questions,
        )
        self.assertFalse(
            any(
                isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "target_confidence_too_low"
                for item in second_decisions
            ),
            second_decisions,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)