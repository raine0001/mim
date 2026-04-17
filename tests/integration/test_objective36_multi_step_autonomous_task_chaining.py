import json
import os
import time
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


class Objective36MultiStepAutonomousTaskChainingTest(unittest.TestCase):
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

    def test_objective36_chain_policies_cooldown_approval_and_audit(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center-obj36-{run_id}"

        self._register_workspace_scan()
        self._run_scan(
            text=f"objective36 chain setup {run_id}",
            scan_area=zone,
            observations=[{"label": f"obj36-{run_id}", "zone": zone, "confidence": 0.95}],
        )

        status, proposals = get_json("/workspace/proposals")
        self.assertEqual(status, 200, proposals)
        all_proposals = [item for item in proposals.get("proposals", []) if int(item.get("proposal_id", 0)) > 0]
        proposal_ids = [int(item["proposal_id"]) for item in all_proposals]
        self.assertGreaterEqual(len(proposal_ids), 2, proposals)

        status, pending = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending)
        pending_ids = [int(item["proposal_id"]) for item in pending.get("proposals", []) if int(item.get("proposal_id", 0)) > 0]
        self.assertGreaterEqual(len(pending_ids), 1)

        # Approval-gated chain: cannot advance before explicit approval.
        status, created = post_json(
            "/workspace/chains",
            {
                "actor": "workspace",
                "reason": "objective36 approval chain",
                "chain_type": "proposal_sequence",
                "proposal_ids": [proposal_ids[0]],
                "source": "objective36-test",
                "requires_approval": True,
                "cooldown_seconds": 0,
                "stop_on_failure": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        chain_id = int(created["chain_id"])
        self.assertEqual(created["status"], "pending_approval")
        self.assertEqual(created["current_step_index"], 0)

        status, blocked_pre_approval = post_json(
            f"/workspace/chains/{chain_id}/advance",
            {
                "actor": "workspace",
                "reason": "advance before approval",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 422, blocked_pre_approval)

        status, approved = post_json(
            f"/workspace/chains/{chain_id}/approve",
            {
                "actor": "operator",
                "reason": "approved objective36 chain",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved.get("approved_by"), "operator")

        status, listed = get_json("/workspace/chains")
        self.assertEqual(status, 200, listed)
        self.assertTrue(any(int(item.get("chain_id", 0)) == chain_id for item in listed.get("chains", [])))

        status, fetched = get_json(f"/workspace/chains/{chain_id}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(int(fetched["chain_id"]), chain_id)

        status, advanced = post_json(
            f"/workspace/chains/{chain_id}/advance",
            {
                "actor": "workspace",
                "reason": "objective36 advance",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, advanced)
        self.assertEqual(advanced["status"], "completed")
        self.assertTrue(
            len(advanced.get("completed_step_ids", [])) >= 1
            or int(advanced.get("current_step_index", 0)) >= 1
        )

        status, audit = get_json(f"/workspace/chains/{chain_id}/audit")
        self.assertEqual(status, 200, audit)
        events = [str(item.get("event", "")) for item in audit.get("audit_trail", [])]
        self.assertIn("chain_created", events)
        self.assertIn("chain_approved", events)
        self.assertIn("chain_advanced", events)

        # Cooldown chain: second advance blocked by chain-level cooldown.
        status, cooldown_chain = post_json(
            "/workspace/chains",
            {
                "actor": "workspace",
                "reason": "objective36 cooldown chain",
                "chain_type": "proposal_sequence",
                "proposal_ids": [proposal_ids[0], proposal_ids[1]],
                "source": "objective36-test",
                "requires_approval": False,
                "cooldown_seconds": 60,
                "stop_on_failure": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, cooldown_chain)
        cooldown_chain_id = int(cooldown_chain["chain_id"])

        if str(cooldown_chain.get("status", "")) == "pending_approval":
            status, cooldown_approved = post_json(
                f"/workspace/chains/{cooldown_chain_id}/approve",
                {
                    "actor": "operator",
                    "reason": "approved objective36 cooldown chain",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, cooldown_approved)
            self.assertEqual(cooldown_approved.get("status"), "active", cooldown_approved)

        status, cooldown_first = post_json(
            f"/workspace/chains/{cooldown_chain_id}/advance",
            {
                "actor": "workspace",
                "reason": "cooldown first advance",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, cooldown_first)
        self.assertGreaterEqual(int(cooldown_first.get("current_step_index", 0)), 1)

        status, cooldown_blocked = post_json(
            f"/workspace/chains/{cooldown_chain_id}/advance",
            {
                "actor": "workspace",
                "reason": "cooldown blocked advance",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 429, cooldown_blocked)

        # Stop-on-failure: policy maps current status to failure and terminates chain.
        failure_target = next((item for item in all_proposals if int(item.get("proposal_id", 0)) == pending_ids[0]), None)
        self.assertIsNotNone(failure_target)
        assert failure_target is not None
        failure_status = str(failure_target.get("status", "pending")).strip().lower() or "pending"

        status, failure_chain = post_json(
            "/workspace/chains",
            {
                "actor": "workspace",
                "reason": "objective36 failure policy chain",
                "chain_type": "proposal_sequence",
                "proposal_ids": [pending_ids[0]],
                "source": "objective36-test",
                "requires_approval": False,
                "cooldown_seconds": 0,
                "stop_on_failure": True,
                "step_policy_json": {
                    "terminal_statuses": [failure_status],
                    "failure_statuses": [failure_status],
                },
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, failure_chain)
        failure_chain_id = int(failure_chain["chain_id"])

        if str(failure_chain.get("status", "")) == "pending_approval":
            status, failure_approved = post_json(
                f"/workspace/chains/{failure_chain_id}/approve",
                {
                    "actor": "operator",
                    "reason": "approved objective36 failure policy chain",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, failure_approved)
            self.assertEqual(failure_approved.get("status"), "active", failure_approved)

        status, failed = post_json(
            f"/workspace/chains/{failure_chain_id}/advance",
            {
                "actor": "workspace",
                "reason": "failure policy advance",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, failed)
        self.assertEqual(failed.get("status"), "failed")
        self.assertGreaterEqual(len(failed.get("failed_step_ids", [])), 1)

        time.sleep(0.05)

        status, filtered = get_json("/workspace/chains?status=completed")
        self.assertEqual(status, 200, filtered)
        self.assertTrue(any(int(item.get("chain_id", 0)) == chain_id for item in filtered.get("chains", [])))

        status, unknown = get_json("/workspace/chains/99999999")
        self.assertEqual(status, 404, unknown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
