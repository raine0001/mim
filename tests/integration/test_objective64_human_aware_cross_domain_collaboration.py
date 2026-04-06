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


class Objective64HumanAwareCrossDomainCollaborationTest(unittest.TestCase):
    def _seed_cross_domain_inputs(self, run_id: str, zone: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"Objective64 routine update {run_id}",
                "parsed_intent": "operator_update",
                "confidence": 0.91,
                "metadata_json": {"run_id": run_id},
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
                        "object_label": "objective64-target",
                        "confidence": 0.84,
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
                "content": f"Objective64 external context {run_id}",
                "summary": "External context for human-aware collaboration",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, memory)

    def _set_human_signals(self, *, run_id: str, operator_present: bool, human_in_workspace: bool, shared_workspace_active: bool) -> None:
        status, payload = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": "objective64-test",
                "reason": "objective64 focused setup",
                "operator_present": operator_present,
                "human_in_workspace": human_in_workspace,
                "shared_workspace_active": shared_workspace_active,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)

    def _build_orchestration(self, *, run_id: str, source: str, task_kind: str, action_risk_level: str) -> dict:
        status, payload = post_json(
            "/orchestration/build",
            {
                "actor": "objective64-test",
                "source": source,
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.3,
                "min_domains_required": 2,
                "dependency_resolution_policy": "ask",
                "collaboration_mode_preference": "auto",
                "task_kind": task_kind,
                "action_risk_level": action_risk_level,
                "use_human_aware_signals": True,
                "generate_goal": True,
                "generate_horizon_plan": False,
                "generate_improvement_proposals": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, payload)
        orchestration = payload.get("orchestration", {}) if isinstance(payload, dict) else {}
        self.assertTrue(bool(orchestration))
        return orchestration

    def test_objective64_human_aware_cross_domain_collaboration(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective64-focused-{run_id}"
        zone = f"obj64-zone-{run_id}"

        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)

        self._set_human_signals(
            run_id=run_id,
            operator_present=False,
            human_in_workspace=False,
            shared_workspace_active=False,
        )
        baseline = self._build_orchestration(
            run_id=run_id,
            source=source,
            task_kind="physical",
            action_risk_level="medium",
        )
        baseline_priority = float(baseline.get("priority_score", 0.0))

        self._set_human_signals(
            run_id=run_id,
            operator_present=True,
            human_in_workspace=True,
            shared_workspace_active=False,
        )
        operator_present_row = self._build_orchestration(
            run_id=run_id,
            source=source,
            task_kind="physical",
            action_risk_level="medium",
        )
        self.assertEqual(str(operator_present_row.get("collaboration_mode", "")), "confirmation-first")

        status, urgent_event = post_json(
            "/gateway/intake/text",
            {
                "text": f"URGENT: please prioritize collaboration update {run_id}",
                "parsed_intent": "operator_urgent_request",
                "confidence": 0.96,
                "metadata_json": {"run_id": run_id, "urgency": "high"},
            },
        )
        self.assertEqual(status, 200, urgent_event)

        self._set_human_signals(
            run_id=run_id,
            operator_present=False,
            human_in_workspace=False,
            shared_workspace_active=False,
        )
        urgent_row = self._build_orchestration(
            run_id=run_id,
            source=source,
            task_kind="physical",
            action_risk_level="medium",
        )
        urgent_priority = float(urgent_row.get("priority_score", 0.0))
        urgent_modifiers = (urgent_row.get("human_context_modifiers", {}) if isinstance(urgent_row.get("human_context_modifiers", {}), dict) else {}).get("active_modifiers", [])
        self.assertGreaterEqual(urgent_priority, baseline_priority)
        self.assertIn("urgent_communication_reprioritize", urgent_modifiers)

        self._set_human_signals(
            run_id=run_id,
            operator_present=True,
            human_in_workspace=True,
            shared_workspace_active=True,
        )
        shared_physical_row = self._build_orchestration(
            run_id=run_id,
            source=source,
            task_kind="physical",
            action_risk_level="high",
        )
        self.assertEqual(str(shared_physical_row.get("collaboration_mode", "")), "deferential")
        self.assertEqual(str(shared_physical_row.get("status", "")), "deferred")

        shared_info_row = self._build_orchestration(
            run_id=run_id,
            source=source,
            task_kind="informational",
            action_risk_level="low",
        )
        self.assertEqual(str(shared_info_row.get("status", "")), "active")

        explainability_modifiers = shared_physical_row.get("human_context_modifiers", {}) if isinstance(shared_physical_row.get("human_context_modifiers", {}), dict) else {}
        explainability_reasoning = shared_physical_row.get("collaboration_reasoning", {}) if isinstance(shared_physical_row.get("collaboration_reasoning", {}), dict) else {}
        self.assertTrue(bool(explainability_modifiers.get("active_modifiers", [])))
        self.assertTrue(bool((explainability_reasoning.get("human_aware_signals", {}) if isinstance(explainability_reasoning.get("human_aware_signals", {}), dict) else {}).get("shared_workspace_active", False)))

        status, state = get_json("/orchestration/collaboration/state")
        self.assertEqual(status, 200, state)
        collaboration = state.get("collaboration", {}) if isinstance(state, dict) else {}
        self.assertTrue(bool(collaboration.get("active_modifiers", [])))


if __name__ == "__main__":
    unittest.main()
