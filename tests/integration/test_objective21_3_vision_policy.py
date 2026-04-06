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


class Objective213VisionPolicyTest(unittest.TestCase):
    def test_vision_confidence_policy_outcomes(self) -> None:
        status, high_safe = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "scan table workspace clear",
                "detected_labels": ["table"],
                "confidence": 0.93,
                "proposed_goal": "observe workspace",
            },
        )
        self.assertEqual(status, 200, high_safe)
        res_high = high_safe["resolution"]
        self.assertEqual(res_high["confidence_tier"], "high")
        self.assertEqual(res_high["outcome"], "auto_execute")
        self.assertTrue(res_high["goal_id"] is not None)

        status, medium_ambiguous = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "detected ambiguous target candidates",
                "detected_labels": ["candidate_a", "candidate_b", "ambiguous_label"],
                "confidence": 0.72,
                "proposed_goal": "identify target object",
            },
        )
        self.assertEqual(status, 200, medium_ambiguous)
        res_medium = medium_ambiguous["resolution"]
        self.assertEqual(res_medium["confidence_tier"], "medium")
        self.assertEqual(res_medium["outcome"], "requires_confirmation")
        self.assertIn("ambiguous_label", res_medium["escalation_reasons"])
        self.assertIn("multiple_candidate_objects", res_medium["escalation_reasons"])

        status, low_conf = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "faint detection near edge",
                "detected_labels": ["unknown_blob"],
                "confidence": 0.29,
                "proposed_goal": "inspect edge",
            },
        )
        self.assertEqual(status, 200, low_conf)
        res_low = low_conf["resolution"]
        self.assertEqual(res_low["confidence_tier"], "low")
        self.assertEqual(res_low["outcome"], "store_only")
        self.assertEqual(res_low["goal_id"], None)
        self.assertIn("low_confidence_detection", res_low["escalation_reasons"])

        status, unsafe = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "detected obstacle near arm path",
                "detected_labels": ["obstacle"],
                "confidence": 0.94,
                "proposed_goal": "move arm through path",
                "metadata_json": {"capability": "arm_movement"},
            },
        )
        self.assertEqual(status, 200, unsafe)
        res_unsafe = unsafe["resolution"]
        self.assertEqual(res_unsafe["outcome"], "blocked")
        self.assertIn("unsafe_capability_implication", res_unsafe["escalation_reasons"])

        status, unknown = post_json(
            "/gateway/vision/observations",
            {
                "raw_observation": "unknown object detected in workspace",
                "detected_labels": ["unknown_object"],
                "confidence": 0.88,
                "proposed_goal": "identify unknown object",
            },
        )
        self.assertEqual(status, 200, unknown)
        unknown_event_id = unknown["input_id"]
        res_unknown = unknown["resolution"]
        self.assertIn("unknown_object", res_unknown["escalation_reasons"])
        self.assertNotEqual(res_unknown["outcome"], "store_only")

        status, unknown_resolution = get_json(f"/gateway/events/{unknown_event_id}/resolution")
        self.assertEqual(status, 200, unknown_resolution)
        self.assertIn("unknown_object", unknown_resolution["escalation_reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
