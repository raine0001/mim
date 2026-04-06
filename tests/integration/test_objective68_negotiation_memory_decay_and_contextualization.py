import json
import os
import unittest
import urllib.error
import urllib.request
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


class Objective68NegotiationMemoryDecayAndContextualizationTest(unittest.TestCase):
    def _reset_negotiation_preferences(self) -> None:
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
                "text": f"URGENT: objective68 negotiation memory flow {run_id}",
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
                        "object_label": "objective68-target",
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
                "content": f"Objective68 external context {run_id}",
                "summary": "External context for objective68 negotiation memory",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, run_id: str) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective68-test",
                "reason": "objective68 focused setup",
                "operator_present": True,
                "human_in_workspace": True,
                "shared_workspace_active": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, run_id: str, source: str, environment_profile: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective68-test",
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
                "metadata_json": {
                    "run_id": run_id,
                    "environment_profile": environment_profile,
                },
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

    def _respond(self, negotiation_id: int, option_id: str, run_id: str) -> None:
        status, payload = post_json(
            f"/collaboration/negotiations/{negotiation_id}/respond",
            {
                "actor": "objective68-test",
                "option_id": option_id,
                "reason": f"objective68 choose {option_id}",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _preferences(self, pattern_key: str = "") -> list[dict]:
        query = f"?limit=100&pattern_key={pattern_key}" if pattern_key else "?limit=100"
        status, payload = get_json(f"/collaboration/preferences{query}")
        self.assertEqual(status, 200, payload)
        preferences = payload.get("preferences", []) if isinstance(payload, dict) else []
        self.assertTrue(isinstance(preferences, list))
        return preferences

    def _negotiation_default(self, orchestration: dict) -> str:
        negotiation_id = self._negotiation_id(orchestration)
        self.assertGreater(negotiation_id, 0)
        status, payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
        self.assertEqual(status, 200, payload)
        negotiation = payload.get("negotiation", {}) if isinstance(payload, dict) else {}
        return str(negotiation.get("default_safe_path", ""))

    def test_objective68_negotiation_memory_decay_and_contextualization(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective68-focused-{run_id}"
        zone = f"obj68-zone-{run_id}"

        self._reset_negotiation_preferences()
        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)
        self._set_human_signals(run_id=run_id)

        for _ in range(4):
            orchestration = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_a")
            negotiation_id = self._negotiation_id(orchestration)
            self.assertGreater(negotiation_id, 0)
            status, negotiation_payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
            self.assertEqual(status, 200, negotiation_payload)
            negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}
            if str(negotiation.get("status", "")) == "open":
                self._respond(negotiation_id, "rescan_first", run_id)

        prefs = self._preferences()
        self.assertTrue(bool(prefs))
        warehouse_a_pref = next((item for item in prefs if "env:warehouse_a" in str(item.get("pattern_key", ""))), {})
        self.assertTrue(bool(warehouse_a_pref))
        self.assertEqual(str(warehouse_a_pref.get("state", "")), "consolidated")
        self.assertEqual(str(warehouse_a_pref.get("preferred_option_id", "")), "rescan_first")

        same_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_a")
        self.assertEqual(self._negotiation_default(same_context), "rescan_first")

        different_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_b")
        self.assertNotEqual(self._negotiation_default(different_context), "rescan_first")

        stale_pattern_key = str(warehouse_a_pref.get("pattern_key", ""))
        stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        status, payload = post_json(
            "/preferences",
            {
                "user_id": "operator",
                "preference_type": "collaboration_negotiation_memory",
                "value": {
                    "version": "objective68-v1",
                    "patterns": {
                        stale_pattern_key: {
                            "pattern_key": stale_pattern_key,
                            "state": "consolidated",
                            "evidence_count": 20,
                            "option_counts": {"rescan_first": 20},
                            "dominant_option_id": "rescan_first",
                            "confidence": 1.0,
                            "source_interactions": [],
                            "last_updated_at": stale_timestamp,
                        }
                    },
                },
                "confidence": 1.0,
                "source": "objective68-test-stale-seed",
            },
        )
        self.assertEqual(status, 200, payload)

        stale_inspect = self._preferences(pattern_key=stale_pattern_key)
        self.assertTrue(bool(stale_inspect))
        stale_pref = stale_inspect[0]
        self.assertEqual(str(stale_pref.get("freshness", "")), "stale")
        self.assertTrue(bool(stale_pref.get("decay_applied", False)))
        self.assertGreater(float(stale_pref.get("raw_confidence", 0.0) or 0.0), float(stale_pref.get("confidence", 0.0) or 0.0))
        self.assertEqual(float(stale_pref.get("context_match_score", 0.0) or 0.0), 1.0)

        stale_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_a")
        self.assertNotEqual(self._negotiation_default(stale_context), "rescan_first")


if __name__ == "__main__":
    unittest.main()
