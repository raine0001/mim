import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
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


class Objective60StewardshipInquiryFollowupTest(unittest.TestCase):
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
                "text": f"objective60 inquiry stale scan {run_id}",
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
                            "label": f"obj60-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_stewardship_followup(self, *, scope: str, run_id: str, source: str) -> dict:
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
                "actor": "objective60-test",
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
                "actor": "objective60-test",
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
                "actor": "objective60-test",
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
                    "key_objects": [f"obj60-critical-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "followup"},
            },
        )
        self.assertEqual(status, 200, cycled)
        return cycled

    def test_persistent_stewardship_degradation_generates_inquiry_followup(
        self,
    ) -> None:
        run_id = uuid4().hex[:8]
        scope = f"stewardship-followup-{run_id}"

        cycled = self._seed_stewardship_followup(
            scope=scope,
            run_id=run_id,
            source="objective60-inquiry-followup",
        )

        summary = cycled.get("summary", {}) if isinstance(cycled, dict) else {}
        cycle = (
            cycled.get("cycle", {}) if isinstance(cycled.get("cycle", {}), dict) else {}
        )
        verification = (
            cycle.get("verification", {})
            if isinstance(cycle.get("verification", {}), dict)
            else {}
        )
        surfaced_types = set(summary.get("inquiry_candidate_types", []))
        self.assertTrue(bool(summary.get("persistent_degradation", False)), summary)
        self.assertGreaterEqual(int(summary.get("inquiry_candidate_count", 0) or 0), 1)
        self.assertTrue(
            bool(
                {
                    "key_object_unknown",
                    "stale_zone_detected",
                    "zone_uncertainty_above_target",
                    "zone_drift_above_target",
                }
                & surfaced_types
            ),
            summary,
        )
        self.assertTrue(bool(summary.get("followup_generated", False)), summary)
        self.assertEqual(str(summary.get("followup_status", "")), "generated")
        self.assertTrue(
            bool(verification.get("persistent_degradation", False)), verification
        )
        self.assertGreaterEqual(
            int(verification.get("inquiry_candidate_count", 0) or 0), 1
        )
        self.assertEqual(
            set(verification.get("inquiry_candidate_types", [])), surfaced_types
        )
        self.assertTrue(
            bool(verification.get("followup_generated", False)), verification
        )

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective60-test",
                "source": "objective60-inquiry-followup",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = (
            generated.get("questions", []) if isinstance(generated, dict) else []
        )
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
        evidence = (
            question.get("trigger_evidence", {})
            if isinstance(question.get("trigger_evidence", {}), dict)
            else {}
        )
        self.assertEqual(str(evidence.get("managed_scope", "")), scope)
        self.assertGreaterEqual(int(evidence.get("degraded_signal_count", 0) or 0), 1)

        stewardship_id = int(
            (
                cycled.get("stewardship", {})
                if isinstance(cycled.get("stewardship", {}), dict)
                else {}
            ).get("stewardship_id", 0)
            or 0
        )
        self.assertGreater(stewardship_id, 0)
        status, history = get_json(
            "/stewardship/history", {"stewardship_id": stewardship_id, "limit": 10}
        )
        self.assertEqual(status, 200, history)
        history_rows = history.get("history", []) if isinstance(history, dict) else []
        history_cycle = next(
            (
                item
                for item in history_rows
                if isinstance(item, dict)
                and int(item.get("cycle_id", 0) or 0)
                == int(cycle.get("cycle_id", 0) or 0)
            ),
            None,
        )
        self.assertIsNotNone(history_cycle, history_rows)
        self.assertTrue(
            bool((history_cycle or {}).get("persistent_degradation", False)),
            history_cycle,
        )
        self.assertGreaterEqual(
            int((history_cycle or {}).get("inquiry_candidate_count", 0) or 0), 1
        )
        self.assertEqual(
            set((history_cycle or {}).get("inquiry_candidate_types", [])),
            surfaced_types,
        )
        self.assertEqual(
            str((history_cycle or {}).get("followup_status", "")), "generated"
        )

        question_id = int(question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "request_stewardship_improvement",
                "answer_json": {"reason": "capture stewardship policy review"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = (
            answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        )
        self.assertTrue(
            bool(applied_effect.get("improvement_proposal_created", False)),
            applied_effect,
        )
        proposal_id = int(applied_effect.get("improvement_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)

        status, proposal_detail = get_json(f"/improvement/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        proposal = (
            proposal_detail.get("proposal", {})
            if isinstance(proposal_detail, dict)
            else {}
        )
        self.assertEqual(
            str(proposal.get("proposal_type", "")), "capability_workflow_improvement"
        )
        self.assertEqual(
            str(proposal.get("affected_component", "")), "environment_stewardship"
        )

    def test_stabilize_scope_now_creates_pending_workspace_rescan_proposal(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"stewardship-rescan-{run_id}"

        self._seed_stewardship_followup(
            scope=scope,
            run_id=run_id,
            source="objective60-rescan-followup",
        )

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective60-test",
                "source": "objective60-rescan-followup",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = (
            generated.get("questions", []) if isinstance(generated, dict) else []
        )
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

        question_id = int(question.get("question_id", 0) or 0)
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
        applied_effect = (
            answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        )
        self.assertTrue(
            bool(applied_effect.get("workspace_proposal_created", False)),
            applied_effect,
        )
        proposal_id = int(applied_effect.get("workspace_proposal_id", 0) or 0)
        self.assertGreater(proposal_id, 0)

        status, proposal_detail = get_json(f"/workspace/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)
        proposal = proposal_detail if isinstance(proposal_detail, dict) else {}
        self.assertEqual(str(proposal.get("proposal_type", "")), "rescan_zone")
        self.assertEqual(str(proposal.get("status", "")), "pending")
        self.assertEqual(str(proposal.get("related_zone", "")), scope)

        status, next_proposal = get_json(
            "/workspace/proposals/next",
            {"actor": "objective60-test", "reason": "verify_rescan_queue"},
        )
        self.assertEqual(status, 200, next_proposal)
        self.assertTrue(bool(next_proposal.get("selected", False)), next_proposal)

        status, pending_payload = get_json(
            "/workspace/proposals",
            {"status": "pending", "limit": 500},
        )
        self.assertEqual(status, 200, pending_payload)
        pending_rows = (
            pending_payload.get("proposals", [])
            if isinstance(pending_payload, dict)
            else []
        )
        queued = next(
            (
                item
                for item in pending_rows
                if isinstance(item, dict)
                and int(item.get("proposal_id", 0) or 0) == proposal_id
            ),
            None,
        )
        self.assertIsNotNone(queued, pending_rows)
        self.assertEqual(str((queued or {}).get("status", "")), "pending")

    def test_tighten_scope_tracking_updates_stewardship_target(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"stewardship-tighten-{run_id}"

        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)

        status, pref = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "stewardship_priority:default",
                "value": 0.8,
                "confidence": 0.9,
                "source": "objective60-tighten-followup",
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective60-test",
                "source": "objective60-tighten-followup",
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
                "actor": "objective60-test",
                "source": "objective60-tighten-followup",
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
                "actor": "objective60-test",
                "source": "objective60-tighten-followup",
                "managed_scope": scope,
                "stale_after_seconds": 900,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 900,
                    "critical_object_confidence": 0.8,
                    "max_degraded_zones": 0,
                    "max_zone_uncertainty_score": 0.35,
                    "max_system_drift_rate": 0.4,
                    "max_missing_key_objects": 0,
                    "key_objects": [f"obj60-critical-missing-{run_id}"],
                    "intervention_policy": {
                        "max_interventions_per_window": 1,
                        "window_minutes": 180,
                        "scope_cooldown_seconds": 3600,
                        "per_strategy_limit": 1,
                    },
                },
                "metadata_json": {"run_id": run_id, "phase": "tighten"},
            },
        )
        self.assertEqual(status, 200, cycled)
        stewardship = (
            cycled.get("stewardship", {})
            if isinstance(cycled.get("stewardship", {}), dict)
            else {}
        )
        stewardship_id = int(stewardship.get("stewardship_id", 0) or 0)
        self.assertGreater(stewardship_id, 0)
        before_target = (
            stewardship.get("target_environment_state", {})
            if isinstance(stewardship.get("target_environment_state", {}), dict)
            else {}
        )
        self.assertEqual(int(before_target.get("zone_freshness_seconds", 0) or 0), 900)
        self.assertEqual(
            float(before_target.get("max_system_drift_rate", 0.0) or 0.0), 0.4
        )

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective60-test",
                "source": "objective60-tighten-followup",
                "lookback_hours": 24,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = (
            generated.get("questions", []) if isinstance(generated, dict) else []
        )
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

        question_id = int(question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "operator",
                "selected_path_id": "tighten_scope_tracking",
                "answer_json": {"reason": "tighten stewardship thresholds"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = (
            answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        )
        self.assertTrue(
            bool(applied_effect.get("stewardship_target_updated", False)),
            applied_effect,
        )
        updated_target = (
            applied_effect.get("updated_target_environment_state", {})
            if isinstance(
                applied_effect.get("updated_target_environment_state", {}), dict
            )
            else {}
        )
        self.assertEqual(int(updated_target.get("zone_freshness_seconds", 0) or 0), 300)
        self.assertEqual(
            float(updated_target.get("max_system_drift_rate", 0.0) or 0.0), 0.2
        )
        self.assertTrue(bool(updated_target.get("proactive_drift_monitoring", False)))
        self.assertIn(
            f"obj60-critical-missing-{run_id}", updated_target.get("key_objects", [])
        )

        status, detail = get_json(f"/stewardship/{stewardship_id}")
        self.assertEqual(status, 200, detail)
        current = detail.get("stewardship", {}) if isinstance(detail, dict) else {}
        current_target = (
            current.get("target_environment_state", {})
            if isinstance(current.get("target_environment_state", {}), dict)
            else {}
        )
        self.assertEqual(int(current_target.get("zone_freshness_seconds", 0) or 0), 300)
        self.assertEqual(
            float(current_target.get("max_system_drift_rate", 0.0) or 0.0), 0.2
        )
        self.assertTrue(bool(current_target.get("proactive_drift_monitoring", False)))
        self.assertIn(
            f"obj60-critical-missing-{run_id}", current_target.get("key_objects", [])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
