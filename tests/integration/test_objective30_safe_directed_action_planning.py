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
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective30SafeDirectedActionPlanningTest(unittest.TestCase):
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

    def _run_scan(self, *, run_id: str, observations: list[dict], scan_area: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace objective30 {run_id}",
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

    def test_safe_directed_action_planning_paths(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        exact_zone = f"front-center-obj30-exact-{run_id}"
        exact_label = f"obj30 exact target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=exact_zone,
            observations=[{"label": exact_label, "zone": exact_zone, "confidence": 0.97}],
        )

        status, exact_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": exact_label,
                "preferred_zone": exact_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, exact_resolve)
        self.assertEqual(exact_resolve.get("policy_outcome"), "target_confirmed")

        status, exact_plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": exact_resolve["target_resolution_id"],
                "action_type": "prepare_reach_plan",
                "source": "integration-test",
                "notes": "prepare safe plan",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, exact_plan)
        self.assertEqual(exact_plan.get("planning_outcome"), "plan_ready_for_approval")
        self.assertEqual(exact_plan.get("status"), "pending_approval")
        self.assertEqual(exact_plan.get("action_type"), "prepare_reach_plan")

        plan_id = exact_plan["plan_id"]
        status, fetched = get_json(f"/workspace/action-plans/{plan_id}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched.get("plan_id"), plan_id)

        status, approved = post_json(
            f"/workspace/action-plans/{plan_id}/approve",
            {"actor": "operator", "reason": "looks safe", "metadata_json": {"run_id": run_id}},
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved.get("status"), "approved")
        self.assertEqual(approved.get("planning_outcome"), "plan_approved")

        status, queued = post_json(
            f"/workspace/action-plans/{plan_id}/queue",
            {
                "actor": "operator",
                "reason": "queue handoff",
                "requested_executor": "tod",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued.get("status"), "queued")
        self.assertEqual(queued.get("planning_outcome"), "plan_queued")
        self.assertIsNotNone(queued.get("queued_task_id"))
        self.assertEqual(queued.get("handoff", {}).get("dispatch_decision"), "queued_for_executor")

        amb_zone = f"rear-center-obj30-amb-{run_id}"
        amb_a = f"obj30 amber cube alpha {run_id}"
        amb_b = f"obj30 amber cube beta {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=amb_zone,
            observations=[
                {"label": amb_a, "zone": amb_zone, "confidence": 0.91},
                {"label": amb_b, "zone": amb_zone, "confidence": 0.92},
            ],
        )

        status, ambiguous_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": "obj30 amber cube",
                "preferred_zone": "",
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, ambiguous_resolve)
        self.assertEqual(ambiguous_resolve.get("policy_outcome"), "target_requires_confirmation")

        status, review_plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": ambiguous_resolve["target_resolution_id"],
                "action_type": "observe",
                "source": "integration-test",
                "notes": "needs review",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, review_plan)
        self.assertEqual(review_plan.get("planning_outcome"), "plan_requires_review")
        self.assertEqual(review_plan.get("status"), "pending_review")

        unsafe_zone = f"rear-right-obj30-unsafe-{run_id}"
        unsafe_label = f"obj30 unsafe target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=unsafe_zone,
            observations=[{"label": unsafe_label, "zone": unsafe_zone, "confidence": 0.93}],
        )

        status, unsafe_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": unsafe_label,
                "preferred_zone": unsafe_zone,
                "source": "integration-test",
                "unsafe_zones": [unsafe_zone],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, unsafe_resolve)
        self.assertEqual(unsafe_resolve.get("policy_outcome"), "target_blocked_unsafe_zone")

        status, blocked_plan = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": unsafe_resolve["target_resolution_id"],
                "action_type": "request_confirmation",
                "source": "integration-test",
                "notes": "unsafe zone",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, blocked_plan)
        self.assertEqual(blocked_plan.get("planning_outcome"), "plan_blocked")
        self.assertEqual(blocked_plan.get("status"), "blocked")

        status, rejected = post_json(
            f"/workspace/action-plans/{review_plan['plan_id']}/reject",
            {"actor": "operator", "reason": "insufficient confidence", "metadata_json": {"run_id": run_id}},
        )
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected.get("status"), "rejected")
        self.assertEqual(rejected.get("planning_outcome"), "plan_rejected")

        status, bad_action = post_json(
            "/workspace/action-plans",
            {
                "target_resolution_id": exact_resolve["target_resolution_id"],
                "action_type": "actuate_gripper",
                "source": "integration-test",
                "notes": "unsupported action type",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 422, bad_action)


if __name__ == "__main__":
    unittest.main(verbosity=2)
