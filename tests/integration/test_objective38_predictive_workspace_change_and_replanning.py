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


class Objective38PredictiveWorkspaceReplanTest(unittest.TestCase):
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
                "confidence": 0.96,
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

    def _prepare_executable_plan(self, *, run_id: str, suffix: str) -> tuple[int, int]:
        zone = f"front-center-obj38-{suffix}-{run_id}"
        label = f"obj38-target-{suffix}-{run_id}"
        self._run_scan(
            text=f"objective38 setup {suffix} {run_id}",
            scan_area=zone,
            observations=[{"label": label, "zone": zone, "confidence": 0.97}],
        )

        status, resolved = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": label,
                "preferred_zone": zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolved)

        status, plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": resolved["target_resolution_id"],
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": f"objective38 {suffix}",
                "motion_plan_overrides": {},
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, plan)
        plan_id = int(plan["plan_id"])

        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {"actor": "operator", "reason": f"approve {suffix}", "metadata_json": {}},
        )
        self.assertEqual(status, 200, approved)

        status, simulated = post_json(
            f"/workspace/action-plans/{plan_id}/simulate",
            {
                "actor": "operator",
                "reason": f"simulate {suffix}",
                "collision_risk_threshold": 0.45,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, simulated)
        self.assertEqual(simulated.get("simulation_outcome"), "plan_safe")

        status, executed = post_json(
            f"/workspace/action-plans/{plan_id}/execute",
            {
                "actor": "operator",
                "reason": f"execute {suffix}",
                "requested_executor": "tod",
                "capability_name": "reach_target",
                "collision_risk_threshold": 0.45,
                "target_confidence_minimum": 0.7,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 200, executed)
        execution_id = int(executed["execution_id"])
        return plan_id, execution_id

    def test_predictive_replan_flow(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        slight_plan_id, slight_execution_id = self._prepare_executable_plan(run_id=run_id, suffix="slight")
        status, slight_signal_resp = post_json(
            f"/workspace/executions/{slight_execution_id}/predict-change",
            {
                "actor": "workspace-monitor",
                "source": "objective38-test",
                "signal_type": "object_moved",
                "predicted_outcome": "pause_and_resimulate",
                "confidence": 0.78,
                "reason": "target drifted within safe adjacent zone",
                "metadata_json": {"case": "slight"},
            },
        )
        self.assertEqual(status, 200, slight_signal_resp)
        self.assertTrue(slight_signal_resp.get("applied_hold"))
        slight_signal_id = int(slight_signal_resp["signal"]["signal_id"])

        status, inbox = get_json("/operator/inbox")
        self.assertEqual(status, 200, inbox)
        self.assertGreaterEqual(inbox["counts"].get("paused", 0), 1)

        status, blocked_resume = post_json(
            f"/workspace/executions/{slight_execution_id}/resume",
            {
                "actor": "operator",
                "source": "operator",
                "reason": "resume without restored conditions",
                "safety_ack": True,
                "conditions_restored": False,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, blocked_resume)

        status, slight_replan = post_json(
            f"/workspace/action-plans/{slight_plan_id}/replan",
            {
                "actor": "operator",
                "reason": "apply slight-drift replan",
                "signal_id": slight_signal_id,
                "force": False,
                "motion_plan_overrides": {},
                "metadata_json": {"case": "slight"},
            },
        )
        self.assertEqual(status, 200, slight_replan)
        self.assertIn(slight_replan.get("planning_outcome"), {"plan_requires_resimulation", "plan_requires_review", "plan_replanned"})

        status, slight_resim = post_json(
            f"/workspace/action-plans/{slight_plan_id}/simulate",
            {
                "actor": "operator",
                "reason": "resimulate after slight drift",
                "collision_risk_threshold": 0.45,
                "metadata_json": {"case": "slight"},
            },
        )
        self.assertEqual(status, 200, slight_resim)
        self.assertEqual(slight_resim.get("simulation_outcome"), "plan_safe")

        obstacle_plan_id, obstacle_execution_id = self._prepare_executable_plan(run_id=run_id, suffix="obstacle")
        status, obstacle_signal_resp = post_json(
            f"/workspace/executions/{obstacle_execution_id}/predict-change",
            {
                "actor": "workspace-monitor",
                "source": "objective38-test",
                "signal_type": "new_obstacle_detected",
                "predicted_outcome": "require_replan",
                "confidence": 0.86,
                "reason": "new obstacle appeared in planned path",
                "metadata_json": {"case": "obstacle"},
            },
        )
        self.assertEqual(status, 200, obstacle_signal_resp)
        obstacle_signal_id = int(obstacle_signal_resp["signal"]["signal_id"])

        status, obstacle_replan = post_json(
            f"/workspace/action-plans/{obstacle_plan_id}/replan",
            {
                "actor": "operator",
                "reason": "obstacle requires replan",
                "signal_id": obstacle_signal_id,
                "force": False,
                "motion_plan_overrides": {},
                "metadata_json": {"case": "obstacle"},
            },
        )
        self.assertEqual(status, 200, obstacle_replan)
        self.assertIn(obstacle_replan.get("planning_outcome"), {"plan_requires_review", "plan_requires_resimulation"})
        self.assertTrue(bool(obstacle_replan.get("replan", {}).get("operator_confirmation_required", False)))

        severe_plan_id, severe_execution_id = self._prepare_executable_plan(run_id=run_id, suffix="severe")
        status, severe_signal_resp = post_json(
            f"/workspace/executions/{severe_execution_id}/predict-change",
            {
                "actor": "workspace-monitor",
                "source": "objective38-test",
                "signal_type": "target_no_longer_valid",
                "predicted_outcome": "abort_chain",
                "confidence": 0.93,
                "reason": "target no longer valid in workspace",
                "metadata_json": {"case": "severe"},
            },
        )
        self.assertEqual(status, 200, severe_signal_resp)
        severe_signal_id = int(severe_signal_resp["signal"]["signal_id"])

        status, severe_replan = post_json(
            f"/workspace/action-plans/{severe_plan_id}/replan",
            {
                "actor": "operator",
                "reason": "severe drift replan attempt",
                "signal_id": severe_signal_id,
                "force": False,
                "motion_plan_overrides": {},
                "metadata_json": {"case": "severe"},
            },
        )
        self.assertEqual(status, 200, severe_replan)
        self.assertEqual(severe_replan.get("status"), "blocked")
        self.assertEqual(severe_replan.get("planning_outcome"), "plan_blocked")
        self.assertTrue(bool(severe_replan.get("replan", {}).get("operator_confirmation_required", False)))

        status, severe_resume_blocked = post_json(
            f"/workspace/executions/{severe_execution_id}/resume",
            {
                "actor": "operator",
                "source": "operator",
                "reason": "unsafe to resume",
                "safety_ack": True,
                "conditions_restored": False,
                "metadata_json": {},
            },
        )
        self.assertEqual(status, 422, severe_resume_blocked)

        status, replan_history = get_json(f"/workspace/action-plans/{severe_plan_id}/replan-history")
        self.assertEqual(status, 200, replan_history)
        self.assertGreaterEqual(len(replan_history.get("replan_history", [])), 1)

        status, signal_list = get_json(f"/workspace/replan-signals?execution_id={severe_execution_id}")
        self.assertEqual(status, 200, signal_list)
        self.assertGreaterEqual(len(signal_list.get("replan_signals", [])), 1)

        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        actions = {entry.get("action") for entry in journal}
        self.assertIn("workspace_execution_predict_change", actions)
        self.assertIn("workspace_action_plan_replan", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
