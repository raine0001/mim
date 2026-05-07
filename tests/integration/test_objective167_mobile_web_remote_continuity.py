import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
import uuid

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)


def get_text(path: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.status, resp.read().decode("utf-8")


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, (dict, list)) else {"data": parsed}


def post_json(path: str, payload: dict, *, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=req_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


class Objective167MobileWebRemoteContinuityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 167",
            base_url=BASE_URL,
            require_mim=True,
            require_ui_state=True,
        )

    def _post_turn(self, session_id: str, text: str) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "discussion",
                "confidence": 0.94,
                "metadata_json": {
                    "source": "objective167_mobile_browser",
                    "conversation_session_id": session_id,
                    "route_preference": "conversation_layer",
                },
            },
            headers={"User-Agent": MOBILE_USER_AGENT},
        )

    def _reply_text(self, payload: dict) -> str:
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        reply = str(interface.get("reply_text", "")).strip()
        if reply:
            return reply
        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        return str(resolution.get("clarification_prompt", "")).strip()

    def test_mobile_web_path_preserves_session_and_handles_required_prompts(self) -> None:
        status, html = get_text(
            "/mim",
            headers={"User-Agent": MOBILE_USER_AGENT},
        )
        self.assertEqual(status, 200)
        self.assertIn('name="viewport"', html)
        self.assertIn("mim_text_chat_session_id", html)
        self.assertIn("/gateway/intake/text", html)

        session_id = f"objective167-mobile-web-{uuid.uuid4()}"

        catch_up_status, catch_up_payload = self._post_turn(session_id, "catch me up")
        self.assertEqual(catch_up_status, 200, catch_up_payload)
        catch_up_reply = self._reply_text(catch_up_payload)
        self.assertIn("while you were away:", catch_up_reply.lower())

        objective_status, objective_payload = self._post_turn(session_id, "what is the current objective?")
        self.assertEqual(objective_status, 200, objective_payload)
        objective_reply = self._reply_text(objective_payload)
        self.assertTrue(objective_reply)
        self.assertIn("current objective", objective_reply.lower())

        warnings_status, warnings_payload = self._post_turn(session_id, "what warnings should i care about?")
        self.assertEqual(warnings_status, 200, warnings_payload)
        warnings_reply = self._reply_text(warnings_payload)
        self.assertTrue(warnings_reply)
        self.assertIn("what warnings should i care about?", warnings_reply.lower())
        self.assertTrue(str(warnings_payload.get("request_id", "")).strip())

        development_status, development_payload = self._post_turn(
            session_id,
            "MIM, the goal/task for you is to leverage the existing mim_wall app on my mobile phone for direct interaction with you. How do we make this happen?",
        )
        self.assertEqual(development_status, 200, development_payload)
        development_reply = self._reply_text(development_payload)
        lowered_development_reply = development_reply.lower()
        self.assertIn("next action: inspect the existing mim_wall app", lowered_development_reply)
        self.assertIn("steps:", lowered_development_reply)
        self.assertIn("current mim session flow", lowered_development_reply)
        self.assertIn("mim_wall", lowered_development_reply)

        session_status, session_payload = get_json(f"/interface/sessions/{session_id}")
        self.assertEqual(session_status, 200, session_payload)
        self.assertIsInstance(session_payload, dict)
        session = session_payload.get("session", {}) if isinstance(session_payload, dict) else {}
        context = session.get("context_json", {}) if isinstance(session.get("context_json", {}), dict) else {}

        self.assertEqual(str(session.get("session_key", "")).strip(), session_id)
        self.assertEqual(str(session.get("channel", "")).strip(), "text")
        self.assertTrue(str(session.get("last_input_at", "")).strip())
        self.assertTrue(str(session.get("last_output_at", "")).strip())
        self.assertEqual(str(context.get("session_id", "")).strip(), session_id)
        self.assertEqual(str(context.get("last_topic", "")).strip(), "development_integration")