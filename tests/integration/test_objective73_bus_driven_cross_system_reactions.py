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


class Objective73BusDrivenCrossSystemReactionsTest(unittest.TestCase):
    def test_objective73_bus_driven_cross_system_reactions(self) -> None:
        run_id = uuid4().hex[:8]
        stream_key = f"objective73:{run_id}"
        consumer_key = "mim-tod-reaction-core"

        def append_event(domain: str, event_type: str, source: str, payload_extra: dict | None = None) -> dict:
            status, body = post_json(
                "/state-bus/events",
                {
                    "actor": "objective73-test",
                    "source": source,
                    "event_domain": domain,
                    "event_type": event_type,
                    "stream_key": stream_key,
                    "payload_json": {"run_id": run_id, **(payload_extra or {})},
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, body)
            event = body.get("event", {}) if isinstance(body, dict) else {}
            self.assertGreater(int(event.get("event_id", 0) or 0), 0)
            return event

        event1 = append_event("tod.runtime", "execution.completed", "tod-system", {"result": "ok"})
        event2 = append_event("tod.runtime", "execution.failed", "tod-system", {"result": "fail"})
        event3 = append_event("mim.perception", "camera.detected", "vision-system", {"zone": "front"})

        first_event_id = int(event1.get("event_id", 0) or 0)
        snapshot_scope = f"objective73-snapshot:{run_id}"
        encoded_scope = urllib.parse.quote(snapshot_scope, safe="")
        status, snapshot_upsert = post_json(
            f"/state-bus/snapshots/{encoded_scope}",
            {
                "actor": "objective73-test",
                "source": "objective73",
                "state_payload_json": {"cursor": first_event_id, "run_id": run_id},
                "last_event_id": first_event_id,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, snapshot_upsert)

        status, step1 = post_json(
            "/state-bus/reactions/mim-tod/step",
            {
                "actor": "objective73-test",
                "limit": 50,
                "metadata_json": {
                    "run_id": run_id,
                    "subscription_stream_keys": [stream_key],
                },
            },
        )
        self.assertEqual(status, 200, step1)
        self.assertEqual(str(step1.get("consumer", {}).get("consumer_key", "")), consumer_key)
        self.assertGreaterEqual(int(step1.get("consumed_count", 0) or 0), 3)
        self.assertGreaterEqual(int(step1.get("produced_count", 0) or 0), 3)

        status, reaction_events_payload = get_json("/state-bus/events?stream_key=reaction:mim-tod-reaction-core&limit=200")
        self.assertEqual(status, 200, reaction_events_payload)
        reaction_events = reaction_events_payload.get("events", []) if isinstance(reaction_events_payload, dict) else []
        reaction_for_run = [
            item
            for item in reaction_events
            if isinstance(item, dict)
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
        ]
        self.assertGreaterEqual(len(reaction_for_run), 3)

        event_types = {str(item.get("event_type", "")) for item in reaction_for_run}
        self.assertIn("tod.execution.completed_observed", event_types)
        self.assertIn("tod.execution.failure_attention_required", event_types)
        self.assertIn("perception.observation_received", event_types)

        status, replay = post_json(
            f"/state-bus/consumers/{consumer_key}/replay",
            {
                "actor": "objective73-test",
                "from_snapshot_scope": snapshot_scope,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, replay)

        status, step2 = post_json(
            "/state-bus/reactions/mim-tod/step",
            {
                "actor": "objective73-test",
                "limit": 50,
                "metadata_json": {
                    "run_id": run_id,
                    "subscription_stream_keys": [stream_key],
                    "phase": "replay-idempotency",
                },
            },
        )
        self.assertEqual(status, 200, step2)
        self.assertEqual(int(step2.get("produced_count", 0) or 0), 0)

        status, consumer_payload = get_json(f"/state-bus/consumers/{consumer_key}")
        self.assertEqual(status, 200, consumer_payload)
        consumer = consumer_payload.get("consumer", {}) if isinstance(consumer_payload, dict) else {}
        self.assertEqual(str(consumer.get("status", "")), "active")
        self.assertGreaterEqual(int(consumer.get("ack_count", 0) or 0), 3)


if __name__ == "__main__":
    unittest.main()
