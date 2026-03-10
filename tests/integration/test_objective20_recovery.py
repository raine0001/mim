import json
import unittest
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:8001"


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


def get_json(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective20RecoveryTest(unittest.TestCase):
    def test_retry_skip_replace_resume_and_graph_guards(self) -> None:
        status, goal = post_json(
            "/goals",
            {
                "goal_type": "objective20_recovery_test",
                "goal_description": "Validate recovery chain behaviors",
                "requested_by": "test",
                "priority": "high",
                "status": "new",
            },
        )
        self.assertEqual(status, 200, goal)
        goal_id = goal["goal_id"]

        status, step1 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "engine-a",
                "action_type": "step-1",
                "input_ref": "chain://s1",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 1,
                "status": "completed",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 0}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 1}},
            },
        )
        self.assertEqual(status, 200, step1)

        status, step2 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "engine-a",
                "action_type": "step-2",
                "input_ref": "chain://s2",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 2,
                "depends_on_action_id": step1["action_id"],
                "parent_action_id": step1["action_id"],
                "status": "failed",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 1}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 1}},
            },
        )
        self.assertEqual(status, 200, step2)

        status, step3 = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "engine-a",
                "action_type": "step-3",
                "input_ref": "chain://s3",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "sequence_index": 3,
                "depends_on_action_id": step2["action_id"],
                "status": "blocked",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 1}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 1}},
            },
        )
        self.assertEqual(status, 200, step3)

        status, dup_seq = post_json(
            "/actions",
            {
                "goal_id": goal_id,
                "engine": "engine-a",
                "action_type": "dup-seq",
                "input_ref": "chain://dup",
                "expected_state_delta": {},
                "validation_method": "expected_delta_compare",
                "sequence_index": 3,
                "status": "completed",
                "pre_state": {"state_type": "state", "state_payload": {}},
                "post_state": {"state_type": "state", "state_payload": {}},
            },
        )
        self.assertEqual(status, 422, dup_seq)

        status, retry = post_json(
            f"/actions/{step2['action_id']}/retry",
            {
                "engine": "engine-b",
                "action_type": "step-2-retry",
                "input_ref": "chain://s2/retry",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "status": "completed",
                "recovery_classification": "recovered",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 1}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 2}},
            },
        )
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry["retry_of_action_id"], step2["action_id"])
        self.assertEqual(retry["retry_count"], 1)
        self.assertEqual(retry["chain_event"], "retry")

        status, skipped = post_json(
            f"/actions/{step3['action_id']}/skip",
            {"reason": "intentional_skip", "continue_to_next_step": True},
        )
        self.assertEqual(status, 200, skipped)
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["chain_event"], "skip")

        status, replacement = post_json(
            f"/actions/{step2['action_id']}/replace",
            {
                "engine": "engine-c",
                "action_type": "step-2-replacement",
                "input_ref": "chain://s2/replacement",
                "expected_state_delta": {"counter": 1},
                "validation_method": "expected_delta_compare",
                "status": "completed",
                "recovery_classification": "recovered",
                "pre_state": {"state_type": "counter", "state_payload": {"counter": 2}},
                "post_state": {"state_type": "counter", "state_payload": {"counter": 3}},
            },
        )
        self.assertEqual(status, 200, replacement)
        self.assertEqual(replacement["replaced_action_id"], step2["action_id"])
        self.assertEqual(replacement["chain_event"], "replace")

        status, resumed = post_json(
            f"/goals/{goal_id}/resume",
            {"recovery_classification": "recovered"},
        )
        self.assertEqual(status, 200, resumed)

        status, plan = get_json(f"/goals/{goal_id}/plan")
        self.assertEqual(status, 200, plan)
        self.assertGreaterEqual(len(plan["ordered_action_ids"]), 6)

        status, timeline = get_json(f"/goals/{goal_id}/timeline")
        self.assertEqual(status, 200, timeline)
        events = [item["action"]["chain_event"] for item in timeline["timeline"]]
        self.assertIn("retry", events)
        self.assertIn("skip", events)
        self.assertIn("replace", events)
        self.assertIn("resume", events)

        status, goal_status = get_json(f"/goals/{goal_id}/status")
        self.assertEqual(status, 200, goal_status)
        self.assertIn(goal_status["derived_status"], {"recovered", "partial", "achieved"})
        self.assertGreaterEqual(goal_status["recovered_steps"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
