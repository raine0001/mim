import json
import os
import unittest
import urllib.error
import urllib.request
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


class Objective67NegotiationMemoryPreferenceConsolidationTest(unittest.TestCase):
    def _reset_negotiation_preferences(self) -> None:
        for preference_type, value in [
            ("collaboration_negotiation_patterns", {"version": "objective66-v1", "patterns": {}}),
            ("collaboration_negotiation_memory", {"version": "objective67-v1", "patterns": {}}),
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
                "text": f"URGENT: objective67 negotiation memory flow {run_id}",
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
                        "object_label": "objective67-target",
                        "confidence": 0.88,
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
                "content": f"Objective67 external context {run_id}",
                "summary": "External context for objective67 negotiation memory",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, run_id: str) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective67-test",
                "reason": "objective67 focused setup",
                "operator_present": True,
                "human_in_workspace": True,
                "shared_workspace_active": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective67-test",
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

    def _negotiation_id(self, orchestration: dict) -> int:
        artifacts = orchestration.get("downstream_artifacts", []) if isinstance(orchestration.get("downstream_artifacts", []), list) else []
        for item in artifacts:
            if isinstance(item, dict) and str(item.get("artifact_type", "")) == "collaboration_negotiation":
                return int(item.get("artifact_id", 0) or 0)
        return 0

    def _respond(self, negotiation_id: int, option_id: str, run_id: str) -> dict:
        status, payload = post_json(
            f"/collaboration/negotiations/{negotiation_id}/respond",
            {
                "actor": "objective67-test",
                "option_id": option_id,
                "reason": f"objective67 choose {option_id}",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        return payload

    def _preferences(self) -> list[dict]:
        status, payload = get_json("/collaboration/preferences?limit=100")
        self.assertEqual(status, 200, payload)
        preferences = payload.get("preferences", []) if isinstance(payload, dict) else []
        self.assertTrue(isinstance(preferences, list))
        return preferences

    def test_objective67_negotiation_memory_preference_consolidation(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective67-focused-{run_id}"
        zone = f"obj67-zone-{run_id}"

        self._reset_negotiation_preferences()
        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)
        self._set_human_signals(run_id=run_id)

        first = self._build_orchestration(run_id=run_id, source=source)
        first_negotiation_id = self._negotiation_id(first)
        self.assertGreater(first_negotiation_id, 0)
        self._respond(first_negotiation_id, "rescan_first", run_id)

        low_evidence = self._preferences()
        self.assertTrue(bool(low_evidence))
        first_pref = low_evidence[0] if low_evidence else {}
        self.assertNotEqual(str(first_pref.get("state", "")), "consolidated")

        for _ in range(3):
            orchestration = self._build_orchestration(run_id=run_id, source=source)
            negotiation_id = self._negotiation_id(orchestration)
            self.assertGreater(negotiation_id, 0)
            status, negotiation_payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
            self.assertEqual(status, 200, negotiation_payload)
            negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}
            if str(negotiation.get("status", "")) == "open":
                self._respond(negotiation_id, "rescan_first", run_id)

        consolidated = self._preferences()
        self.assertTrue(bool(consolidated))
        pref = consolidated[0]
        self.assertEqual(str(pref.get("state", "")), "consolidated")
        self.assertEqual(str(pref.get("preferred_option_id", "")), "rescan_first")
        self.assertGreaterEqual(int(pref.get("evidence_count", 0) or 0), 4)
        previous_confidence = float(pref.get("confidence", 0.0) or 0.0)

        influenced = self._build_orchestration(run_id=run_id, source=source)
        influenced_negotiation_id = self._negotiation_id(influenced)
        self.assertGreater(influenced_negotiation_id, 0)
        status, influenced_payload = get_json(f"/collaboration/negotiations/{influenced_negotiation_id}")
        self.assertEqual(status, 200, influenced_payload)
        influenced_negotiation = influenced_payload.get("negotiation", {}) if isinstance(influenced_payload, dict) else {}
        self.assertEqual(str(influenced_negotiation.get("default_safe_path", "")), "rescan_first")

        for _ in range(5):
            orchestration = self._build_orchestration(run_id=run_id, source=source)
            negotiation_id = self._negotiation_id(orchestration)
            self.assertGreater(negotiation_id, 0)
            status, negotiation_payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
            self.assertEqual(status, 200, negotiation_payload)
            negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}
            if str(negotiation.get("status", "")) == "open":
                self._respond(negotiation_id, "defer_action", run_id)

        revised = self._preferences()
        self.assertTrue(bool(revised))
        revised_pref = revised[0]
        revised_option = str(revised_pref.get("preferred_option_id", ""))
        revised_confidence = float(revised_pref.get("confidence", 0.0) or 0.0)
        revised_state = str(revised_pref.get("state", ""))

        self.assertTrue(
            revised_option == "defer_action"
            or revised_state == "learning"
            or revised_confidence < previous_confidence
        )

        self.assertTrue(bool(revised_pref.get("source_interactions", [])))


if __name__ == "__main__":
    unittest.main()
