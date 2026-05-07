import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tests.integration.operator_resolution_test_utils import age_objective88_preferences, cleanup_objective88_rows
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


class Objective88OperatorPreferencePolicyConvergenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 88",
            base_url=BASE_URL,
            require_ui_state=True,
        )
        cleanup_objective88_rows()

    def setUp(self) -> None:
        cleanup_objective88_rows()

    def tearDown(self) -> None:
        cleanup_objective88_rows()

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
                "text": f"objective88 stale scan {run_id}",
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
                            "label": f"obj88-stale-{run_id}",
                            "zone": zone,
                            "confidence": 0.91,
                            "observed_at": stale_time,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _seed_prereqs(self, *, scope: str, run_id: str, source: str) -> None:
        self._register_workspace_scan()
        self._create_stale_observation(zone=scope, run_id=run_id)
        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective88-test",
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

    def _create_commitment(self, *, scope: str, run_id: str, decision_type: str) -> int:
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective88-test-operator",
                "managed_scope": scope,
                "decision_type": decision_type,
                "reason": "objective88 convergence probe",
                "recommendation_snapshot_json": {
                    "recommendation": "keep autonomous remediation deferred",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.95,
                "duration_seconds": 7200,
                "provenance_json": {"source": "objective88-focused", "run_id": run_id},
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                    "maintenance_mode": "deferred",
                    "stewardship_defer_actions": True,
                    "strategy_priority_mode": "prefer_stabilization",
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        commitment = created.get("commitment", {}) if isinstance(created, dict) else {}
        commitment_id = int(commitment.get("commitment_id", 0) or 0)
        self.assertGreater(commitment_id, 0)
        return commitment_id

    def _resolve_commitment(self, *, commitment_id: int, source: str, target_status: str, run_id: str) -> None:
        status, resolved = post_json(
            f"/operator/resolution-commitments/{commitment_id}/resolve",
            {
                "actor": "objective88-test-operator",
                "source": source,
                "target_status": target_status,
                "reason": f"objective88 terminal {target_status}",
                "lookback_hours": 24,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, resolved)

    def test_repeated_satisfied_outcomes_converge_and_influence_downstream_surfaces(self) -> None:
        scope = f"objective88-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective88-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)

        for _ in range(3):
            commitment_id = self._create_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="require_additional_evidence",
            )
            self._resolve_commitment(
                commitment_id=commitment_id,
                source=source,
                target_status="satisfied",
                run_id=run_id,
            )

        status, converged = post_json(
            "/operator/preferences/converge",
            {
                "actor": "objective88-test",
                "source": "objective88",
                "managed_scope": scope,
                "lookback_hours": 168,
                "min_evidence": 3,
            },
        )
        self.assertEqual(status, 200, converged)
        preferences = converged.get("preferences", []) if isinstance(converged, dict) else []
        self.assertTrue(preferences, converged)
        first = preferences[0] if isinstance(preferences[0], dict) else {}
        self.assertEqual(str(first.get("preference_direction", "")), "reinforce")
        self.assertEqual(int(first.get("success_count", 0) or 0), 3)
        self.assertEqual(str(first.get("managed_scope", "")), scope)

        status, listed = get_json("/operator/preferences", {"managed_scope": scope})
        self.assertEqual(status, 200, listed)
        listed_preferences = listed.get("preferences", []) if isinstance(listed, dict) else []
        self.assertTrue(listed_preferences, listed)

        status, strategy_payload = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective88-test",
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
        self.assertEqual(status, 200, strategy_payload)
        goals = strategy_payload.get("goals", []) if isinstance(strategy_payload, dict) else []
        self.assertTrue(goals, strategy_payload)
        first_goal = goals[0] if isinstance(goals[0], dict) else {}
        self.assertIn("operator_learned_preference_influence", first_goal, first_goal)

        status, autonomy_payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective88-test",
                "source": source,
                "scope": scope,
                "lookback_hours": 168,
                "min_samples": 1,
                "apply_recommended_boundaries": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, autonomy_payload)
        boundary = autonomy_payload.get("boundary", {}) if isinstance(autonomy_payload, dict) else {}
        self.assertIn("operator_learned_preference", boundary.get("adaptation_reasoning", {}), boundary)

        status, stewardship_payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective88-test",
                "source": source,
                "managed_scope": scope,
                "lookback_hours": 168,
                "max_strategies": 5,
                "max_actions": 5,
                "auto_execute": False,
                "force_degraded": False,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, stewardship_payload)
        summary = stewardship_payload.get("summary", {}) if isinstance(stewardship_payload, dict) else {}
        integrations = summary.get("integrations", {}) if isinstance(summary.get("integrations", {}), dict) else {}
        self.assertGreater(float(integrations.get("operator_preference_weight", 0.0) or 0.0), 0.5)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        learned_preferences = operator_reasoning.get("learned_preferences", []) if isinstance(operator_reasoning.get("learned_preferences", []), list) else []
        self.assertTrue(learned_preferences, operator_reasoning)

    def test_repeated_ineffective_outcomes_converge_to_avoidance(self) -> None:
        scope = f"objective88-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective88-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)

        for _ in range(3):
            commitment_id = self._create_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="defer_action",
            )
            self._resolve_commitment(
                commitment_id=commitment_id,
                source=source,
                target_status="ineffective",
                run_id=run_id,
            )

        status, converged = post_json(
            "/operator/preferences/converge",
            {
                "actor": "objective88-test",
                "source": "objective88",
                "managed_scope": scope,
                "lookback_hours": 168,
                "min_evidence": 3,
            },
        )
        self.assertEqual(status, 200, converged)
        preferences = converged.get("preferences", []) if isinstance(converged, dict) else []
        self.assertTrue(preferences, converged)
        first = preferences[0] if isinstance(preferences[0], dict) else {}
        self.assertEqual(str(first.get("preference_direction", "")), "avoid")
        self.assertEqual(int(first.get("failure_count", 0) or 0), 3)

    def test_conflicting_preferences_are_arbitrated_and_losing_signal_stays_inspectable(self) -> None:
        scope = f"objective88-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective88-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)

        for _ in range(3):
            commitment_id = self._create_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="require_additional_evidence",
            )
            self._resolve_commitment(
                commitment_id=commitment_id,
                source=source,
                target_status="satisfied",
                run_id=run_id,
            )

        for _ in range(3):
            commitment_id = self._create_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="defer_action",
            )
            self._resolve_commitment(
                commitment_id=commitment_id,
                source=source,
                target_status="ineffective",
                run_id=run_id,
            )

        status, converged = post_json(
            "/operator/preferences/converge",
            {
                "actor": "objective88-test",
                "source": "objective88",
                "managed_scope": scope,
                "lookback_hours": 168,
                "min_evidence": 3,
            },
        )
        self.assertEqual(status, 200, converged)

        status, listed = get_json("/operator/preferences", {"managed_scope": scope, "limit": 10})
        self.assertEqual(status, 200, listed)
        preferences = listed.get("preferences", []) if isinstance(listed, dict) else []
        self.assertEqual(len(preferences), 2, preferences)
        winner = next(
            (item for item in preferences if str(item.get("arbitration_state", "")) == "won_scope"),
            {},
        )
        loser = next(
            (item for item in preferences if str(item.get("arbitration_state", "")) == "lost_scope_conflict"),
            {},
        )
        self.assertTrue(winner, preferences)
        self.assertTrue(loser, preferences)
        self.assertNotEqual(
            str(winner.get("preference_key", "")),
            str(loser.get("preference_key", "")),
        )
        self.assertTrue(
            str(winner.get("precedence_rule", "")) in {"scope_effective_strength", "operator_commitment_precedence"},
            winner,
        )

        winner_reasoning = winner.get("arbitration_reasoning_json", {}) if isinstance(winner.get("arbitration_reasoning_json", {}), dict) else {}
        loser_reasoning = loser.get("arbitration_reasoning_json", {}) if isinstance(loser.get("arbitration_reasoning_json", {}), dict) else {}
        self.assertTrue(str(winner_reasoning.get("reason", "")).strip(), winner)
        self.assertTrue(str(loser_reasoning.get("reason", "")).strip(), loser)
        self.assertEqual(
            str(loser_reasoning.get("winning_preference_key", "")),
            str(winner.get("preference_key", "")),
        )

    def test_stale_preferences_are_demoted_until_reinforced(self) -> None:
        scope = f"objective88-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective88-{run_id}"
        self._seed_prereqs(scope=scope, run_id=run_id, source=source)

        for _ in range(3):
            commitment_id = self._create_commitment(
                scope=scope,
                run_id=run_id,
                decision_type="require_additional_evidence",
            )
            self._resolve_commitment(
                commitment_id=commitment_id,
                source=source,
                target_status="satisfied",
                run_id=run_id,
            )

        status, converged = post_json(
            "/operator/preferences/converge",
            {
                "actor": "objective88-test",
                "source": "objective88",
                "managed_scope": scope,
                "lookback_hours": 168,
                "min_evidence": 3,
            },
        )
        self.assertEqual(status, 200, converged)

        age_objective88_preferences(managed_scope=scope, hours=800)

        status, listed = get_json("/operator/preferences", {"managed_scope": scope, "limit": 10})
        self.assertEqual(status, 200, listed)
        preferences = listed.get("preferences", []) if isinstance(listed, dict) else []
        self.assertTrue(preferences, listed)
        self.assertEqual(str(preferences[0].get("arbitration_state", "")), "stale_signal")
        self.assertEqual(str(preferences[0].get("precedence_rule", "")), "freshness_decay")


if __name__ == "__main__":
    unittest.main()