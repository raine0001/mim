import json
import os
import unittest
import urllib.error
import urllib.request


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


class Objective23OperatorControlTest(unittest.TestCase):
    def test_operator_inbox_actions_and_audit(self) -> None:
        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": "workspace_check",
                "category": "diagnostic",
                "description": "Workspace check capability",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200)

        status, pending_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": "do something around there",
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
                "metadata_json": {"capability": "workspace_check", "trace_id": "obj23-pending"},
            },
        )
        self.assertEqual(status, 200, pending_event)
        pending_exec_id = pending_event["execution"]["execution_id"]

        status, blocked_event = post_json(
            "/gateway/voice/input",
            {
                "transcript": "move it there now",
                "parsed_intent": "execute_capability",
                "confidence": 0.95,
                "metadata_json": {"capability": "arm_movement", "trace_id": "obj23-blocked"},
            },
        )
        self.assertEqual(status, 200, blocked_event)
        blocked_exec_id = blocked_event["execution"]["execution_id"]

        status, failed_event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check failed path",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {"capability": "workspace_check", "trace_id": "obj23-failed"},
            },
        )
        self.assertEqual(status, 200, failed_event)
        failed_exec_id = failed_event["execution"]["execution_id"]

        status, _ = post_json(
            f"/gateway/capabilities/executions/{failed_exec_id}/feedback",
            {
                "status": "failed",
                "runtime_outcome": "unrecovered_failure",
                "actor": "tod",
                "feedback_json": {"error": "runtime exception"},
            },
        )
        self.assertEqual(status, 200)

        status, running_event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check running path",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {"capability": "workspace_check", "trace_id": "obj23-running"},
            },
        )
        self.assertEqual(status, 200, running_event)
        running_exec_id = running_event["execution"]["execution_id"]

        status, _ = post_json(
            f"/gateway/capabilities/executions/{running_exec_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "feedback_json": {},
            },
        )
        self.assertEqual(status, 200)

        status, _ = post_json(
            f"/gateway/capabilities/executions/{running_exec_id}/feedback",
            {
                "status": "running",
                "reason": "running",
                "actor": "tod",
                "feedback_json": {},
            },
        )
        self.assertEqual(status, 200)

        status, succeeded_event = post_json(
            "/gateway/intake/text",
            {
                "text": "run workspace check success path",
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
                "metadata_json": {"capability": "workspace_check", "trace_id": "obj23-succeeded"},
            },
        )
        self.assertEqual(status, 200, succeeded_event)
        succeeded_exec_id = succeeded_event["execution"]["execution_id"]

        status, _ = post_json(
            f"/gateway/capabilities/executions/{succeeded_exec_id}/feedback",
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "feedback_json": {},
            },
        )
        self.assertEqual(status, 200)

        status, _ = post_json(
            f"/gateway/capabilities/executions/{succeeded_exec_id}/feedback",
            {
                "status": "running",
                "reason": "running",
                "actor": "tod",
                "feedback_json": {},
            },
        )
        self.assertEqual(status, 200)

        status, _ = post_json(
            f"/gateway/capabilities/executions/{succeeded_exec_id}/feedback",
            {
                "status": "succeeded",
                "reason": "done",
                "actor": "tod",
                "feedback_json": {},
            },
        )
        self.assertEqual(status, 200)

        status, inbox = get_json("/operator/inbox")
        self.assertEqual(status, 200, inbox)
        counts = inbox["counts"]
        self.assertGreaterEqual(counts["pending_confirmations"], 1)
        self.assertGreaterEqual(counts["blocked"], 1)
        self.assertGreaterEqual(counts["failed"], 1)
        self.assertGreaterEqual(counts["active"], 1)
        self.assertGreaterEqual(counts["succeeded_recent"], 1)

        status, failed_detail = get_json(f"/operator/executions/{failed_exec_id}")
        self.assertEqual(status, 200, failed_detail)
        self.assertEqual(failed_detail["exception_reason"], "runtime_failure")

        status, blocked_detail = get_json(f"/operator/executions/{blocked_exec_id}")
        self.assertEqual(status, 200, blocked_detail)
        self.assertIn(blocked_detail["exception_reason"], {"blocked_by_policy", "missing_capability", "low_voice_confidence"})

        status, approved = post_json(
            f"/operator/executions/{pending_exec_id}/approve",
            {"actor": "operator", "reason": "approved for run", "metadata_json": {"ticket": "OBJ23-APPROVE"}},
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["status"], "dispatched")

        status, retried = post_json(
            f"/operator/executions/{failed_exec_id}/retry",
            {"actor": "operator", "reason": "retry requested", "metadata_json": {"ticket": "OBJ23-RETRY"}},
        )
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["status"], "dispatched")

        status, resumed = post_json(
            f"/operator/executions/{blocked_exec_id}/resume",
            {"actor": "operator", "reason": "manual resume", "metadata_json": {"ticket": "OBJ23-RESUME"}},
        )
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed["status"], "running")

        status, cancelled = post_json(
            f"/operator/executions/{running_exec_id}/cancel",
            {"actor": "operator", "reason": "stop active run", "metadata_json": {"ticket": "OBJ23-CANCEL"}},
        )
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["status"], "blocked")

        status, promoted = post_json(
            f"/operator/executions/{pending_exec_id}/promote-to-goal",
            {"actor": "operator", "reason": "promote proposal", "metadata_json": {"ticket": "OBJ23-PROMOTE"}},
        )
        self.assertEqual(status, 200, promoted)
        self.assertEqual(promoted["status"], "dispatched")

        status, execution_list = get_json("/operator/executions?status=dispatched,blocked,running")
        self.assertEqual(status, 200, execution_list)
        self.assertTrue(any(item["execution_id"] == failed_exec_id for item in execution_list))

        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        operator_entries = [entry for entry in journal if str(entry.get("action", "")).startswith("operator_")]
        self.assertTrue(any(entry.get("target_id") == str(pending_exec_id) for entry in operator_entries))


if __name__ == "__main__":
    unittest.main(verbosity=2)
