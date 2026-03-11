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


class Objective26ObjectIdentityPersistenceTest(unittest.TestCase):
    def test_object_identity_matching_move_missing_and_queries(self) -> None:
        run_id = uuid4().hex[:8]
        table_zone = f"table_obj26_{run_id}"
        shelf_zone = f"shelf_obj26_{run_id}"
        label_primary = f"blue_block_obj26_{run_id}"
        label_alias = f"blue block obj26 {run_id}"
        label_other = f"green_pen_other_{uuid4().hex[:6]}"

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

            status, succeeded = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": "succeeded",
                    "reason": "scan complete",
                    "actor": "tod",
                    "feedback_json": {
                        "observations": observations,
                        "observation_confidence": 0.9,
                    },
                },
            )
            self.assertEqual(status, 200, succeeded)
            self.assertIn("workspace_object_ids", succeeded.get("feedback_json", {}))
            return execution_id

        run_scan(table_zone, [{"label": label_primary, "zone": table_zone, "confidence": 0.93}])

        status, objects_by_label = get_json(f"/workspace/objects?label={label_primary}")
        self.assertEqual(status, 200, objects_by_label)
        blue_objects = objects_by_label.get("objects", [])
        self.assertGreaterEqual(len(blue_objects), 1)

        blue_obj = blue_objects[0]
        object_id = int(blue_obj["object_memory_id"])
        self.assertEqual(blue_obj.get("zone"), table_zone)
        self.assertIn(blue_obj.get("status"), {"active", "uncertain"})

        run_scan(table_zone, [{"label": label_alias, "zone": table_zone, "confidence": 0.86}])
        status, after_match = get_json(f"/workspace/objects/{object_id}")
        self.assertEqual(status, 200, after_match)
        self.assertEqual(after_match.get("object_memory_id"), object_id)
        self.assertEqual(after_match.get("zone"), table_zone)
        self.assertIn(label_alias.lower(), [item.lower() for item in after_match.get("aliases", [])])

        run_scan(shelf_zone, [{"label": label_primary, "zone": shelf_zone, "confidence": 0.9}])
        status, after_move = get_json(f"/workspace/objects/{object_id}")
        self.assertEqual(status, 200, after_move)
        self.assertEqual(after_move.get("zone"), shelf_zone)
        self.assertEqual(after_move.get("status"), "uncertain")
        self.assertGreaterEqual(len(after_move.get("location_history", [])), 1)
        moved_confidence = float(after_move.get("confidence", 0.0))

        status, uncertain_resolution_event = post_json(
            "/gateway/intake/text",
            {
                "text": "scan workspace shelf",
                "parsed_intent": "observe_workspace",
                "confidence": 0.9,
                "metadata_json": {
                    "scan_mode": "quick",
                    "scan_area": shelf_zone,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, uncertain_resolution_event)
        resolution = uncertain_resolution_event["resolution"]
        self.assertEqual(resolution.get("reason"), "memory_object_uncertain_requires_reconfirm")
        self.assertEqual(resolution.get("outcome"), "requires_confirmation")

        run_scan(shelf_zone, [{"label": label_other, "zone": shelf_zone, "confidence": 0.82}])
        status, after_missing = get_json(f"/workspace/objects/{object_id}")
        self.assertEqual(status, 200, after_missing)
        self.assertIn(after_missing.get("status"), {"missing", "active"})
        self.assertLessEqual(float(after_missing.get("confidence", 1.0)), moved_confidence)

        status, list_by_label = get_json(f"/workspace/objects?label={run_id}")
        self.assertEqual(status, 200, list_by_label)
        self.assertGreaterEqual(len(list_by_label.get("objects", [])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
