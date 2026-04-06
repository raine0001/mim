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


class Objective63CrossDomainTaskOrchestrationTest(unittest.TestCase):
    def _seed_stewardship_cycle(self, run_id: str, zone: str) -> int:
        status, result = post_json(
            "/stewardship/cycle",
            {
                "actor": "objective63-test",
                "source": "objective63-focused",
                "managed_scope": zone,
                "stale_after_seconds": 600,
                "lookback_hours": 24,
                "max_strategies": 3,
                "max_actions": 2,
                "auto_execute": False,
                "force_degraded": True,
                "target_environment_state": {
                    "zone_freshness_seconds": 900,
                    "max_system_drift_rate": 0.35,
                    "max_missing_key_objects": 0,
                    "proactive_drift_monitoring": True,
                    "key_objects": ["objective63-marker", "stability-anchor"],
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, result)
        stewardship = result.get("stewardship", {}) if isinstance(result, dict) else {}
        stewardship_id = int(stewardship.get("stewardship_id", 0))
        self.assertGreater(stewardship_id, 0)
        return stewardship_id

    def _seed_cross_domain_inputs(self, run_id: str, zone: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"Objective63 operator intent {run_id}",
                "parsed_intent": "operator_request",
                "confidence": 0.92,
                "metadata_json": {"run_id": run_id, "channel": "operator"},
            },
        )
        self.assertEqual(status, 200, event)

        status, memory = post_json(
            "/memory",
            {
                "memory_class": "external_signal",
                "content": f"Objective63 external context {run_id}",
                "summary": "External requirement changed",
                "metadata_json": {"run_id": run_id, "domain": "external_information"},
            },
        )
        self.assertEqual(status, 200, memory)

        status, perception = post_json(
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
                        "object_label": "objective63-marker",
                        "confidence": 0.86,
                        "zone": zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, perception)

    def test_objective63_cross_domain_task_orchestration(self) -> None:
        run_id = uuid4().hex[:8]
        source = f"objective63-focused-{run_id}"
        zone = f"obj63-zone-{run_id}"

        self._seed_cross_domain_inputs(run_id=run_id, zone=zone)
        stewardship_id = self._seed_stewardship_cycle(run_id=run_id, zone=zone)

        status, blocked_result = post_json(
            "/orchestration/build",
            {
                "actor": "objective63-test",
                "source": source,
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.95,
                "min_domains_required": 10,
                "dependency_resolution_policy": "ask",
                "generate_goal": True,
                "generate_horizon_plan": True,
                "generate_improvement_proposals": False,
                "metadata_json": {"run_id": run_id, "phase": "blocked"},
            },
        )
        self.assertEqual(status, 200, blocked_result)
        blocked = (
            blocked_result.get("orchestration", {})
            if isinstance(blocked_result, dict)
            else {}
        )
        self.assertEqual(str(blocked.get("status", "")), "blocked_needs_input")

        blocked_resolution = (
            blocked.get("dependency_resolution", {})
            if isinstance(blocked.get("dependency_resolution", {}), dict)
            else {}
        )
        self.assertEqual(str(blocked_resolution.get("path", "")), "ask")
        self.assertTrue(bool(blocked_resolution.get("unmet_dependencies", [])))
        self.assertGreaterEqual(len(blocked.get("linked_inquiry_question_ids", [])), 1)

        blocked_artifacts = (
            blocked.get("downstream_artifacts", [])
            if isinstance(blocked.get("downstream_artifacts", []), list)
            else []
        )
        self.assertTrue(
            any(
                str(item.get("artifact_type", "")) == "inquiry_question"
                for item in blocked_artifacts
                if isinstance(item, dict)
            )
        )

        status, ready_result = post_json(
            "/orchestration/build",
            {
                "actor": "objective63-test",
                "source": source,
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.4,
                "min_domains_required": 2,
                "dependency_resolution_policy": "replan",
                "generate_goal": True,
                "generate_horizon_plan": True,
                "generate_improvement_proposals": False,
                "metadata_json": {"run_id": run_id, "phase": "ready"},
            },
        )
        self.assertEqual(status, 200, ready_result)
        ready = (
            ready_result.get("orchestration", {})
            if isinstance(ready_result, dict)
            else {}
        )
        self.assertEqual(str(ready.get("status", "")), "active")

        contributing_domains = (
            ready.get("contributing_domains", [])
            if isinstance(ready.get("contributing_domains", []), list)
            else []
        )
        self.assertGreaterEqual(len(contributing_domains), 2)
        self.assertIn("stewardship_state", contributing_domains)
        self.assertTrue(bool(ready.get("orchestration_reason", "")))

        reasoning = (
            ready.get("reasoning", {})
            if isinstance(ready.get("reasoning", {}), dict)
            else {}
        )
        self.assertTrue(bool(reasoning.get("priority_reason", "")))
        self.assertGreaterEqual(len(reasoning.get("contributing_domains", [])), 2)
        stewardship_summary = (
            reasoning.get("stewardship_summary", {})
            if isinstance(reasoning.get("stewardship_summary", {}), dict)
            else {}
        )
        self.assertGreaterEqual(
            int(stewardship_summary.get("active_stewardship_count", 0)), 1
        )
        self.assertGreaterEqual(
            int(stewardship_summary.get("unstable_scope_count", 0)), 1
        )

        origin_context = (
            ready_result.get("origin_context", {})
            if isinstance(ready_result, dict)
            else {}
        )
        workspace_state = (
            origin_context.get("workspace_state", {})
            if isinstance(origin_context.get("workspace_state", {}), dict)
            else {}
        )
        origin_stewardship = (
            workspace_state.get("stewardship_summary", {})
            if isinstance(workspace_state.get("stewardship_summary", {}), dict)
            else {}
        )
        self.assertGreaterEqual(
            int(origin_stewardship.get("active_stewardship_count", 0)), 1
        )
        scope_samples = (
            origin_stewardship.get("recent_scope_samples", [])
            if isinstance(origin_stewardship.get("recent_scope_samples", []), list)
            else []
        )
        self.assertTrue(
            any(
                int(item.get("stewardship_id", 0)) == stewardship_id
                for item in scope_samples
                if isinstance(item, dict)
            )
        )

        linked_goal_ids = (
            ready.get("linked_goal_ids", [])
            if isinstance(ready.get("linked_goal_ids", []), list)
            else []
        )
        linked_plan_ids = (
            ready.get("linked_horizon_plan_ids", [])
            if isinstance(ready.get("linked_horizon_plan_ids", []), list)
            else []
        )
        self.assertTrue(bool(linked_goal_ids) or bool(linked_plan_ids))

        ready_artifacts = (
            ready.get("downstream_artifacts", [])
            if isinstance(ready.get("downstream_artifacts", []), list)
            else []
        )
        self.assertTrue(
            any(
                str(item.get("artifact_type", "")) in {"goal", "horizon_plan"}
                for item in ready_artifacts
                if isinstance(item, dict)
            )
        )

        status, listed = get_json(f"/orchestration?source={source}&limit=20")
        self.assertEqual(status, 200, listed)
        rows = listed.get("orchestrations", []) if isinstance(listed, dict) else []
        self.assertGreaterEqual(len(rows), 2)

        row_ids = {
            int(item.get("orchestration_id", 0))
            for item in rows
            if isinstance(item, dict)
        }
        blocked_id = int(blocked.get("orchestration_id", 0))
        ready_id = int(ready.get("orchestration_id", 0))
        self.assertIn(blocked_id, row_ids)
        self.assertIn(ready_id, row_ids)

        status, fetched = get_json(f"/orchestration/{ready_id}")
        self.assertEqual(status, 200, fetched)
        fetched_row = (
            fetched.get("orchestration", {}) if isinstance(fetched, dict) else {}
        )
        self.assertEqual(int(fetched_row.get("orchestration_id", 0)), ready_id)
        self.assertGreaterEqual(len(fetched_row.get("contributing_domains", [])), 2)


if __name__ == "__main__":
    unittest.main()
