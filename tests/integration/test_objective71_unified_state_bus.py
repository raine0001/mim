import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
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


class Objective71UnifiedStateBusTest(unittest.TestCase):
    def _seed_strategy_event(self, run_id: str) -> None:
        status, intake = post_json(
            "/gateway/intake/text",
            {
                "text": f"Objective71 state-bus strategy seed {run_id}",
                "parsed_intent": "operator_request",
                "confidence": 0.9,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, intake)

        status, build = post_json(
            "/orchestration/build",
            {
                "actor": "objective71-test",
                "source": f"objective71-focused-{run_id}",
                "lookback_hours": 24,
                "max_items_per_domain": 50,
                "min_context_confidence": 0.95,
                "min_domains_required": 10,
                "dependency_resolution_policy": "ask",
                "collaboration_mode_preference": "auto",
                "task_kind": "mixed",
                "action_risk_level": "medium",
                "use_human_aware_signals": False,
                "generate_goal": False,
                "generate_horizon_plan": False,
                "generate_improvement_proposals": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, build)

    def test_objective71_unified_state_bus(self) -> None:
        run_id = uuid4().hex[:8]
        stream_key = f"sync:{run_id}"
        snapshot_scope = f"workspace:{run_id}"

        self._seed_strategy_event(run_id=run_id)

        status, strategy_events_payload = get_json("/state-bus/events?event_domain=mim.strategy&limit=200")
        self.assertEqual(status, 200, strategy_events_payload)
        strategy_events = strategy_events_payload.get("events", []) if isinstance(strategy_events_payload, dict) else []
        orchestration_events = [
            item
            for item in strategy_events
            if isinstance(item, dict) and str(item.get("event_type", "")) == "orchestration.built"
        ]
        self.assertTrue(bool(orchestration_events))

        status, first_event_payload = post_json(
            "/state-bus/events",
            {
                "actor": "objective71-test",
                "source": "objective71",
                "event_domain": "tod.runtime",
                "event_type": "execution.completed",
                "stream_key": stream_key,
                "payload_json": {"run_id": run_id, "result": "ok", "latency_ms": 120},
                "metadata_json": {"run_id": run_id, "objective": "71"},
            },
        )
        self.assertEqual(status, 200, first_event_payload)
        first_event = first_event_payload.get("event", {}) if isinstance(first_event_payload, dict) else {}
        self.assertEqual(str(first_event.get("event_domain", "")), "tod.runtime")
        self.assertEqual(int(first_event.get("sequence_id", 0) or 0), 1)

        status, second_event_payload = post_json(
            "/state-bus/events",
            {
                "actor": "objective71-test",
                "source": "objective71",
                "event_domain": "mim.improvement",
                "event_type": "proposal.generated",
                "stream_key": stream_key,
                "payload_json": {"run_id": run_id, "proposal_id": 999},
                "metadata_json": {"run_id": run_id, "objective": "71"},
            },
        )
        self.assertEqual(status, 200, second_event_payload)
        second_event = second_event_payload.get("event", {}) if isinstance(second_event_payload, dict) else {}
        self.assertEqual(str(second_event.get("event_domain", "")), "mim.improvement")
        self.assertEqual(int(second_event.get("sequence_id", 0) or 0), 2)

        encoded_stream = urllib.parse.quote(stream_key, safe="")
        status, stream_payload = get_json(f"/state-bus/events?stream_key={encoded_stream}&limit=20")
        self.assertEqual(status, 200, stream_payload)
        stream_events = stream_payload.get("events", []) if isinstance(stream_payload, dict) else []
        self.assertGreaterEqual(len(stream_events), 2)
        self.assertEqual(int(stream_events[0].get("sequence_id", 0) or 0), 2)

        second_event_id = int(second_event.get("event_id", 0) or 0)
        self.assertGreater(second_event_id, 0)
        status, event_by_id_payload = get_json(f"/state-bus/events/{second_event_id}")
        self.assertEqual(status, 200, event_by_id_payload)
        event_by_id = event_by_id_payload.get("event", {}) if isinstance(event_by_id_payload, dict) else {}
        self.assertEqual(int(event_by_id.get("event_id", 0) or 0), second_event_id)

        status, snapshot_payload = post_json(
            f"/state-bus/snapshots/{snapshot_scope}",
            {
                "actor": "objective71-test",
                "source": "objective71",
                "state_payload_json": {
                    "cursor": second_event_id,
                    "domains": ["tod.runtime", "mim.improvement"],
                    "run_id": run_id,
                },
                "last_event_id": second_event_id,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, snapshot_payload)
        snapshot = snapshot_payload.get("snapshot", {}) if isinstance(snapshot_payload, dict) else {}
        self.assertEqual(str(snapshot.get("snapshot_scope", "")), snapshot_scope)
        self.assertEqual(int(snapshot.get("state_version", 0) or 0), 1)
        self.assertEqual(int(snapshot.get("last_event_id", 0) or 0), second_event_id)

        status, snapshot_payload_2 = post_json(
            f"/state-bus/snapshots/{snapshot_scope}",
            {
                "actor": "objective71-test",
                "source": "objective71",
                "state_payload_json": {
                    "cursor": second_event_id,
                    "domains": ["tod.runtime", "mim.improvement", "mim.strategy"],
                    "run_id": run_id,
                    "mode": "updated",
                },
                "last_event_id": second_event_id,
                "metadata_json": {"run_id": run_id, "updated": True},
            },
        )
        self.assertEqual(status, 200, snapshot_payload_2)
        snapshot2 = snapshot_payload_2.get("snapshot", {}) if isinstance(snapshot_payload_2, dict) else {}
        self.assertEqual(int(snapshot2.get("state_version", 0) or 0), 2)

        encoded_scope = urllib.parse.quote(snapshot_scope, safe="")
        status, snapshots_list_payload = get_json(f"/state-bus/snapshots?snapshot_scope={encoded_scope}&limit=20")
        self.assertEqual(status, 200, snapshots_list_payload)
        snapshots = snapshots_list_payload.get("snapshots", []) if isinstance(snapshots_list_payload, dict) else []
        self.assertTrue(bool(snapshots))

        status, snapshot_by_scope_payload = get_json(f"/state-bus/snapshots/{encoded_scope}")
        self.assertEqual(status, 200, snapshot_by_scope_payload)
        snapshot_by_scope = snapshot_by_scope_payload.get("snapshot", {}) if isinstance(snapshot_by_scope_payload, dict) else {}
        self.assertEqual(str(snapshot_by_scope.get("snapshot_scope", "")), snapshot_scope)
        self.assertEqual(int(snapshot_by_scope.get("state_version", 0) or 0), 2)


if __name__ == "__main__":
    unittest.main()
