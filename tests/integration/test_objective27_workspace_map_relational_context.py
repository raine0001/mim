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


class Objective27WorkspaceMapRelationalContextTest(unittest.TestCase):
    def test_workspace_map_relational_context_and_spatial_hints(self) -> None:
        run_id = uuid4().hex[:8]
        obj_a = f"blue_block_obj27_{run_id}"
        obj_b = f"red_cup_obj27_{run_id}"

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

        status, zone_map = get_json("/workspace/map")
        self.assertEqual(status, 200, zone_map)
        self.assertGreaterEqual(len(zone_map.get("zones", [])), 6)
        self.assertGreaterEqual(len(zone_map.get("relations", [])), 1)

        status, zones = get_json("/workspace/map/zones")
        self.assertEqual(status, 200, zones)
        self.assertGreaterEqual(len(zones.get("zones", [])), 6)

        def run_scan(scan_area: str, observations: list[dict]) -> int:
            status, event = post_json(
                "/gateway/intake/text",
                {
                    "text": f"scan workspace {scan_area}",
                    "parsed_intent": "observe_workspace",
                    "confidence": 0.95,
                    "metadata_json": {
                        "scan_mode": "full",
                        "scan_area": scan_area,
                        "confidence_threshold": 0.65,
                    },
                },
            )
            self.assertEqual(status, 200, event)
            execution_id = event["execution"]["execution_id"]
            for step in ["accepted", "running"]:
                status, step_resp = post_json(
                    f"/gateway/capabilities/executions/{execution_id}/feedback",
                    {"status": step, "reason": step, "actor": "tod", "feedback_json": {}},
                )
                self.assertEqual(status, 200, step_resp)

            status, done = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": "succeeded",
                    "reason": "scan complete",
                    "actor": "tod",
                    "feedback_json": {
                        "observations": observations,
                        "observation_confidence": 0.92,
                    },
                },
            )
            self.assertEqual(status, 200, done)
            return execution_id

        run_scan(
            "front-left",
            [
                {"label": obj_a, "zone": "front-left", "confidence": 0.93},
                {"label": obj_b, "zone": "front-left", "confidence": 0.91},
            ],
        )

        status, objects = get_json(f"/workspace/objects?label={obj_a}")
        self.assertEqual(status, 200, objects)
        target = objects.get("objects", [])[0]
        object_id = int(target["object_memory_id"])

        status, relations_near = get_json(f"/workspace/objects/{object_id}/relations")
        self.assertEqual(status, 200, relations_near)
        self.assertGreaterEqual(len(relations_near.get("relations", [])), 1)
        self.assertIn("near", {item.get("relation_type") for item in relations_near.get("relations", [])})

        run_scan(
            "front-center",
            [
                {"label": obj_a, "zone": "front-center", "confidence": 0.9},
                {"label": obj_b, "zone": "front-left", "confidence": 0.89},
            ],
        )

        status, moved = get_json(f"/workspace/objects/{object_id}")
        self.assertEqual(status, 200, moved)
        self.assertEqual(moved.get("zone"), "front-center")
        self.assertIn(moved.get("status"), {"uncertain", "active"})

        status, relations_far = get_json(f"/workspace/objects/{object_id}/relations")
        self.assertEqual(status, 200, relations_far)
        relation_types = {item.get("relation_type") for item in relations_far.get("relations", [])}
        self.assertTrue("far" in relation_types or "near" in relation_types)

        status, resolution_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace front-center for target",
                "parsed_intent": "observe_workspace",
                "confidence": 0.92,
                "metadata_json": {
                    "scan_mode": "quick",
                    "scan_area": "front-center",
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, resolution_event)
        resolution = resolution_event.get("resolution", {})
        self.assertIn(
            resolution.get("reason"),
            {
                "memory_spatial_change_requires_reconfirm",
                "memory_object_uncertain_requires_reconfirm",
                "memory_confident_recent_identity",
                "memory_stale_requires_reconfirm",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
