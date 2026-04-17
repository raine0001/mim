import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
import uuid


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
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


class Objective153ConversationSessionBridgeTest(unittest.TestCase):
    def _post_turn(self, session_id: str, text: str) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "discussion",
                "confidence": 0.94,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "user_id": "operator",
                },
            },
        )

    def test_gateway_text_turns_are_persisted_into_interface_session(self) -> None:
        session_id = f"objective153-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, payload)
        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        self.assertTrue(str(resolution.get("clarification_prompt", "")).strip())

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(session.get("session_key", "")), session_id)
        self.assertEqual(str(session.get("channel", "")), "text")
        self.assertGreaterEqual(int(context.get("turn_count", 0) or 0), 2)
        self.assertEqual(
            str(context.get("last_user_input", "")),
            "what should we prioritize next?",
        )
        self.assertEqual(str(context.get("last_parsed_intent", "")), "discussion")
        self.assertEqual(str(context.get("last_internal_intent", "")), "speak_response")
        self.assertTrue(str(context.get("last_prompt", "")).strip())
        self.assertEqual(str(context.get("last_topic", "")), "priorities")
        self.assertTrue(bool(context.get("last_proposed_actions", [])))

        status, messages_payload = get_json(
            f"/interface/sessions/{encoded_session}/messages",
            {"limit": 20},
        )
        self.assertEqual(status, 200, messages_payload)
        messages = (
            messages_payload.get("messages", [])
            if isinstance(messages_payload, dict)
            else []
        )
        self.assertGreaterEqual(len(messages), 2, messages)
        relevant = [
            item
            for item in messages
            if isinstance(item, dict)
            and str(item.get("content", "")).strip()
            in {
                "what should we prioritize next?",
                str(context.get("last_prompt", "")).strip(),
            }
        ]
        self.assertGreaterEqual(len(relevant), 2, messages)

    def test_followup_retry_reuses_last_action_request_from_session_context(self) -> None:
        session_id = f"objective153-retry-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, followup_payload = self._post_turn(
            session_id,
            "do that again but slower",
        )
        self.assertEqual(status, 200, followup_payload)
        resolution = (
            followup_payload.get("resolution", {})
            if isinstance(followup_payload, dict)
            else {}
        )
        prompt = str(resolution.get("clarification_prompt", ""))
        self.assertIn("open the dashboard", prompt)
        self.assertIn("slower", prompt)

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(
            str(context.get("last_action_request", "")),
            "open the dashboard",
        )
        self.assertGreaterEqual(int(context.get("turn_count", 0) or 0), 4)
        self.assertIn(
            str(context.get("last_control_state", "")),
            {"active", "confirmed", "stopped"},
        )

    def test_confirm_followup_promotes_pending_session_action_into_goal(self) -> None:
        session_id = f"objective153-confirm-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, followup_payload = self._post_turn(
            session_id,
            "do that again but slower",
        )
        self.assertEqual(status, 200, followup_payload)

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)
        self.assertIn(
            "retry open the dashboard at a slower pace",
            str(resolution.get("proposed_goal_description", "")),
        )
        self.assertIn(
            "Confirmed. I created a goal for:",
            str(resolution.get("clarification_prompt", "")),
        )

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_internal_intent", "")), "create_goal")
        self.assertEqual(str(context.get("last_control_state", "")), "confirmed")
        self.assertIn(
            "retry open the dashboard at a slower pace",
            str(context.get("pending_action_request", "")),
        )

    def test_revise_followup_replaces_pending_session_action_before_confirm(self) -> None:
        session_id = f"objective153-revise-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, revise_payload = self._post_turn(
            session_id,
            "change it to open the reports page",
        )
        self.assertEqual(status, 200, revise_payload)
        revise_resolution = (
            revise_payload.get("resolution", {})
            if isinstance(revise_payload, dict)
            else {}
        )
        self.assertIn(
            "updated the pending action",
            str(revise_resolution.get("clarification_prompt", "")).lower(),
        )

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertIn(
            "open the reports page",
            str(resolution.get("proposed_goal_description", "")),
        )

    def test_cancel_followup_clears_pending_session_action_before_confirm(self) -> None:
        session_id = f"objective153-cancel-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, cancel_payload = self._post_turn(session_id, "cancel it")
        self.assertEqual(status, 200, cancel_payload)
        cancel_resolution = (
            cancel_payload.get("resolution", {})
            if isinstance(cancel_payload, dict)
            else {}
        )
        self.assertIn(
            "cancelled",
            str(cancel_resolution.get("clarification_prompt", "")).lower(),
        )

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "speak_response")
        self.assertEqual(str(resolution.get("outcome", "")), "store_only")
        self.assertFalse(bool(resolution.get("goal_id")))

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_control_state", "")), "cancelled")
        self.assertEqual(str(context.get("pending_action_request", "")), "")

    def test_pause_then_resume_controls_when_pending_action_can_be_confirmed(self) -> None:
        session_id = f"objective153-pause-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, pause_payload = self._post_turn(session_id, "pause")
        self.assertEqual(status, 200, pause_payload)
        pause_resolution = (
            pause_payload.get("resolution", {}) if isinstance(pause_payload, dict) else {}
        )
        self.assertIn("paused", str(pause_resolution.get("clarification_prompt", "")).lower())

        status, blocked_confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, blocked_confirm_payload)
        blocked_resolution = (
            blocked_confirm_payload.get("resolution", {})
            if isinstance(blocked_confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(blocked_resolution.get("internal_intent", "")), "speak_response")
        self.assertEqual(str(blocked_resolution.get("outcome", "")), "store_only")
        self.assertIn("say resume", str(blocked_resolution.get("clarification_prompt", "")).lower())

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_control_state", "")), "paused")
        self.assertEqual(str(context.get("pending_action_request", "")), "open the dashboard")

        status, resume_payload = self._post_turn(session_id, "resume")
        self.assertEqual(status, 200, resume_payload)

        status, confirm_payload = self._post_turn(session_id, "confirm")
        self.assertEqual(status, 200, confirm_payload)
        resolution = (
            confirm_payload.get("resolution", {})
            if isinstance(confirm_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)

    def test_short_affirmation_approves_pending_action_from_clarification_state(self) -> None:
        session_id = f"objective153-affirm-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "open the dashboard")
        self.assertEqual(status, 200, first_payload)

        status, approval_payload = self._post_turn(session_id, "yes")
        self.assertEqual(status, 200, approval_payload)
        resolution = (
            approval_payload.get("resolution", {})
            if isinstance(approval_payload, dict)
            else {}
        )
        self.assertEqual(str(resolution.get("internal_intent", "")), "create_goal")
        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertTrue(int(resolution.get("goal_id", 0) or 0) > 0)
        self.assertIn(
            "open the dashboard",
            str(resolution.get("proposed_goal_description", "")),
        )

    def test_precision_prompt_accepts_terse_status_followup(self) -> None:
        session_id = f"objective153-precision-{uuid.uuid4()}"

        status, first_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertEqual(
            str(first_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )
        self.assertIn(
            "one specific request",
            str(first_resolution.get("clarification_prompt", "")).lower(),
        )

        status, second_payload = self._post_turn(session_id, "status")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        self.assertIn(
            "one-line status:",
            str(second_resolution.get("clarification_prompt", "")).lower(),
        )

    def test_precision_prompt_preserves_prior_topic_for_terse_after_followup(self) -> None:
        session_id = f"objective153-precision-topic-{uuid.uuid4()}"
        encoded_session = urllib.parse.quote(session_id, safe="")

        status, first_payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertIn(
            "top priority today",
            str(first_resolution.get("clarification_prompt", "")).lower(),
        )

        status, second_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )

        status, session_payload = get_json(f"/interface/sessions/{encoded_session}")
        self.assertEqual(status, 200, session_payload)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session, dict) else {}
        self.assertEqual(str(context.get("last_topic", "")), "priorities")

        status, third_payload = self._post_turn(session_id, "after")
        self.assertEqual(status, 200, third_payload)
        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        self.assertEqual(
            str(third_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        third_prompt = str(third_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("after that", third_prompt)
        self.assertIn("regression", third_prompt)

    def test_precision_prompt_accepts_terse_recap_followup_from_prior_topic(self) -> None:
        session_id = f"objective153-precision-recap-{uuid.uuid4()}"

        status, first_payload = self._post_turn(
            session_id,
            "what should we prioritize next?",
        )
        self.assertEqual(status, 200, first_payload)

        status, second_payload = self._post_turn(session_id, "ok")
        self.assertEqual(status, 200, second_payload)
        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_precision_prompt",
        )

        status, third_payload = self._post_turn(session_id, "recap")
        self.assertEqual(status, 200, third_payload)
        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        self.assertEqual(
            str(third_resolution.get("reason", "")),
            "conversation_clarification_followup",
        )
        third_prompt = str(third_resolution.get("clarification_prompt", "")).lower()
        self.assertIn("one line:", third_prompt)
        self.assertIn("stabilize routing", third_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)