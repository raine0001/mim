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


class Objective69NegotiationPatternAbstractionTest(unittest.TestCase):
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
                "text": f"URGENT: objective69 negotiation abstraction flow {run_id}",
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
                        "object_label": "objective69-target",
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
                "content": f"Objective69 external context {run_id}",
                "summary": "External context for objective69 pattern abstraction",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, run_id: str) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective69-test",
                "reason": "objective69 focused setup",
                "operator_present": True,
                "human_in_workspace": True,
                "shared_workspace_active": True,
                "occupied_zones": ["front-center"],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, run_id: str, source: str, environment_profile: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective69-test",
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
                "actor": "objective69-test",
                "option_id": option_id,
                "reason": f"objective69 choose {option_id}",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _pattern_signature(self, env: str) -> str:
        return (
            "collaboration_negotiation|physical|high|shared:True|operator:True|"
            f"urgency:high|env:{env}"
        )

    def test_objective69_negotiation_pattern_abstraction(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective69-focused-{run_id}"
        zone = f"obj69-zone-{run_id}"

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

        signature = self._pattern_signature("warehouse_a")
        encoded_signature = urllib.parse.quote(signature, safe="")
        status, patterns_payload = get_json(f"/collaboration/patterns?context_signature={encoded_signature}&limit=20")
        self.assertEqual(status, 200, patterns_payload)
        patterns = patterns_payload.get("patterns", []) if isinstance(patterns_payload, dict) else []
        self.assertTrue(bool(patterns))

        pattern = patterns[0]
        self.assertEqual(str(pattern.get("context_signature", "")), signature)
        self.assertGreaterEqual(int(pattern.get("evidence_count", 0) or 0), 4)
        self.assertGreaterEqual(float(pattern.get("confidence", 0.0) or 0.0), 0.74)
        self.assertEqual(str(pattern.get("dominant_outcome", "")), "rescan_first")
        self.assertTrue(str(pattern.get("status", "")) in {"consolidated", "acknowledged"})
        self.assertTrue(bool(pattern.get("evidence_summary", "")))
        self.assertTrue(isinstance(pattern.get("explainability", {}), dict))

        pattern_id = int(pattern.get("pattern_id", 0) or 0)
        self.assertGreater(pattern_id, 0)
        status, acknowledged = post_json(
            f"/collaboration/patterns/{pattern_id}/acknowledge",
            {
                "actor": "objective69-test",
                "reason": "reviewed objective69 pattern",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, acknowledged)
        acknowledged_pattern = acknowledged.get("pattern", {}) if isinstance(acknowledged, dict) else {}
        self.assertEqual(str(acknowledged_pattern.get("status", "")), "acknowledged")

        same_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_a")
        same_negotiation_id = self._negotiation_id(same_context)
        self.assertGreater(same_negotiation_id, 0)
        status, same_payload = get_json(f"/collaboration/negotiations/{same_negotiation_id}")
        self.assertEqual(status, 200, same_payload)
        same_negotiation = same_payload.get("negotiation", {}) if isinstance(same_payload, dict) else {}
        self.assertEqual(str(same_negotiation.get("default_safe_path", "")), "rescan_first")
        explainability = same_negotiation.get("explainability", {}) if isinstance(same_negotiation.get("explainability", {}), dict) else {}
        obj69 = explainability.get("objective69_pattern_influence", {}) if isinstance(explainability.get("objective69_pattern_influence", {}), dict) else {}
        self.assertTrue(bool(obj69.get("influence_applied", False)))

        different_context = self._build_orchestration(run_id=run_id, source=source, environment_profile="warehouse_b")
        different_negotiation_id = self._negotiation_id(different_context)
        self.assertGreater(different_negotiation_id, 0)
        status, different_payload = get_json(f"/collaboration/negotiations/{different_negotiation_id}")
        self.assertEqual(status, 200, different_payload)
        different_negotiation = different_payload.get("negotiation", {}) if isinstance(different_payload, dict) else {}
        self.assertNotEqual(str(different_negotiation.get("default_safe_path", "")), "rescan_first")


if __name__ == "__main__":
    unittest.main()
