import json
import os
import unittest
import urllib.error
import urllib.parse
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


class Objective70CollaborationStrategyProfilesTest(unittest.TestCase):
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
                "text": f"URGENT: objective70 collaboration profile flow {run_id}",
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
                        "object_label": "objective70-target",
                        "confidence": 0.9,
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
                "content": f"Objective70 external context {run_id}",
                "summary": "External context for objective70 profile synthesis",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, run_id: str) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective70-test",
                "reason": "objective70 focused setup",
                "operator_present": True,
                "human_in_workspace": False,
                "shared_workspace_active": False,
                "occupied_zones": [],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, run_id: str, source: str, environment_profile: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective70-test",
                "source": source,
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.3,
                "min_domains_required": 2,
                "dependency_resolution_policy": "ask",
                "collaboration_mode_preference": "auto",
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
                "actor": "objective70-test",
                "option_id": option_id,
                "reason": f"objective70 choose {option_id}",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _context_scope(self, env: str) -> str:
        return (
            "collaboration_negotiation|physical|high|shared:False|operator:True|"
            f"urgency:high|env:{env}"
        )

    def test_objective70_collaboration_strategy_profiles(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective70-focused-{run_id}"
        zone = f"obj70-zone-{run_id}"

        self._reset_negotiation_preferences()
        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)
        self._set_human_signals(run_id=run_id)

        for _ in range(4):
            orchestration = self._build_orchestration(run_id=run_id, source=source, environment_profile="lab_a")
            negotiation_id = self._negotiation_id(orchestration)
            self.assertGreater(negotiation_id, 0)
            status, negotiation_payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
            self.assertEqual(status, 200, negotiation_payload)
            negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}
            if str(negotiation.get("status", "")) == "open":
                self._respond(negotiation_id, "rescan_first", run_id)

        scope = self._context_scope("lab_a")
        status, recompute = post_json(
            "/collaboration/profiles/recompute",
            {
                "actor": "objective70-test",
                "context_scope": scope,
                "limit": 20,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, recompute)
        self.assertGreaterEqual(int(recompute.get("recomputed", 0) or 0), 1)

        encoded_scope = urllib.parse.quote(scope, safe="")
        status, profiles_payload = get_json(f"/collaboration/profiles?context_scope={encoded_scope}&limit=20")
        self.assertEqual(status, 200, profiles_payload)
        profiles = profiles_payload.get("profiles", []) if isinstance(profiles_payload, dict) else []
        self.assertTrue(bool(profiles))

        profile = profiles[0]
        self.assertEqual(str(profile.get("context_scope", "")), scope)
        self.assertGreaterEqual(int(profile.get("evidence_count", 0) or 0), 4)
        self.assertGreaterEqual(float(profile.get("confidence", 0.0) or 0.0), 0.72)
        self.assertTrue(str(profile.get("status", "")) in {"consolidated", "learning"})
        self.assertTrue(bool(profile.get("supporting_pattern_ids", [])))
        self.assertTrue(isinstance(profile.get("explainability", {}), dict))

        profile_id = int(profile.get("profile_id", 0) or 0)
        self.assertGreater(profile_id, 0)
        status, by_id_payload = get_json(f"/collaboration/profiles/{profile_id}?context_scope={encoded_scope}")
        self.assertEqual(status, 200, by_id_payload)
        by_id = by_id_payload.get("profile", {}) if isinstance(by_id_payload, dict) else {}
        self.assertEqual(int(by_id.get("profile_id", 0) or 0), profile_id)
        self.assertTrue(bool(by_id.get("evidence_summary", "")))

        influenced = self._build_orchestration(run_id=run_id, source=source, environment_profile="lab_a")
        self.assertEqual(str(influenced.get("collaboration_mode", "")), str(profile.get("dominant_collaboration_mode", "")))
        self.assertEqual(
            str(influenced.get("human_context_modifiers", {}).get("mode_reason", "")),
            "objective70_profile_influence",
        )

        influenced_negotiation_id = self._negotiation_id(influenced)
        self.assertGreater(influenced_negotiation_id, 0)
        status, influenced_payload = get_json(f"/collaboration/negotiations/{influenced_negotiation_id}")
        self.assertEqual(status, 200, influenced_payload)
        influenced_negotiation = influenced_payload.get("negotiation", {}) if isinstance(influenced_payload, dict) else {}
        explainability = influenced_negotiation.get("explainability", {}) if isinstance(influenced_negotiation.get("explainability", {}), dict) else {}
        obj70 = explainability.get("objective70_profile_influence", {}) if isinstance(explainability.get("objective70_profile_influence", {}), dict) else {}
        self.assertTrue(bool(obj70.get("influence_applied", False)))

        different_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="lab_b")
        self.assertNotEqual(
            str(different_context.get("human_context_modifiers", {}).get("mode_reason", "")),
            "objective70_profile_influence",
        )


if __name__ == "__main__":
    unittest.main()
