import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from tests.integration.operator_resolution_test_utils import cleanup_objective86_rows
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


class Objective86CommitmentEnforcementDriftMonitoringLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 86",
            base_url=BASE_URL,
            require_ui_state=True,
        )
        cleanup_objective86_rows()

    def setUp(self) -> None:
        cleanup_objective86_rows()

    def tearDown(self) -> None:
        cleanup_objective86_rows()

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
                "text": f"objective86 stale scan {run_id}",
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
                            "label": f"obj86-stale-{run_id}",
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
                "value": 0.82,
                "confidence": 0.9,
                "source": source,
            },
        )
        self.assertEqual(status, 200, pref)

        status, goals = post_json(
            "/strategy/goals/build",
            {
                "actor": "objective86-test",
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

    def _create_commitment(self, *, scope: str, run_id: str) -> int:
        status, created = post_json(
            "/operator/resolution-commitments",
            {
                "actor": "objective86-test-operator",
                "managed_scope": scope,
                "decision_type": "require_additional_evidence",
                "reason": "hold auto-execution until the drift pattern is verified",
                "recommendation_snapshot_json": {
                    "recommendation": "keep auto-remediation deferred",
                    "governance_decision": "increase_visibility",
                },
                "authority_level": "temporary_safety_hold",
                "confidence": 0.94,
                "duration_seconds": 7200,
                "provenance_json": {"source": "objective86-focused", "run_id": run_id},
                "downstream_effects_json": {
                    "suppress_duplicate_inquiry": True,
                    "maintenance_mode": "deferred",
                    "stewardship_defer_actions": True,
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        commitment = created.get("commitment", {}) if isinstance(created, dict) else {}
        commitment_id = int(commitment.get("commitment_id", 0) or 0)
        self.assertGreater(commitment_id, 0, created)
        return commitment_id

    def _run_stewardship_cycle(self, *, scope: str, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective86-test",
                "source": source,
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
                    "key_objects": [f"objective86-missing-{run_id}"],
                },
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, payload)
        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        self.assertTrue(
            bool(summary.get("operator_resolution_blocked_auto_execution", False)),
            payload,
        )
        return payload

    def _prepare_monitoring_fixture(self) -> tuple[str, str, str, int]:
        scope = f"objective86-zone-{uuid4().hex[:8]}"
        run_id = uuid4().hex[:10]
        source = f"objective86-{run_id}"
        self._seed_stewardship_prereqs(scope=scope, run_id=run_id, source=source)
        commitment_id = self._create_commitment(scope=scope, run_id=run_id)
        for _ in range(3):
            self._run_stewardship_cycle(scope=scope, run_id=run_id, source=source)
        return scope, run_id, source, commitment_id

    def test_commitment_monitoring_endpoints_and_ui_surface(self) -> None:
        scope, run_id, _, commitment_id = self._prepare_monitoring_fixture()

        status, evaluated = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective86-test",
                "source": "objective86-monitoring",
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, evaluated)
        monitoring = evaluated.get("monitoring", {}) if isinstance(evaluated, dict) else {}
        monitoring_id = int(monitoring.get("monitoring_id", 0) or 0)
        self.assertGreater(monitoring_id, 0, evaluated)
        self.assertEqual(str(monitoring.get("managed_scope", "")), scope)
        self.assertGreaterEqual(int(monitoring.get("blocked_auto_execution_count", 0) or 0), 3)
        self.assertGreaterEqual(float(monitoring.get("drift_score", 0.0) or 0.0), 0.45)
        self.assertIn(
            str(monitoring.get("governance_state", "")),
            {"watch", "drifting"},
            monitoring,
        )

        status, listed = get_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring",
            {"limit": 10},
        )
        self.assertEqual(status, 200, listed)
        rows = listed.get("monitoring", []) if isinstance(listed, dict) else []
        self.assertTrue(
            any(
                isinstance(item, dict)
                and int(item.get("monitoring_id", 0) or 0) == monitoring_id
                for item in rows
            ),
            listed,
        )

        status, detail = get_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/{monitoring_id}"
        )
        self.assertEqual(status, 200, detail)
        detail_monitoring = detail.get("monitoring", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_monitoring.get("monitoring_id", 0) or 0), monitoring_id)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = (
            ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        )
        commitment = operator_reasoning.get("resolution_commitment", {})
        monitoring_snapshot = operator_reasoning.get("commitment_monitoring", {})
        recommendation = operator_reasoning.get("current_recommendation", {})
        self.assertEqual(int(commitment.get("commitment_id", 0) or 0), commitment_id)
        self.assertEqual(int(monitoring_snapshot.get("monitoring_id", 0) or 0), monitoring_id)
        self.assertIn(
            str(monitoring_snapshot.get("governance_state", "")),
            {"watch", "drifting"},
            monitoring_snapshot,
        )
        self.assertEqual(str(recommendation.get("source", "")), "commitment_monitoring")

    def test_commitment_drift_inquiry_can_revoke_commitment(self) -> None:
        _, run_id, source, commitment_id = self._prepare_monitoring_fixture()

        status, evaluated = post_json(
            f"/operator/resolution-commitments/{commitment_id}/monitoring/evaluate",
            {
                "actor": "objective86-test",
                "source": "objective86-monitoring",
                "lookback_hours": 168,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, evaluated)

        status, generated = post_json(
            "/inquiry/questions/generate",
            {
                "actor": "objective86-test",
                "source": source,
                "lookback_hours": 168,
                "max_questions": 10,
                "min_soft_friction_count": 3,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, generated)
        questions = generated.get("questions", []) if isinstance(generated, dict) else []
        drift_question = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and str(item.get("trigger_type", "")) == "operator_commitment_drift_detected"
            ),
            None,
        )
        self.assertIsNotNone(drift_question, generated)

        question_id = int(drift_question.get("question_id", 0) or 0)
        self.assertGreater(question_id, 0)
        status, answered = post_json(
            f"/inquiry/questions/{question_id}/answer",
            {
                "actor": "objective86-test-operator",
                "selected_path_id": "revoke_commitment",
                "answer_json": {"reason": "drift review confirmed the hold is no longer useful"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, answered)
        applied_effect = answered.get("applied_effect", {}) if isinstance(answered, dict) else {}
        self.assertTrue(bool(applied_effect.get("commitment_status_updated", False)), answered)
        self.assertEqual(str(applied_effect.get("commitment_status", "")), "revoked")

        status, fetched = get_json(f"/operator/resolution-commitments/{commitment_id}")
        self.assertEqual(status, 200, fetched)
        commitment = fetched.get("commitment", {}) if isinstance(fetched, dict) else {}
        self.assertEqual(str(commitment.get("status", "")), "revoked")


if __name__ == "__main__":
    unittest.main()