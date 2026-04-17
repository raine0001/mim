import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
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


class Objective79PeopleInteractionConversationMemoryTest(unittest.TestCase):
    def _post_turn(self, session_id: str, text: str) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "discussion",
                "confidence": 0.93,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "user_id": "operator",
                },
            },
        )

    def test_objective79_people_preferences_and_conversation_memory_are_persisted(
        self,
    ) -> None:
        session_id = f"objective79-{uuid.uuid4()}"

        for text in [
            "call me Jordan",
            "I like concise answers",
            "what should we prioritize next?",
            "and after that?",
        ]:
            status, payload = self._post_turn(session_id, text)
            self.assertEqual(status, 200, payload)
            resolution = (
                payload.get("resolution", {}) if isinstance(payload, dict) else {}
            )
            self.assertTrue(str(resolution.get("clarification_prompt", "")).strip())

        status, display_name = get_json(
            "/preferences/display_name",
            {"user_id": "operator"},
        )
        self.assertEqual(status, 200, display_name)
        self.assertEqual(str(display_name.get("value", "")), "Jordan")

        status, likes = get_json(
            "/preferences/conversation_likes",
            {"user_id": "operator"},
        )
        self.assertEqual(status, 200, likes)
        self.assertIn("concise answers", [str(item) for item in likes.get("value", [])])

        status, person_payload = get_json("/memory/people/operator")
        self.assertEqual(status, 200, person_payload)
        person = (
            person_payload.get("person", {}) if isinstance(person_payload, dict) else {}
        )
        self.assertEqual(str(person.get("display_name", "")), "Jordan")
        self.assertIn("Jordan", person.get("aliases", []))
        self.assertTrue(person.get("profile_memory"))

        recent_memories = (
            person_payload.get("recent_memories", [])
            if isinstance(person_payload, dict)
            else []
        )
        self.assertTrue(
            any(
                str(item.get("memory_class", "")) == "person_preference"
                for item in recent_memories
            ),
            recent_memories,
        )

        status, conversations = get_json("/memory/conversations")
        self.assertEqual(status, 200, conversations)
        conversation_rows = (
            conversations.get("conversations", [])
            if isinstance(conversations, dict)
            else []
        )
        self.assertTrue(
            any(
                str((item.get("metadata_json", {}) or {}).get("session_id", ""))
                == session_id
                for item in conversation_rows
            ),
            conversation_rows,
        )

        status, conversation_detail = get_json(f"/memory/conversations/{session_id}")
        self.assertEqual(status, 200, conversation_detail)
        conversation = (
            conversation_detail.get("conversation", {})
            if isinstance(conversation_detail, dict)
            else {}
        )
        self.assertEqual(
            str((conversation.get("metadata_json", {}) or {}).get("session_id", "")),
            session_id,
        )
        self.assertIn(
            "priorities",
            str((conversation.get("metadata_json", {}) or {}).get("last_topic", "")),
        )

        turns = (
            conversation_detail.get("turns", [])
            if isinstance(conversation_detail, dict)
            else []
        )
        self.assertGreaterEqual(len(turns), 8, turns)
        self.assertTrue(
            any(str(turn.get("speaker", "")) == "user" for turn in turns), turns
        )
        self.assertTrue(
            any(str(turn.get("speaker", "")) == "assistant" for turn in turns), turns
        )
        self.assertTrue(
            any(
                "Jordan" in str(turn.get("display_name", ""))
                for turn in turns
                if str(turn.get("speaker", "")) == "user"
            ),
            turns,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
