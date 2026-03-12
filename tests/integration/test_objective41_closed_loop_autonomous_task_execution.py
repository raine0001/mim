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


class Objective41ClosedLoopAutonomousTaskExecutionTest(unittest.TestCase):
    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_scan",
                "category": "diagnostic",
                "description": "Scan workspace and return observation set",
                "requires_confirmation": False,
                "enabled": True,
                "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
            },
        )
        self.assertEqual(status, 200, payload)

    def _run_scan(self, *, text: str, scan_area: str, observations: list[dict]) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scan_area,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event["execution"]["execution_id"]

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {"observations": observations},
            },
        )
        self.assertEqual(status, 200, done)

    def _clear_active_interruptions(self) -> None:
        status, body = get_json("/workspace/interruptions?status=active&limit=200")
        if status != 200 or not isinstance(body, dict):
            return
        for item in body.get("interruptions", []):
            if not isinstance(item, dict):
                continue
            execution_id = int(item.get("execution_id", 0)) if str(item.get("execution_id", "")).isdigit() else 0
            if execution_id <= 0:
                continue
            post_json(
                f"/workspace/executions/{execution_id}/resume",
                {
                    "actor": "operator",
                    "source": "operator",
                    "reason": "objective41 pre-test cleanup",
                    "safety_ack": True,
                    "conditions_restored": True,
                    "metadata_json": {},
                },
            )

    def test_objective41_closed_loop_autonomy(self) -> None:
        run_id = uuid4().hex[:8]
        zone_a = f"front-left-obj41-{run_id}"
        zone_b = f"rear-left-obj41-{run_id}"
        zone_filter = f"obj41-{run_id}"
        moved_label = f"obj41-moved-{run_id}"

        self._register_workspace_scan()
        self._clear_active_interruptions()

        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "objective41 setup pending proposals",
                "force_manual_approval": True,
                "auto_execution_enabled": True,
                "reset_auto_history": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        self._run_scan(
            text=f"objective41 baseline {run_id}",
            scan_area=zone_a,
            observations=[{"label": moved_label, "zone": zone_a, "confidence": 0.95}],
        )
        self._run_scan(
            text=f"objective41 moved {run_id}",
            scan_area=zone_b,
            observations=[{"label": moved_label, "zone": zone_b, "confidence": 0.94}],
        )

        status, pending_before = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending_before)
        self.assertTrue(any(item.get("proposal_type") == "verify_moved_object" for item in pending_before.get("proposals", [])))

        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "objective41 enable bounded auto loop",
                "force_manual_approval": False,
                "auto_execution_enabled": True,
                "max_auto_tasks_per_window": 1,
                "auto_window_seconds": 300,
                "cooldown_between_actions_seconds": 0,
                "capability_cooldown_seconds": {"workspace_scan": 0},
                "zone_action_limits": {},
                "auto_safe_confidence_threshold": 0.7,
                "auto_preferred_confidence_threshold": 0.7,
                "low_risk_score_max": 0.3,
                "max_autonomy_retries": 1,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        status, step_one = post_json(
            f"/workspace/autonomy/loop/step?actor=objective41-test&reason=closed-loop-check&zone_filter={zone_filter}",
            {},
        )
        self.assertEqual(status, 200, step_one)
        self.assertTrue(step_one.get("executed"), step_one)
        first_proposal = step_one.get("proposal") or {}
        first_proposal_id = int(first_proposal.get("proposal_id", 0))
        self.assertGreater(first_proposal_id, 0)

        status, first_proposal_body = get_json(f"/workspace/proposals/{first_proposal_id}")
        self.assertEqual(status, 200, first_proposal_body)
        first_meta = first_proposal_body.get("metadata_json", {})
        execution_id = int(first_meta.get("active_execution_id", 0))
        self.assertGreater(execution_id, 0)

        status, pending_after_first = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending_after_first)
        self.assertTrue(any(item.get("proposal_type") == "verify_moved_object" for item in pending_after_first.get("proposals", [])))

        self._run_scan(
            text=f"objective41 extra safe proposal {run_id}",
            scan_area=zone_a,
            observations=[{"label": f"obj41-extra-{run_id}", "zone": zone_a, "confidence": 0.96}],
        )

        status, step_two = post_json(
            f"/workspace/autonomy/loop/step?actor=objective41-test&reason=throttle-check&zone_filter={zone_filter}",
            {},
        )
        self.assertEqual(status, 200, step_two)
        self.assertFalse(step_two.get("executed"), step_two)
        self.assertIn(
            step_two.get("result"),
            {"max_auto_tasks_per_window", "cooldown_between_actions", "capability_cooldown", "policy_operator_required"},
        )

        status, paused = post_json(
            f"/workspace/executions/{execution_id}/pause",
            {
                "actor": "operator",
                "source": "operator",
                "interruption_type": "human_detected_in_workspace",
                "reason": "objective41 interruption pause",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, paused)

        status, paused_step = post_json(
            f"/workspace/autonomy/loop/step?actor=objective41-test&reason=interruption-check&zone_filter={zone_filter}",
            {},
        )
        self.assertEqual(status, 200, paused_step)
        self.assertEqual(paused_step.get("result"), "paused_by_interruption")

        status, resumed = post_json(
            f"/workspace/executions/{execution_id}/resume",
            {
                "actor": "operator",
                "source": "operator",
                "reason": "objective41 resume after interruption check",
                "safety_ack": True,
                "conditions_restored": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, resumed)

        status, feedback_state = get_json(f"/gateway/capabilities/executions/{execution_id}/feedback")
        self.assertEqual(status, 200, feedback_state)
        current_status = str(feedback_state.get("status", "")).strip()

        if current_status in {"dispatched", "accepted"}:
            status, feedback = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": "running",
                    "reason": "objective41 running",
                    "actor": "tod",
                    "feedback_json": {},
                },
            )
            self.assertEqual(status, 200, feedback)

        status, feedback = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "objective41 succeeded",
                "actor": "tod",
                "feedback_json": {
                    "observations": [{"label": f"obj41-resolved-{run_id}", "zone": zone_a, "confidence": 0.98}]
                },
            },
        )
        self.assertEqual(status, 200, feedback)

        status, verification_step = post_json(
            f"/workspace/autonomy/loop/step?actor=objective41-test&reason=verification-check&zone_filter={zone_filter}",
            {},
        )
        self.assertEqual(status, 200, verification_step)
        updates = verification_step.get("verification_updates", [])
        self.assertTrue(any(int(item.get("proposal_id", 0)) == first_proposal_id and item.get("result") == "success" for item in updates), verification_step)

        status, first_after_feedback = get_json(f"/workspace/proposals/{first_proposal_id}")
        self.assertEqual(status, 200, first_after_feedback)
        self.assertEqual(first_after_feedback.get("status"), "resolved")
        memory_delta = first_after_feedback.get("metadata_json", {}).get("memory_delta", {})
        self.assertTrue(
            bool(memory_delta.get("workspace_observation_ids"))
            or int(memory_delta.get("observation_count", 0)) > 0,
            first_after_feedback,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
