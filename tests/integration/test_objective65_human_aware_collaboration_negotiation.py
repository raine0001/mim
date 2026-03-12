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


class Objective65HumanAwareCollaborationNegotiationTest(unittest.TestCase):
    def _seed_cross_domain_inputs(self, run_id: str, zone: str, urgent: bool) -> None:
        text = f"Objective65 routine update {run_id}"
        intent = "operator_update"
        metadata = {"run_id": run_id}
        if urgent:
            text = f"URGENT: collaboration conflict update {run_id}"
            intent = "operator_urgent_request"
            metadata["urgency"] = "high"

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": intent,
                "confidence": 0.96 if urgent else 0.9,
                "metadata_json": metadata,
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
                        "object_label": "objective65-target",
                        "confidence": 0.87,
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
                "content": f"Objective65 external context {run_id}",
                "summary": "External context for collaboration negotiation",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, *, run_id: str, operator_present: bool, human_in_workspace: bool, shared_workspace_active: bool) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective65-test",
                "reason": "objective65 focused setup",
                "operator_present": operator_present,
                "human_in_workspace": human_in_workspace,
                "shared_workspace_active": shared_workspace_active,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, *, run_id: str, source: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective65-test",
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

    def test_objective65_human_aware_collaboration_negotiation(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective65-focused-{run_id}"
        zone = f"obj65-zone-{run_id}"

        self._seed_cross_domain_inputs(run_id=run_id, zone=zone, urgent=True)
        self._set_human_signals(
            run_id=run_id,
            operator_present=True,
            human_in_workspace=True,
            shared_workspace_active=True,
        )

        first_orchestration = self._build_orchestration(run_id=run_id, source=source)
        self.assertIn(str(first_orchestration.get("status", "")), {"blocked_needs_input", "deferred"})

        negotiation_id = self._negotiation_id_from_orchestration(first_orchestration)
        self.assertGreater(negotiation_id, 0)

        status, negotiation_payload = get_json(f"/collaboration/negotiations/{negotiation_id}")
        self.assertEqual(status, 200, negotiation_payload)
        negotiation = negotiation_payload.get("negotiation", {}) if isinstance(negotiation_payload, dict) else {}

        options = negotiation.get("options_presented", []) if isinstance(negotiation.get("options_presented", []), list) else []
        option_ids = {str(item.get("option_id", "")) for item in options if isinstance(item, dict)}
        self.assertGreaterEqual(len(option_ids), 2)
        self.assertIn("defer_action", option_ids)
        self.assertIn("rescan_first", option_ids)

        explainability = negotiation.get("explainability", {}) if isinstance(negotiation.get("explainability", {}), dict) else {}
        self.assertTrue(bool(explainability.get("trigger_summary", [])))
        self.assertTrue(bool(str(explainability.get("why_human_input_needed", "")).strip()))
        self.assertTrue(bool(str(explainability.get("safe_fallback_if_unanswered", "")).strip()))

        human_context_state = negotiation.get("human_context_state", {}) if isinstance(negotiation.get("human_context_state", {}), dict) else {}
        signals = human_context_state.get("signals", {}) if isinstance(human_context_state.get("signals", {}), dict) else {}
        self.assertTrue(bool(signals.get("shared_workspace_active", False)))

        status, response_payload = post_json(
            f"/collaboration/negotiations/{negotiation_id}/respond",
            {
                "actor": "objective65-test",
                "option_id": "rescan_first",
                "reason": "prefer verification-first path",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, response_payload)
        response_orchestration = response_payload.get("orchestration", {}) if isinstance(response_payload, dict) else {}
        self.assertEqual(str(response_orchestration.get("status", "")), "replan_required")

        fallback_orchestration = self._build_orchestration(run_id=run_id, source=source)
        fallback_negotiation_id = self._negotiation_id_from_orchestration(fallback_orchestration)
        self.assertGreater(fallback_negotiation_id, 0)

        status, fallback_payload = get_json(
            f"/collaboration/negotiations/{fallback_negotiation_id}?apply_fallback=true&fallback_after_seconds=0"
        )
        self.assertEqual(status, 200, fallback_payload)
        fallback_negotiation = fallback_payload.get("negotiation", {}) if isinstance(fallback_payload, dict) else {}
        self.assertEqual(str(fallback_negotiation.get("status", "")), "fallback_applied")
        self.assertEqual(
            str(fallback_negotiation.get("selected_option_id", "")),
            str(fallback_negotiation.get("default_safe_path", "")),
        )


if __name__ == "__main__":
    unittest.main()
