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


class Objective28AutonomousTaskProposalsTest(unittest.TestCase):
    def test_workspace_state_generates_and_actions_proposals(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center-obj28-{run_id}"
        label = f"target_obj28_{run_id}"

        status, _ = post_json(
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
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
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

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {"label": label, "zone": zone, "confidence": 0.92},
                    ],
                },
            },
        )
        self.assertEqual(status, 200, succeeded)
        proposal_ids = succeeded.get("feedback_json", {}).get("workspace_proposal_ids", [])
        self.assertGreaterEqual(len(proposal_ids), 1)

        status, proposals = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, proposals)
        pending = proposals.get("proposals", [])
        self.assertGreaterEqual(len(pending), 1)

        proposal = pending[0]
        proposal_id = int(proposal["proposal_id"])
        status, proposal_detail = get_json(f"/workspace/proposals/{proposal_id}")
        self.assertEqual(status, 200, proposal_detail)

        status, accepted = post_json(
            f"/workspace/proposals/{proposal_id}/accept",
            {"actor": "operator", "reason": "accept proposal", "metadata_json": {"ticket": f"OBJ28-{run_id}"}},
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("status"), "accepted")
        self.assertIsNotNone(accepted.get("linked_task_id"))

        reject_target = None
        for item in pending[1:]:
            if int(item["proposal_id"]) != proposal_id:
                reject_target = int(item["proposal_id"])
                break

        if reject_target is None:
            status, event2 = post_json(
                "/gateway/intake/text",
                {
                    "text": "scan workspace moved object",
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.93,
                    "metadata_json": {
                        "scan_mode": "full",
                        "scan_area": zone,
                        "confidence_threshold": 0.6,
                    },
                },
            )
            self.assertEqual(status, 200, event2)
            execution2 = event2["execution"]["execution_id"]
            for state in ["accepted", "running"]:
                post_json(
                    f"/gateway/capabilities/executions/{execution2}/feedback",
                    {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
                )
            post_json(
                f"/gateway/capabilities/executions/{execution2}/feedback",
                {
                    "status": "succeeded",
                    "reason": "scan complete",
                    "actor": "tod",
                    "feedback_json": {
                        "observations": [
                            {"label": f"{label}_moved", "zone": f"rear-center-obj28-{run_id}", "confidence": 0.89},
                        ],
                    },
                },
            )
            status, proposals2 = get_json("/workspace/proposals?status=pending")
            self.assertEqual(status, 200, proposals2)
            for item in proposals2.get("proposals", []):
                candidate = int(item["proposal_id"])
                if candidate != proposal_id:
                    reject_target = candidate
                    break

        self.assertIsNotNone(reject_target)
        status, rejected = post_json(
            f"/workspace/proposals/{reject_target}/reject",
            {"actor": "operator", "reason": "reject proposal", "metadata_json": {"ticket": f"OBJ28-REJ-{run_id}"}},
        )
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected.get("status"), "rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
