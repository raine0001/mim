import json
import os
import unittest
import urllib.error
import urllib.request


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


class Objective214VoicePolicyTest(unittest.TestCase):
    def test_voice_policy_and_output_contract(self) -> None:
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

        status, high = post_json(
            "/gateway/voice/input",
            {
                "transcript": "run workspace check",
                "parsed_intent": "observe_workspace",
                "confidence": 0.92,
                "requested_goal": "run workspace check",
                "metadata_json": {"capability": "workspace_check"},
            },
        )
        self.assertEqual(status, 200, high)
        high_res = high["resolution"]
        self.assertEqual(high_res["confidence_tier"], "high")
        self.assertEqual(high_res["outcome"], "auto_execute")
        self.assertTrue(high_res["goal_id"] is not None)

        status, ambiguous = post_json(
            "/gateway/voice/input",
            {
                "transcript": "do something around there",
                "parsed_intent": "execute_capability",
                "confidence": 0.73,
            },
        )
        self.assertEqual(status, 200, ambiguous)
        ambiguous_res = ambiguous["resolution"]
        self.assertEqual(ambiguous_res["outcome"], "requires_confirmation")
        self.assertIn("ambiguous_command", ambiguous_res["escalation_reasons"])
        self.assertTrue(ambiguous_res["clarification_prompt"])

        status, low = post_json(
            "/gateway/voice/input",
            {
                "transcript": "maybe",
                "parsed_intent": "execute_capability",
                "confidence": 0.31,
            },
        )
        self.assertEqual(status, 200, low)
        low_res = low["resolution"]
        self.assertEqual(low_res["confidence_tier"], "low")
        self.assertEqual(low_res["outcome"], "store_only")
        self.assertIn("low_transcript_confidence", low_res["escalation_reasons"])
        self.assertIsNone(low_res["goal_id"])

        status, unsafe = post_json(
            "/gateway/voice/input",
            {
                "transcript": "move it there now",
                "parsed_intent": "execute_capability",
                "confidence": 0.95,
                "metadata_json": {"capability": "arm_movement"},
            },
        )
        self.assertEqual(status, 200, unsafe)
        unsafe_res = unsafe["resolution"]
        self.assertEqual(unsafe_res["outcome"], "blocked")
        self.assertIn("unsafe_action_request", unsafe_res["escalation_reasons"])
        self.assertTrue(unsafe_res["clarification_prompt"])

        status, output_action = post_json(
            "/gateway/voice/output",
            {
                "message": ambiguous_res["clarification_prompt"],
                "voice_profile": "assistant",
                "priority": "normal",
                "channel": "speaker",
            },
        )
        self.assertEqual(status, 200, output_action)
        self.assertEqual(output_action["delivery_status"], "queued")
        self.assertEqual(output_action["priority"], "normal")

        status, blocked_output = post_json(
            "/gateway/voice/output",
            {
                "message": "x" * 500,
                "voice_profile": "assistant",
                "priority": "normal",
                "channel": "speaker",
            },
        )
        self.assertEqual(status, 200, blocked_output)
        self.assertEqual(blocked_output["delivery_status"], "blocked")
        self.assertEqual(blocked_output["failure_reason"], "output_too_long")

        status, voice_policy = get_json("/gateway/voice-policy")
        self.assertEqual(status, 200, voice_policy)
        self.assertEqual(voice_policy["policy_version"], "voice-policy-v1")
        self.assertIn("max_output_chars", voice_policy)

    def test_one_clarifier_then_options_fallback(self) -> None:
        first_transcript = "do something with that thing near there"
        second_transcript = "still do it around there now"

        status, turn1 = post_json(
            "/gateway/voice/input",
            {
                "transcript": first_transcript,
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
            },
        )
        self.assertEqual(status, 200, turn1)
        res1 = turn1["resolution"]
        prompt1 = str(res1.get("clarification_prompt", ""))
        self.assertTrue(prompt1)
        self.assertIn("missing one detail", prompt1.lower())
        self.assertIn("answer", prompt1.lower())
        self.assertIn("plan", prompt1.lower())
        self.assertIn("action", prompt1.lower())
        self.assertNotIn("clarification_limit_reached", res1.get("escalation_reasons", []))

        status, turn2 = post_json(
            "/gateway/voice/input",
            {
                "transcript": second_transcript,
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
            },
        )
        self.assertEqual(status, 200, turn2)
        res2 = turn2["resolution"]
        prompt2 = str(res2.get("clarification_prompt", ""))
        self.assertTrue(prompt2)
        self.assertIn("options: 1)", prompt2.lower())
        self.assertIn("clarification_limit_reached", res2.get("escalation_reasons", []))
        self.assertNotEqual(prompt1, prompt2)
        self.assertNotIn("do you want me to answer a question", prompt2.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
