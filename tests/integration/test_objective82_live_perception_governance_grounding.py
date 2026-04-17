import json
import os
import urllib.error
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


class Objective82LivePerceptionGovernanceGroundingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 82",
            base_url=BASE_URL,
            require_governance=True,
        )

    def _register_capability(self, *, capability_name: str) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 82 live perception governance probe",
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
        simulation_match_status: str,
        environment_shift_detected: bool,
    ) -> int:
        capability_name = f"objective82_truth_probe_{run_id}_{suffix}"
        self._register_capability(capability_name=capability_name)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective82 governance probe {run_id} {suffix}",
                "parsed_intent": "workspace_check",
                "requested_goal": "collect execution truth for perception grounding",
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
                    "actual_duration_ms": 980,
                    "duration_delta_ratio": round((980 - 900) / 900.0, 6),
                    "retry_count": 0,
                    "fallback_used": False,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": environment_shift_detected,
                    "simulation_match_status": simulation_match_status,
                    "truth_confidence": 0.93,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        self.assertEqual(status, 200, payload)
        return execution_id

    def test_objective82_fresh_camera_grounding_marks_world_drift(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective82-world-{run_id}"
        camera_label = f"objective82-marker-{run_id}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-obj82-{run_id}",
                "source_type": "camera",
                "session_id": f"session-{run_id}",
                "observations": [
                    {
                        "object_label": camera_label,
                        "confidence": 0.97,
                        "zone": f"bench-{run_id}",
                    }
                ],
                "observation_confidence_floor": 0.5,
                "metadata_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, camera)
        self.assertEqual(str(camera.get("status", "")), "accepted")

        for suffix in ["a", "b"]:
            self._seed_execution_truth(
                scope=scope,
                run_id=run_id,
                suffix=suffix,
                simulation_match_status="mismatch",
                environment_shift_detected=True,
            )

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective82-test",
                "source": "objective82-live-perception-grounding",
                "managed_scope": scope,
                "lookback_hours": 24,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)
        governance = governance_payload.get("governance", {})
        trigger_evidence = (
            governance.get("trigger_evidence", {})
            if isinstance(governance.get("trigger_evidence", {}), dict)
            else {}
        )
        perception_grounding = (
            trigger_evidence.get("perception_grounding", {})
            if isinstance(trigger_evidence.get("perception_grounding", {}), dict)
            else {}
        )

        self.assertIn(
            str(trigger_evidence.get("perception_grounding_classification", "")),
            {"world_drift", "mixed"},
            governance,
        )
        self.assertEqual(
            str(governance.get("governance_decision", "")),
            "require_sandbox_experiment",
            governance,
        )
        self.assertGreater(
            float(perception_grounding.get("camera_grounding_weight", 0.0) or 0.0),
            0.55,
            governance,
        )
        latest_camera = (
            perception_grounding.get("latest_camera", {})
            if isinstance(perception_grounding.get("latest_camera", {}), dict)
            else {}
        )
        self.assertIn(camera_label, latest_camera.get("labels", []), governance)

    def test_objective82_noisy_perception_does_not_overreact(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"objective82-noise-{run_id}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-noise-obj82-{run_id}",
                "source_type": "camera",
                "session_id": f"session-{run_id}",
                "observations": [
                    {
                        "object_label": f"blurred-object-{run_id}",
                        "confidence": 0.18,
                        "zone": f"shadow-zone-{run_id}",
                    }
                ],
                "observation_confidence_floor": 0.5,
                "metadata_json": {"managed_scope": scope, "run_id": run_id},
            },
        )
        self.assertEqual(status, 200, camera)
        self.assertEqual(str(camera.get("status", "")), "discarded_low_confidence")

        for suffix in ["a", "b"]:
            self._seed_execution_truth(
                scope=scope,
                run_id=run_id,
                suffix=suffix,
                simulation_match_status="mismatch",
                environment_shift_detected=False,
            )

        status, governance_payload = post_json(
            "/execution-truth/governance/evaluate",
            {
                "actor": "objective82-test",
                "source": "objective82-live-perception-grounding",
                "managed_scope": scope,
                "lookback_hours": 24,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, governance_payload)
        governance = governance_payload.get("governance", {})
        trigger_evidence = (
            governance.get("trigger_evidence", {})
            if isinstance(governance.get("trigger_evidence", {}), dict)
            else {}
        )
        perception_grounding = (
            trigger_evidence.get("perception_grounding", {})
            if isinstance(trigger_evidence.get("perception_grounding", {}), dict)
            else {}
        )

        self.assertEqual(
            str(trigger_evidence.get("perception_grounding_classification", "")),
            "sensor_noise",
            governance,
        )
        self.assertEqual(
            str(governance.get("governance_decision", "")),
            "increase_visibility",
            governance,
        )
        self.assertGreaterEqual(
            float(perception_grounding.get("sensor_noise_weight", 0.0) or 0.0),
            0.6,
            governance,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)