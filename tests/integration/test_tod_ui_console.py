import json
import urllib.request
import unittest


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


BASE_URL = DEFAULT_BASE_URL


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def get_text(path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


class TodUiConsoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe_current_source_runtime(
            suite_name="TOD UI Console",
            base_url=BASE_URL,
            require_mim=False,
            require_ui_state=False,
        )

    def test_tod_ui_state_exposes_console_payload(self) -> None:
        status, payload = get_json("/tod/ui/state")
        self.assertEqual(status, 200, payload)
        self.assertIsInstance(payload, dict)

        status_block = payload.get("status", {}) if isinstance(payload.get("status", {}), dict) else {}
        self.assertTrue(str(status_block.get("code", "")).strip(), payload)
        self.assertTrue(str(status_block.get("label", "")).strip(), payload)
        self.assertTrue(str(status_block.get("headline", "")).strip(), payload)
        self.assertTrue(str(status_block.get("summary", "")).strip(), payload)

        quick_facts = payload.get("quick_facts", {}) if isinstance(payload.get("quick_facts", {}), dict) else {}
        self.assertIn("canonical_objective", quick_facts)
        self.assertIn("live_request_objective", quick_facts)
        self.assertIn("listener_state", quick_facts)
        self.assertIn("publish_status", quick_facts)
        self.assertIn("training_state", quick_facts)
        self.assertIn("training_progress", quick_facts)

        self.assertIsInstance(payload.get("objective_alignment", {}), dict)
        self.assertIsInstance(payload.get("bridge_canonical_evidence", {}), dict)
        self.assertIsInstance(payload.get("live_task_request", {}), dict)
        self.assertIsInstance(payload.get("listener_decision", {}), dict)
        self.assertIsInstance(payload.get("publish", {}), dict)
        self.assertIsInstance(payload.get("authority_reset", {}), dict)
        self.assertIsInstance(payload.get("operator_guidance", []), list)
        self.assertIsInstance(payload.get("training_status", {}), dict)
        self.assertIsInstance(payload.get("conversation", {}), dict)

        training_status = payload.get("training_status", {}) if isinstance(payload.get("training_status", {}), dict) else {}
        self.assertIn("available", training_status)
        self.assertIn("summary", training_status)
        self.assertIn("percent_complete", training_status)
        self.assertIn("recent_events", training_status)

        conversation = payload.get("conversation", {}) if isinstance(payload.get("conversation", {}), dict) else {}
        self.assertEqual(conversation.get("mode"), "tod")
        self.assertEqual(conversation.get("state_url"), "/public/chat/state")
        self.assertEqual(conversation.get("message_url"), "/public/chat/message")

    def test_tod_console_html_wires_new_surface(self) -> None:
        status, html = get_text("/tod")
        self.assertEqual(status, 200)
        self.assertIn("TOD Authority, Training, And Dialog Console", html)
        self.assertIn("id=\"todStatusChip\"", html)
        self.assertIn("id=\"trainingSummary\"", html)
        self.assertIn("id=\"chatThread\"", html)
        self.assertIn("Talk To TOD", html)
        self.assertIn("id=\"guidanceList\"", html)
        self.assertIn("fetch('/tod/ui/state'", html)
        self.assertIn("/public/chat/message", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)