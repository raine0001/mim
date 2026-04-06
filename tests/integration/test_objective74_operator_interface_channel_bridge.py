import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
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


class Objective74OperatorInterfaceChannelBridgeTest(unittest.TestCase):
    def test_objective74_operator_interface_channel_bridge(self) -> None:
        run_id = uuid4().hex[:8]
        session_key = f"objective74-{run_id}"
        encoded_session = urllib.parse.quote(session_key, safe="")

        status, upsert = post_json(
            f"/interface/sessions/{encoded_session}",
            {
                "actor": "objective74-test",
                "source": "objective74",
                "channel": "text",
                "status": "active",
                "context_json": {"run_id": run_id, "purpose": "channel-bridge"},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, upsert)
        session = upsert.get("session", {}) if isinstance(upsert, dict) else {}
        self.assertEqual(str(session.get("session_key", "")), session_key)

        status, message_res = post_json(
            f"/interface/sessions/{encoded_session}/messages",
            {
                "actor": "objective74-test",
                "source": "objective74",
                "direction": "inbound",
                "role": "operator",
                "content": "Please verify shared export path and propose safe fix",
                "parsed_intent": "path_validation_request",
                "confidence": 0.94,
                "requires_approval": True,
                "metadata_json": {"run_id": run_id, "task": "shared-path-check"},
            },
        )
        self.assertEqual(status, 200, message_res)
        message = message_res.get("message", {}) if isinstance(message_res, dict) else {}
        self.assertGreater(int(message.get("message_id", 0) or 0), 0)
        self.assertTrue(bool(message.get("requires_approval", False)))

        message_id = int(message.get("message_id", 0) or 0)

        status, approval_res = post_json(
            f"/interface/sessions/{encoded_session}/approvals",
            {
                "actor": "objective74-test",
                "source": "objective74",
                "message_id": message_id,
                "decision": "approved",
                "reason": "bounded integration work approved",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, approval_res)
        approval = approval_res.get("approval", {}) if isinstance(approval_res, dict) else {}
        self.assertEqual(str(approval.get("decision", "")), "approved")

        status, listed_sessions = get_json(f"/interface/sessions?limit=200&channel=text")
        self.assertEqual(status, 200, listed_sessions)
        sessions = listed_sessions.get("sessions", []) if isinstance(listed_sessions, dict) else []
        found = [item for item in sessions if isinstance(item, dict) and str(item.get("session_key", "")) == session_key]
        self.assertTrue(bool(found))

        status, listed_messages = get_json(f"/interface/sessions/{encoded_session}/messages?limit=200")
        self.assertEqual(status, 200, listed_messages)
        messages = listed_messages.get("messages", []) if isinstance(listed_messages, dict) else []
        relevant = [
            item
            for item in messages
            if isinstance(item, dict)
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
        ]
        self.assertTrue(bool(relevant))

        status, events_payload = get_json("/state-bus/events?event_domain=mim.assist&limit=300")
        self.assertEqual(status, 200, events_payload)
        events = events_payload.get("events", []) if isinstance(events_payload, dict) else []
        approval_events = [
            item
            for item in events
            if isinstance(item, dict)
            and str(item.get("event_type", "")) == "interface.approval.approved"
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
        ]
        self.assertTrue(bool(approval_events))


if __name__ == "__main__":
    unittest.main()
