import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective58AdaptiveAutonomyBoundariesTest(unittest.TestCase):
    def _recompute(self, *, run_id: str, scope: str, min_samples: int, apply: bool, evidence: dict, hard_violations: dict | None = None) -> dict:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective58-test",
                "source": "objective58-focused",
                "scope": scope,
                "lookback_hours": 48,
                "min_samples": min_samples,
                "apply_recommended_boundaries": apply,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    **evidence,
                    "hard_ceiling_violations": hard_violations or {},
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
        self.assertTrue(isinstance(boundary, dict), payload)
        self.assertGreater(int(boundary.get("boundary_id", 0) or 0), 0)
        return boundary

    def test_objective58_adaptive_autonomy_boundaries(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"workspace-default-{run_id}"

        status, override = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "objective58 baseline",
                "auto_execution_enabled": True,
                "force_manual_approval": True,
                "max_auto_actions_per_minute": 2,
                "max_auto_tasks_per_window": 2,
                "auto_window_seconds": 60,
                "cooldown_between_actions_seconds": 8,
                "auto_safe_confidence_threshold": 0.7,
                "auto_preferred_confidence_threshold": 0.7,
                "low_risk_score_max": 0.22,
                "max_autonomy_retries": 1,
                "reset_auto_history": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, override)

        raised = self._recompute(
            run_id=run_id,
            scope=scope,
            min_samples=5,
            apply=True,
            evidence={
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
        )
        self.assertEqual(str(raised.get("current_level", "")), "bounded_auto")
        self.assertIn("raise", str(raised.get("adjustment_reason", "")))

        lowered = self._recompute(
            run_id=run_id,
            scope=scope,
            min_samples=5,
            apply=True,
            evidence={
                "sample_count": 20,
                "success_rate": 0.3,
                "escalation_rate": 0.55,
                "retry_rate": 0.45,
                "interruption_rate": 0.5,
                "memory_delta_rate": 0.2,
                "override_rate": 0.6,
                "replan_rate": 0.5,
                "environment_stability": 0.2,
                "development_confidence": 0.3,
                "constraint_reliability": 0.35,
                "experiment_confidence": 0.2,
            },
        )
        self.assertEqual(str(lowered.get("current_level", "")), "operator_required")
        self.assertIn("lower", str(lowered.get("adjustment_reason", "")))

        hard_capped = self._recompute(
            run_id=run_id,
            scope=scope,
            min_samples=5,
            apply=True,
            evidence={
                "sample_count": 20,
                "success_rate": 0.99,
                "escalation_rate": 0.0,
                "retry_rate": 0.0,
                "interruption_rate": 0.0,
                "memory_delta_rate": 0.9,
                "override_rate": 0.0,
                "replan_rate": 0.0,
                "environment_stability": 0.95,
                "development_confidence": 0.9,
                "constraint_reliability": 0.95,
                "experiment_confidence": 0.95,
            },
            hard_violations={"human_safety": True},
        )
        self.assertEqual(str(hard_capped.get("current_level", "")), "operator_required")
        hard_reasoning = hard_capped.get("adaptation_reasoning", {}) if isinstance(hard_capped.get("adaptation_reasoning", {}), dict) else {}
        self.assertEqual(str(hard_reasoning.get("decision", "")), "hard_ceiling_enforced")

        before_level = str(hard_capped.get("current_level", ""))
        low_quality = self._recompute(
            run_id=run_id,
            scope=scope,
            min_samples=25,
            apply=True,
            evidence={
                "sample_count": 2,
                "success_rate": 1.0,
                "escalation_rate": 0.0,
                "retry_rate": 0.0,
                "interruption_rate": 0.0,
                "memory_delta_rate": 1.0,
                "override_rate": 0.0,
                "replan_rate": 0.0,
                "environment_stability": 1.0,
                "development_confidence": 1.0,
                "constraint_reliability": 1.0,
                "experiment_confidence": 1.0,
            },
        )
        self.assertEqual(str(low_quality.get("current_level", "")), before_level)
        self.assertIn("hold", str(low_quality.get("adjustment_reason", "")))
        evidence_inputs = low_quality.get("evidence_inputs", {}) if isinstance(low_quality.get("evidence_inputs", {}), dict) else {}
        self.assertIn("override_rate", evidence_inputs)
        self.assertIn("constraint_reliability", evidence_inputs)
        self.assertIn("environment_stability", evidence_inputs)
        self.assertIn("experiment_confidence", evidence_inputs)

        boundary_id = int(low_quality.get("boundary_id", 0) or 0)
        self.assertGreater(boundary_id, 0)

        status, policy = get_json("/workspace/autonomy/policy")
        self.assertEqual(status, 200, policy)
        active_autonomy = policy.get("autonomy", {}) if isinstance(policy, dict) else {}
        applied_boundaries = low_quality.get("applied_boundaries", {}) if isinstance(low_quality.get("applied_boundaries", {}), dict) else {}
        if applied_boundaries:
            self.assertEqual(
                int(active_autonomy.get("max_auto_tasks_per_window", 0) or 0),
                int(applied_boundaries.get("max_auto_tasks_per_window", 0) or 0),
            )
        else:
            self.assertEqual(str(low_quality.get("profile_status", "")), "evaluated")

        status, listed = get_json("/autonomy/boundaries", {"scope": scope, "limit": 50})
        self.assertEqual(status, 200, listed)
        rows = listed.get("boundaries", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("boundary_id", 0) or 0) == boundary_id for item in rows if isinstance(item, dict)))

        status, detail = get_json(f"/autonomy/boundaries/{boundary_id}")
        self.assertEqual(status, 200, detail)
        detail_boundary = detail.get("boundary", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_boundary.get("boundary_id", 0) or 0), boundary_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
