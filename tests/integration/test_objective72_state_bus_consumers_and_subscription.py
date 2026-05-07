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


class Objective72StateBusConsumersTest(unittest.TestCase):
    def test_objective72_state_bus_consumers_and_subscription(self) -> None:
        run_id = uuid4().hex[:8]
        stream_key = f"objective72:{run_id}"
        snapshot_scope = f"objective72-snapshot:{run_id}"
        consumer_key = f"consumer-{run_id}"

        def append_event(domain: str, event_type: str, source: str, payload_extra: dict | None = None) -> dict:
            status, body = post_json(
                "/state-bus/events",
                {
                    "actor": "objective72-test",
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
        _event2 = append_event("tod.runtime", "execution.failed", "tod-system", {"result": "fail"})
        _event3 = append_event("tod.runtime", "execution.completed", "other-system", {"result": "ok"})
        _event4 = append_event("mim.perception", "camera.detected", "vision-system", {"zone": "front"})

        event1_id = int(event1.get("event_id", 0) or 0)

        encoded_scope = urllib.parse.quote(snapshot_scope, safe="")
        status, snapshot_upsert = post_json(
            f"/state-bus/snapshots/{encoded_scope}",
            {
                "actor": "objective72-test",
                "source": "objective72",
                "state_payload_json": {"cursor": event1_id, "run_id": run_id},
                "last_event_id": event1_id,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, snapshot_upsert)

        status, register = post_json(
            f"/state-bus/consumers/{consumer_key}",
            {
                "actor": "objective72-test",
                "source": "objective72",
                "status": "active",
                "subscription_domains": ["tod.runtime"],
                "subscription_event_types": ["execution.completed"],
                "subscription_sources": ["tod-system"],
                "subscription_stream_keys": [stream_key],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, register)
        consumer = register.get("consumer", {}) if isinstance(register, dict) else {}
        self.assertEqual(str(consumer.get("consumer_key", "")), consumer_key)

        status, poll1 = post_json(f"/state-bus/consumers/{consumer_key}/poll", {"limit": 20})
        self.assertEqual(status, 200, poll1)
        events1 = poll1.get("events", []) if isinstance(poll1, dict) else []
        self.assertEqual(len(events1), 1)
        delivered_event_id = int(events1[0].get("event_id", 0) or 0)
        self.assertEqual(delivered_event_id, event1_id)

        status, ack1 = post_json(
            f"/state-bus/consumers/{consumer_key}/ack",
            {
                "actor": "objective72-test",
                "event_ids": [delivered_event_id],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, ack1)
        accepted1 = ack1.get("accepted_event_ids", []) if isinstance(ack1, dict) else []
        self.assertEqual(accepted1, [delivered_event_id])

        status, ack2 = post_json(
            f"/state-bus/consumers/{consumer_key}/ack",
            {
                "actor": "objective72-test",
                "event_ids": [delivered_event_id],
                "metadata_json": {"run_id": run_id, "idempotent": True},
            },
        )
        self.assertEqual(status, 200, ack2)
        accepted2 = ack2.get("accepted_event_ids", []) if isinstance(ack2, dict) else []
        self.assertEqual(accepted2, [])

        status, poll2 = post_json(f"/state-bus/consumers/{consumer_key}/poll", {"limit": 20})
        self.assertEqual(status, 200, poll2)
        events2 = poll2.get("events", []) if isinstance(poll2, dict) else []
        self.assertEqual(events2, [])

        event5 = append_event("tod.runtime", "execution.completed", "tod-system", {"result": "ok-later"})
        event5_id = int(event5.get("event_id", 0) or 0)

        status, replay = post_json(
            f"/state-bus/consumers/{consumer_key}/replay",
            {
                "actor": "objective72-test",
                "from_snapshot_scope": snapshot_scope,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, replay)
        replay_consumer = replay.get("consumer", {}) if isinstance(replay, dict) else {}
        self.assertEqual(int(replay_consumer.get("cursor_event_id", 0) or 0), event1_id)

        status, poll3 = post_json(f"/state-bus/consumers/{consumer_key}/poll", {"limit": 20})
        self.assertEqual(status, 200, poll3)
        events3 = poll3.get("events", []) if isinstance(poll3, dict) else []
        self.assertEqual(len(events3), 1)
        self.assertEqual(int(events3[0].get("event_id", 0) or 0), event5_id)

        _event6 = append_event("tod.runtime", "execution.completed", "tod-system", {"for": "mim-core"})
        status, mim_step = post_json(
            "/state-bus/consumers/mim-core/step",
            {
                "actor": "objective72-test",
                "limit": 50,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, mim_step)
        self.assertGreaterEqual(int(mim_step.get("consumed_count", 0) or 0), 1)
        self.assertGreaterEqual(int(mim_step.get("memory_written", 0) or 0), 1)

        status, memory_payload = get_json("/memory")
        self.assertEqual(status, 200, memory_payload)
        memory_rows = memory_payload if isinstance(memory_payload, list) else []
        relevant_memory = [
            item
            for item in memory_rows
            if isinstance(item, dict)
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
            and str(item.get("metadata_json", {}).get("consumer_key", "")) == "mim-core"
        ]
        self.assertTrue(bool(relevant_memory))

        status, strategy_events_payload = get_json("/state-bus/events?event_domain=mim.strategy&limit=200")
        self.assertEqual(status, 200, strategy_events_payload)
        strategy_events = strategy_events_payload.get("events", []) if isinstance(strategy_events_payload, dict) else []
        derived = [
            item
            for item in strategy_events
            if isinstance(item, dict)
            and str(item.get("event_type", "")) == "tod.execution.ingested"
            and isinstance(item.get("metadata_json", {}), dict)
            and str(item.get("metadata_json", {}).get("run_id", "")) == run_id
        ]
        self.assertTrue(bool(derived))

        status, consumers_payload = get_json("/state-bus/consumers?limit=200")
        self.assertEqual(status, 200, consumers_payload)
        consumers = consumers_payload.get("consumers", []) if isinstance(consumers_payload, dict) else []
        found_keys = {str(item.get("consumer_key", "")) for item in consumers if isinstance(item, dict)}
        self.assertIn(consumer_key, found_keys)
        self.assertIn("mim-core", found_keys)


if __name__ == "__main__":
    unittest.main()
