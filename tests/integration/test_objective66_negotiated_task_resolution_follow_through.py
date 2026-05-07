import json
import os
import unittest
import urllib.error
import urllib.request
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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective66NegotiatedTaskResolutionFollowThroughTest(unittest.TestCase):
    def _reset_negotiation_patterns(self) -> None:
        for preference_type, value in [
            ("collaboration_negotiation_patterns", {"version": "objective66-v1", "patterns": {}}),
            ("collaboration_negotiation_memory", {"version": "objective68-v1", "patterns": {}}),
        ]:
            status, payload = post_json(
                "/preferences",
                {
                    "user_id": "operator",
                    "preference_type": preference_type,
                    "value": value,
                    "confidence": 0.0,
                    "source": "test_reset",
                },
            )
            self.assertEqual(status, 200, payload)

    def _seed_cross_domain_inputs(self, run_id: str, zone: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"URGENT: objective66 negotiation flow {run_id}",
                "parsed_intent": "operator_urgent_request",
                "confidence": 0.97,
                "metadata_json": {"run_id": run_id, "urgency": "high"},
            },
        )
        self.assertEqual(status, 200, event)

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-{run_id}",
                "source_type": "camera",
                "session_id": run_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 20,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"run_id": run_id},
                "observations": [
                    {
                        "object_label": "objective66-target",
                        "confidence": 0.89,
                        "zone": zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, memory = post_json(
            "/memory",
            {
                "memory_class": "external_signal",
                "content": f"Objective66 external context {run_id}",
                "summary": "External context for objective66 negotiation learning",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, *, run_id: str) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective66-test",
                "reason": "objective66 focused setup",
                "operator_present": True,
                "human_in_workspace": True,
                "shared_workspace_active": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, *, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective66-test",
                "source": source,
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.3,
                "min_domains_required": 2,
                "dependency_resolution_policy": "ask",
                "collaboration_mode_preference": "autonomous",
                "task_kind": "physical",
                "action_risk_level": "high",
                "use_human_aware_signals": True,
                "generate_goal": True,
                "generate_horizon_plan": True,
                "generate_improvement_proposals": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        orchestration = payload.get("orchestration", {}) if isinstance(payload, dict) else {}
        self.assertTrue(bool(orchestration))
        return orchestration

    def _negotiation_id_from_orchestration(self, orchestration: dict) -> int:
        artifacts = orchestration.get("downstream_artifacts", []) if isinstance(orchestration.get("downstream_artifacts", []), list) else []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            if str(item.get("artifact_type", "")) == "collaboration_negotiation":
                return int(item.get("artifact_id", 0) or 0)
        return 0

    def _respond(self, negotiation_id: int, run_id: str) -> dict:
        status, response_payload = post_json(
            f"/collaboration/negotiations/{negotiation_id}/respond",
            {
                "actor": "objective66-test",
                "option_id": "rescan_first",
                "reason": "prefer validation-first negotiated path",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, response_payload)
        return response_payload

    def test_objective66_negotiated_task_resolution_follow_through(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective66-focused-{run_id}"
        zone = f"obj66-zone-{run_id}"

        self._reset_negotiation_patterns()
        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)
        self._set_human_signals(run_id=run_id)

        first = self._build_orchestration(run_id=run_id, source=source)
        first_negotiation_id = self._negotiation_id_from_orchestration(first)
        self.assertGreater(first_negotiation_id, 0)
        first_response = self._respond(first_negotiation_id, run_id)
        self.assertEqual(
            str((first_response.get("orchestration", {}) if isinstance(first_response, dict) else {}).get("status", "")),
            "replan_required",
        )

        second = self._build_orchestration(run_id=run_id, source=source)
        second_negotiation_id = self._negotiation_id_from_orchestration(second)
        self.assertGreater(second_negotiation_id, 0)
        second_response = self._respond(second_negotiation_id, run_id)
        self.assertEqual(
            str((second_response.get("orchestration", {}) if isinstance(second_response, dict) else {}).get("status", "")),
            "replan_required",
        )

        third = self._build_orchestration(run_id=run_id, source=source)
        third_negotiation_id = self._negotiation_id_from_orchestration(third)
        self.assertGreater(third_negotiation_id, 0)
        self.assertEqual(str(third.get("status", "")), "replan_required")

        status, negotiation_payload = get_json(f"/collaboration/negotiations/{third_negotiation_id}")
        self.assertEqual(status, 200, negotiation_payload)
        negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}
        self.assertEqual(str(negotiation.get("status", "")), "resolved")
        self.assertEqual(str(negotiation.get("resolution_status", "")), "reused_prior_pattern")
        self.assertEqual(str(negotiation.get("selected_option_id", "")), "rescan_first")

        applied_effect = negotiation.get("applied_effect", {}) if isinstance(negotiation.get("applied_effect", {}), dict) else {}
        follow_through = applied_effect.get("follow_through", {}) if isinstance(applied_effect.get("follow_through", {}), dict) else {}
        self.assertEqual(str(follow_through.get("selected_option_id", "")), "rescan_first")
        self.assertIn("updated_horizon_plan", follow_through)

        status, pref_payload = get_json("/preferences/collaboration_negotiation_patterns")
        self.assertEqual(status, 200, pref_payload)
        value = pref_payload.get("value", {}) if isinstance(pref_payload, dict) else {}
        patterns = value.get("patterns", {}) if isinstance(value.get("patterns", {}), dict) else {}
        self.assertTrue(bool(patterns))


if __name__ == "__main__":
    unittest.main()
