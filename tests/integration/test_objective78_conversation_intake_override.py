import json
import os
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)
SHARED_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "shared"


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
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, (dict, list)) else {
            "data": parsed
        }


def read_local_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class Objective78ConversationIntakeOverrideTest(unittest.TestCase):
    def _post_session_turn(
        self, session_id: str, text: str, parsed_intent: str = "discussion"
    ) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": parsed_intent,
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )

    def _post_camera_event(
        self,
        *,
        session_id: str,
        device_suffix: str,
        observations: list[dict],
    ) -> tuple[int, dict]:
        return post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-{device_suffix}-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": f"objective78-{device_suffix}"},
                "observations": observations,
            },
        )

    def _run_workspace_scan(self, scan_area: str, observations: list[dict]) -> int:
        status, capability = post_json(
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
        self.assertEqual(status, 200, capability)

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
        return execution_id

    def _wait_for_ui_request_id(
        self,
        *,
        previous_request_id: str,
        timeout_seconds: float = 10.0,
    ) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            status, payload = get_json("/mim/ui/state")
            self.assertEqual(status, 200, payload)
            self.assertIsInstance(payload, dict)
            collaboration = (
                payload.get("collaboration_progress", {})
                if isinstance(payload.get("collaboration_progress", {}), dict)
                else {}
            )
            request_id = str(collaboration.get("request_id", "")).strip()
            if request_id and request_id != previous_request_id:
                return request_id
            time.sleep(0.1)
        self.fail("Timed out waiting for `/mim/ui/state` to publish a new request id.")

    def _new_headless_firefox_driver(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.firefox.options import Options
            from selenium.webdriver.firefox.service import Service
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        browser_binary = Path("/snap/firefox/current/usr/lib/firefox/firefox")
        geckodriver_binary = Path("/snap/bin/geckodriver")
        if not browser_binary.exists() or not geckodriver_binary.exists():
            self.skipTest("Firefox or geckodriver is unavailable for `/mim` browser validation.")

        options = Options()
        options.add_argument("-headless")
        options.binary_location = str(browser_binary)

        driver = webdriver.Firefox(
            service=Service(str(geckodriver_binary)),
            options=options,
        )
        driver.set_page_load_timeout(25)
        return driver

    def _install_text_chat_result_capture(self, driver) -> None:
        driver.execute_script(
            """
            if (!window.__mimTextChatCaptureInstalled) {
              const originalFetch = window.fetch.bind(window);
              window.__mimLastTextChatResult = null;
              window.__mimTextChatResults = [];
              window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                try {
                  const input = args[0];
                  const init = args[1] || {};
                  const url = typeof input === 'string' ? input : String((input && input.url) || '');
                  if (url.includes('/gateway/intake/text') && String(init.method || 'GET').toUpperCase() === 'POST') {
                    const payload = await response.clone().json();
                    window.__mimLastTextChatResult = payload;
                    window.__mimTextChatResults.push(payload);
                  }
                } catch (error) {
                  window.__mimLastTextChatCaptureError = String((error && error.message) || error || 'capture_failed');
                }
                return response;
              };
              window.__mimTextChatCaptureInstalled = true;
            }
            """
        )

    def _wait_for_captured_text_chat_result(
        self,
        driver,
        *,
        previous_request_id: str,
        timeout_seconds: float = 10.0,
    ) -> dict:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            result = driver.execute_script(
                "return window.__mimLastTextChatResult || null;"
            )
            if isinstance(result, dict):
                request_id = str(result.get("request_id", "")).strip()
                if request_id and request_id != previous_request_id:
                    return result
            time.sleep(0.1)
        self.fail("Timed out waiting for the browser to capture a new `/gateway/intake/text` result.")

    def _expected_recent_changes_reply(
        self,
        *,
        request_id: str,
        message: str,
        result_reason: str,
    ) -> str:
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: dispatch one bounded TOD recent-changes summary request and surface TOD's result. "
            f"Status: done. Result: {result_reason}"
        )

    def _expected_recent_changes_next_step_reply(
        self,
        *,
        request_id: str,
        message: str,
        continuation: dict | None = None,
        recent_changes_result_reason: str = "",
        selection_reason: str = "",
        followup_result_reason: str = "",
    ) -> str:
        chain = continuation
        if not isinstance(chain, dict):
            chain = {
                "steps": [
                    {"step_number": 1, "result_reason": recent_changes_result_reason},
                    {
                        "step_number": 2,
                        "selection_reason": selection_reason,
                        "result_reason": followup_result_reason,
                    },
                ],
                "stop_detail": "I stopped because the only clear bounded next step would repeat a previous action and create a loop.",
            }
        combined_result = self._expected_controlled_continuation_result(
            primary_result_label="Recent-changes summary",
            continuation=chain,
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result. "
            f"Status: done. Result: {combined_result}"
        )

    def _expected_warning_care_reply(
        self,
        *,
        request_id: str,
        message: str,
        continuation: dict | None = None,
        warnings_result_reason: str = "",
        selection_reason: str = "",
        followup_result_reason: str = "",
    ) -> str:
        chain = continuation
        if not isinstance(chain, dict):
            chain = {
                "steps": [
                    {"step_number": 1, "result_reason": warnings_result_reason},
                    {
                        "step_number": 2,
                        "selection_reason": selection_reason,
                        "result_reason": followup_result_reason,
                    },
                ],
                "stop_detail": "I stopped because the only clear bounded next step would repeat a previous action and create a loop.",
            }
        combined_result = self._expected_controlled_continuation_result(
            primary_result_label="Warnings summary",
            continuation=chain,
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result. "
            f"Status: done. Result: {combined_result}"
        )

    def _expected_bridge_warning_next_step_reply(
        self,
        *,
        request_id: str,
        message: str,
        continuation: dict | None = None,
        bridge_warning_result_reason: str = "",
        selection_reason: str = "",
        followup_result_reason: str = "",
    ) -> str:
        chain = continuation
        if not isinstance(chain, dict):
            chain = {
                "steps": [
                    {"step_number": 1, "result_reason": bridge_warning_result_reason},
                    {
                        "step_number": 2,
                        "selection_reason": selection_reason,
                        "result_reason": followup_result_reason,
                    },
                ],
                "stop_detail": "I stopped because there was no clear bounded next step after the current action.",
            }
        combined_result = self._expected_controlled_continuation_result(
            primary_result_label="Bridge-warning explanation",
            continuation=chain,
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result. "
            f"Status: done. Result: {combined_result}"
        )

    def _expected_objective_summary_next_step_reply(
        self,
        *,
        request_id: str,
        message: str,
        continuation: dict,
    ) -> str:
        combined_result = self._expected_controlled_continuation_result(
            primary_result_label="Current-objective summary",
            continuation=continuation,
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result. "
            f"Status: done. Result: {combined_result}"
        )

    def _expected_controlled_continuation_result(
        self,
        *,
        primary_result_label: str,
        continuation: dict,
    ) -> str:
        steps = continuation.get("steps", []) if isinstance(continuation, dict) else []
        self.assertTrue(steps)

        primary_result_reason = str(steps[0].get("result_reason", "")).strip()
        parts = [f"Step 1 result ({primary_result_label}): {primary_result_reason}"]
        for step in steps[1:]:
            step_number = int(step.get("step_number", 0) or 0)
            selection_reason = str(step.get("selection_reason", "")).strip()
            result_reason = str(step.get("result_reason", "")).strip()
            if selection_reason:
                parts.append(f"Step {step_number} selection: {selection_reason}")
            if result_reason:
                parts.append(f"Step {step_number} result: {result_reason}")

        stop_detail = str(continuation.get("stop_detail", "")).strip()
        if stop_detail:
            parts.append(f"Stop: {stop_detail}")
        return " ".join(parts)

    def _expected_instructional_setup_reply(
        self,
        *,
        request_id: str,
        message: str,
    ) -> str:
        result = (
            "Steps:\n\n"
            "1. Ensure MIM is running on 0.0.0.0 instead of 127.0.0.1.\n"
            "2. Find the computer's local IP with hostname -I or ip addr.\n"
            "3. Make sure your phone is on the same local network as the MIM host.\n"
            "4. Open http://<ip>:18001/mim on your phone.\n"
            "5. If it still does not load, allow port 18001 through the firewall and verify MIM is still listening on that port."
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: explain local network access setup for MIM. "
            f"Status: done. Result: {result}"
        )

    def _expected_instructional_autostart_reply(
        self,
        *,
        request_id: str,
        message: str,
    ) -> str:
        result = (
            "Steps:\n\n"
            "1. Create the user service directory with mkdir -p ~/.config/systemd/user.\n"
            "2. Copy /home/testpilot/mim/deploy/systemd-user/mim-desktop-shell.service into ~/.config/systemd/user/.\n"
            "3. Reload user units with systemctl --user daemon-reload.\n"
            "4. Enable and start the service with systemctl --user enable --now mim-desktop-shell.service.\n"
            "5. Verify it is running with systemctl --user status mim-desktop-shell.service."
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: explain user-service setup for MIM desktop shell. "
            f"Status: done. Result: {result}"
        )

    def _expected_instructional_existing_asset_integration_reply(
        self,
        *,
        request_id: str,
        message: str,
    ) -> str:
        result = (
            "Steps:\n\n"
            "1. Treat the existing phone assistant app as a thin client for the current MIM text-chat/backend path instead of building a second assistant stack.\n"
            "2. Reuse the same session model that /mim already uses by keeping one stable conversation_session_id, like the browser-side mim_text_chat_session_id.\n"
            "3. Send phone messages to /gateway/intake/text on the same MIM backend so the app uses the current conversation layer and bounded TOD bridge behavior.\n"
            "4. Render the returned mim_interface.reply_text, or the equivalent understood/next_action/result fields, directly in the phone app UI.\n"
            "5. If you want a more native mobile shell later, keep the same gateway and session contract and only swap the presentation layer around it."
        )
        return (
            f"Request {request_id}. I understood: {message}. "
            "Next action: explain how to reuse the existing phone assistant app as a thin client for MIM. "
            f"Status: done. Result: {result}"
        )

    def test_conversation_intent_routes_to_conversation_layer_without_execution(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "How are you doing today?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )

        self.assertEqual(str(resolution.get("outcome", "")), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")), "store_only")
        self.assertEqual(str(resolution.get("reason", "")), "conversation_override")
        self.assertEqual(
            str(metadata.get("route_preference", "")), "conversation_layer"
        )
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertTrue(str(resolution.get("clarification_prompt", "")).strip())
        self.assertFalse("execution" in payload)

    def test_instructional_setup_question_returns_actionable_steps_in_conversation_layer(
        self,
    ) -> None:
        message = "What do I need to do to access MIM from my phone?"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_setup_instruction")
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "conversation_layer")
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertFalse("execution" in payload)
        self.assertFalse("tod_dispatch" in payload)

        expected_reply = self._expected_instructional_setup_reply(
            request_id=request_id,
            message=message,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "explain local network access setup for MIM",
        )
        self.assertEqual(str(interface.get("result", "")).strip(), str(resolution.get("clarification_prompt", "")).strip())
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

    def test_instructional_setup_question_renders_exact_browser_reply_text(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What do I need to do to access MIM from my phone?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_send.click()

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            expected_reply = self._expected_instructional_setup_reply(
                request_id=request_id,
                message=message,
            )

            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_instructional_autostart_question_returns_actionable_steps_in_conversation_layer(
        self,
    ) -> None:
        message = "How do I set up MIM to start automatically on login?"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_setup_instruction")
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "conversation_layer")
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertFalse("execution" in payload)
        self.assertFalse("tod_dispatch" in payload)

        expected_reply = self._expected_instructional_autostart_reply(
            request_id=request_id,
            message=message,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "explain user-service setup for MIM desktop shell",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

    def test_instructional_autostart_question_renders_exact_browser_reply_text(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "How do I set up MIM to start automatically on login?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_send.click()

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            expected_reply = self._expected_instructional_autostart_reply(
                request_id=request_id,
                message=message,
            )

            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_instructional_existing_asset_integration_question_returns_actionable_steps(self) -> None:
        message = (
            "We already have a MIM phone assistant app started. "
            "How do we leverage what we already created to accomplish a direct tie to MIM from my phone?"
        )
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_setup_instruction")
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "conversation_layer")
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertFalse("execution" in payload)
        self.assertFalse("tod_dispatch" in payload)

        expected_reply = self._expected_instructional_existing_asset_integration_reply(
            request_id=request_id,
            message=message,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "explain how to reuse the existing phone assistant app as a thin client for MIM",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

    def test_instructional_existing_mobile_client_integration_question_returns_actionable_steps(self) -> None:
        message = (
            "We already have a mobile client running on my phone. "
            "How do we connect it to MIM without rebuilding the app?"
        )
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_setup_instruction")
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "conversation_layer")
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertFalse("execution" in payload)
        self.assertFalse("tod_dispatch" in payload)

        expected_reply = self._expected_instructional_existing_asset_integration_reply(
            request_id=request_id,
            message=message,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "explain how to reuse the existing phone assistant app as a thin client for MIM",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

    def test_instructional_existing_asset_integration_question_renders_exact_browser_reply_text(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = (
            "We already have a MIM phone assistant app started. "
            "How do we leverage what we already created to accomplish a direct tie to MIM from my phone?"
        )
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_send.click()

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            expected_reply = self._expected_instructional_existing_asset_integration_reply(
                request_id=request_id,
                message=message,
            )

            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_conversation_with_action_auto_executes_under_initiative_authority(self) -> None:
        session_id = f"objective78-action-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )

        self.assertEqual(str(resolution.get("outcome", "")), "auto_execute")
        self.assertEqual(str(resolution.get("safety_decision", "")), "auto_execute")
        self.assertEqual(
            str(resolution.get("reason", "")), "authorized_initiative_auto_execute"
        )
        self.assertTrue(bool(metadata.get("initiative_auto_execute")))
        self.assertFalse(str(metadata.get("optional_escalation", "")).strip())
        self.assertFalse("execution" in payload)
        self.assertIn("created one bounded goal", str(resolution.get("clarification_prompt", "")).lower())

    def test_explicit_action_request_keeps_goal_routing(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Execute workspace scan now",
                "parsed_intent": "unknown",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )

        self.assertEqual(str(metadata.get("route_preference", "")), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertTrue("execution" in payload)

    def test_repeated_conversation_action_prompt_stays_auto_execute(self) -> None:
        session_id = f"objective78-repeat-{uuid.uuid4()}"
        first_status, first_payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(first_status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertEqual(
            str(first_resolution.get("reason", "")), "authorized_initiative_auto_execute"
        )
        self.assertEqual(str(first_resolution.get("outcome", "")), "auto_execute")

        second_status, second_payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan now?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )

        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "authorized_initiative_auto_execute",
        )
        self.assertEqual(str(second_resolution.get("outcome", "")), "auto_execute")
        self.assertFalse("execution" in second_payload)

    def test_conversation_implementation_request_routes_to_authorized_initiative_without_execution(
        self,
    ) -> None:
        session_id = f"objective78-implementation-{uuid.uuid4()}"
        message = "Implement your plan for improving context retention and disambiguation."
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "discussion",
                "confidence": 0.9,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "route_preference": "conversation_layer",
                },
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        initiative_run = payload.get("initiative_run", {}) if isinstance(payload, dict) else {}

        self.assertEqual(
            str(resolution.get("reason", "")).strip(),
            "authorized_initiative_auto_execute",
        )
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertTrue(bool(metadata.get("initiative_auto_execute")))
        self.assertTrue(isinstance(metadata.get("initiative_run"), dict))
        self.assertTrue(isinstance(initiative_run, dict))
        self.assertNotIn("execution", payload)
        self.assertNotIn("handoff_submission", payload)
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertIn(
            str(interface.get("status", "")).strip(),
            {"doing", "done"},
        )
        self.assertIn(
            "initiative",
            str(interface.get("next_action", "")).lower(),
        )
        self.assertIn(request_id, str(interface.get("reply_text", "")))

    def test_greeting_turn_does_not_trigger_precision_prompt(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "hi MIM",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertEqual(str(resolution.get("reason", "")), "conversation_override")
        self.assertIn("ready to help", prompt)
        self.assertNotIn("one specific request", prompt)

    def test_identity_question_returns_direct_identity_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "do you know who you are?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip()

        self.assertIn("mim", prompt.lower())
        self.assertTrue(
            ("i am" in prompt.lower()) or ("i'm" in prompt.lower()),
            prompt,
        )

    def test_session_identity_statement_is_acknowledged_without_execution(self) -> None:
        session_id = f"objective78-identity-{uuid.uuid4()}"
        status, payload = self._post_session_turn(session_id, "I'm David")
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(
            str(resolution.get("reason", "")).strip(),
            "conversation_session_identity_capture",
        )
        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("clarification_prompt", "")).strip(), "Got it, David.")
        self.assertEqual(str(metadata.get("session_display_name", "")).strip(), "David")
        self.assertTrue(bool(metadata.get("skip_conversation_memory")))
        self.assertEqual(str(interface.get("reply_text", "")).strip(), "Got it, David.")
        self.assertFalse("execution" in payload)

    def test_session_identity_is_reused_in_followup_conversation_reply(self) -> None:
        session_id = f"objective78-identity-followup-{uuid.uuid4()}"

        capture_status, capture_payload = self._post_session_turn(session_id, "I'm David")
        self.assertEqual(capture_status, 200, capture_payload)

        status, payload = self._post_session_turn(session_id, "hello")
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_override")
        prompt = str(resolution.get("clarification_prompt", "")).strip()
        reply_text = str(interface.get("reply_text", "")).strip()
        self.assertIn("mim", prompt.lower())
        self.assertTrue(
            ("hi" in prompt.lower()) or ("hello" in prompt.lower()),
            prompt,
        )
        self.assertIn("david", reply_text.lower())
        self.assertIn("mim", reply_text.lower())
        self.assertFalse("execution" in payload)

    def test_session_identity_call_me_statement_is_acknowledged_without_execution(self) -> None:
        session_id = f"objective78-identity-callme-{uuid.uuid4()}"
        status, payload = self._post_session_turn(session_id, "call me David")
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(
            str(resolution.get("reason", "")).strip(),
            "conversation_session_identity_capture",
        )
        self.assertEqual(str(resolution.get("outcome", "")).strip(), "store_only")
        self.assertEqual(str(resolution.get("clarification_prompt", "")).strip(), "Got it, David.")
        self.assertEqual(str(metadata.get("session_display_name", "")).strip(), "David")
        self.assertTrue(bool(metadata.get("skip_conversation_memory")))
        self.assertEqual(str(interface.get("reply_text", "")).strip(), "Got it, David.")
        self.assertFalse("execution" in payload)

    def test_session_identity_my_name_is_is_reused_in_followup_conversation_reply(self) -> None:
        session_id = f"objective78-identity-myname-{uuid.uuid4()}"

        capture_status, capture_payload = self._post_session_turn(session_id, "my name is David")
        self.assertEqual(capture_status, 200, capture_payload)

        status, payload = self._post_session_turn(session_id, "hello")
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_override")
        prompt = str(resolution.get("clarification_prompt", "")).strip()
        reply_text = str(interface.get("reply_text", "")).strip()
        self.assertIn("mim", prompt.lower())
        self.assertTrue(
            ("hi" in prompt.lower()) or ("hello" in prompt.lower()),
            prompt,
        )
        self.assertIn("david", reply_text.lower())
        self.assertIn("mim", reply_text.lower())
        self.assertFalse("execution" in payload)

    def test_remembered_identity_is_reused_across_conversation_sessions(self) -> None:
        remembered_session_id = f"objective78-identity-cross-session-{uuid.uuid4()}"
        followup_session_id = f"objective78-identity-cross-session-followup-{uuid.uuid4()}"

        capture_status, capture_payload = self._post_session_turn(
            remembered_session_id,
            "call me David",
        )
        self.assertEqual(capture_status, 200, capture_payload)

        status, payload = self._post_session_turn(followup_session_id, "hello")
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_override")
        self.assertEqual(str(metadata.get("session_display_name", "")).strip(), "David")
        self.assertIn("david", str(interface.get("reply_text", "")).strip().lower())
        self.assertFalse("execution" in payload)

    def _assert_name_mention_does_not_capture_identity(self, text: str) -> None:
        session_id = f"objective78-name-mention-{uuid.uuid4()}"

        status, payload = self._post_session_turn(session_id, text)
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(resolution.get("reason", "")).strip(), "conversation_override")
        self.assertNotEqual(
            str(resolution.get("reason", "")).strip(),
            "conversation_session_identity_capture",
        )
        self.assertFalse(str(metadata.get("session_display_name", "")).strip())
        self.assertFalse(bool(metadata.get("skip_conversation_memory")))
        self.assertNotEqual(str(interface.get("reply_text", "")).strip(), "Got it, David.")
        self.assertFalse("execution" in payload)

        follow_status, follow_payload = self._post_session_turn(session_id, "hello")
        self.assertEqual(follow_status, 200, follow_payload)

        follow_resolution = (
            follow_payload.get("resolution", {}) if isinstance(follow_payload, dict) else {}
        )
        follow_interface = (
            follow_payload.get("mim_interface", {}) if isinstance(follow_payload, dict) else {}
        )

        self.assertEqual(str(follow_resolution.get("reason", "")).strip(), "conversation_override")
        prompt = str(follow_resolution.get("clarification_prompt", "")).strip()
        reply_text = str(follow_interface.get("reply_text", "")).strip()
        self.assertIn("mim", prompt.lower())
        self.assertTrue(
            ("hi" in prompt.lower()) or ("hello" in prompt.lower()),
            prompt,
        )
        self.assertIn(str(follow_interface.get("request_id", "")).strip(), reply_text)
        self.assertIn("reply directly", reply_text.lower())
        self.assertIn("mim", reply_text.lower())

    def test_name_mention_do_you_know_david_does_not_capture_identity(self) -> None:
        self._assert_name_mention_does_not_capture_identity("Do you know David?")

    def test_name_mention_my_friend_david_was_here_does_not_capture_identity(self) -> None:
        self._assert_name_mention_does_not_capture_identity("My friend David was here")

    def test_name_mention_tell_david_hello_does_not_capture_identity(self) -> None:
        self._assert_name_mention_does_not_capture_identity("Tell David hello")

    def test_name_mention_who_is_david_does_not_capture_identity(self) -> None:
        self._assert_name_mention_does_not_capture_identity("Who is David?")

    def test_name_mention_i_talked_to_david_today_does_not_capture_identity(self) -> None:
        self._assert_name_mention_does_not_capture_identity("I talked to David today")

    def test_bounded_tod_status_request_is_unchanged_after_call_me_identity_capture(self) -> None:
        session_id = f"objective78-identity-callme-tod-{uuid.uuid4()}"

        capture_status, capture_payload = self._post_session_turn(session_id, "call me David")
        self.assertEqual(capture_status, 200, capture_payload)

        status, payload = self._post_session_turn(
            session_id,
            "Check TOD status and report it back",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

    def test_bounded_tod_status_request_is_unchanged_after_session_identity_capture(self) -> None:
        session_id = f"objective78-identity-tod-{uuid.uuid4()}"

        capture_status, capture_payload = self._post_session_turn(session_id, "I'm David")
        self.assertEqual(capture_status, 200, capture_payload)

        status, payload = self._post_session_turn(
            session_id,
            "Check TOD status and report it back",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

    def test_text_turn_returns_explicit_mim_interface_reply_contract(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "do you know who you are?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertTrue(str(payload.get("request_id", "")).strip())
        self.assertEqual(
            str(interface.get("request_id", "")).strip(),
            str(payload.get("request_id", "")).strip(),
        )
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn("who you are", str(interface.get("understood", "")).lower())
        self.assertIn("reply directly", str(interface.get("next_action", "")).lower())
        result_text = str(interface.get("result", "")).lower()
        self.assertIn("mim", result_text)
        self.assertTrue(("i am" in result_text) or ("i'm" in result_text), result_text)
        self.assertFalse(str(interface.get("blocker", "")).strip())
        self.assertIn(
            str(interface.get("request_id", "")).strip(),
            str(interface.get("reply_text", "")),
        )

    def test_conversation_action_turn_returns_active_interface_reply_contract(self) -> None:
        session_id = f"objective78-interface-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertNotIn(
            "explicit confirmation",
            str(interface.get("next_action", "")).lower(),
        )
        self.assertIn("bounded goal", str(interface.get("result", "")).lower())
        self.assertFalse(str(interface.get("blocker", "")).strip())

    def test_start_training_routes_to_authorized_initiative_without_confirmation(self) -> None:
        session_id = f"objective78-training-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "start training",
                "parsed_intent": "discussion",
                "confidence": 0.95,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        initiative_run = payload.get("initiative_run", {}) if isinstance(payload, dict) else {}
        initiative_status = payload.get("initiative_status", {}) if isinstance(payload, dict) else {}

        self.assertEqual(
            str(resolution.get("reason", "")).strip(),
            "authorized_initiative_auto_execute",
        )
        self.assertEqual(str(resolution.get("outcome", "")).strip(), "auto_execute")
        self.assertTrue(bool(metadata.get("initiative_auto_execute")))
        self.assertTrue(isinstance(metadata.get("initiative_run"), dict))
        self.assertTrue(isinstance(initiative_run, dict))
        self.assertTrue(isinstance(initiative_status, dict))
        self.assertFalse("handoff_submission" in payload)
        self.assertFalse("execution" in payload)
        self.assertIn(str(interface.get("status", "")).strip(), {"doing", "done"})
        self.assertIn("initiative", str(interface.get("next_action", "")).lower())
        self.assertNotIn(
            "waiting for explicit confirmation",
            str(interface.get("reply_text", "")).lower(),
        )

    def test_blocked_text_turn_with_conversation_hint_stays_in_goal_system(self) -> None:
        session_id = f"objective78-blocked-browser-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Delete the database now",
                "parsed_intent": "discussion",
                "confidence": 0.9,
                "safety_flags": ["blocked"],
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "route_preference": "conversation_layer",
                },
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(resolution.get("outcome", "")).strip(), "blocked")
        self.assertEqual(str(interface.get("status", "")).strip(), "blocked")
        self.assertIn("blocked", str(interface.get("reply_text", "")).lower())
        self.assertTrue(str(interface.get("blocker", "")).strip())

    def test_bounded_tod_status_request_preserves_request_id_across_bridge(self) -> None:
        session_id = f"objective78-tod-status-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Check TOD status and report it back",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertIn("bounded status", str(result_artifact.get("result_reason", "")).lower())

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_status_request_preserves_browser_session_continuity_with_enter_submit(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Check TOD status and report it back"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            session_id_before_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_submit)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_input.send_keys(Keys.ENTER)

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(
                previous_request_id=previous_request_id,
            )
            self.assertEqual(ui_request_id, request_id)

            request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            session_id_after_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_submit, session_id_before_submit)
            self.assertEqual(str(request_artifact.get("session_key", "")).strip(), session_id_before_submit)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            dispatch = (
                metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(dispatch.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch.get("dispatch_kind", "")).strip())

            result_reason = str(result_artifact.get("result_reason", "")).strip()
            self.assertTrue(result_reason)
            expected_reply = (
                f"Request {request_id}. I understood: {message}. "
                "Next action: dispatch one bounded TOD status request and surface TOD's result. "
                f"Status: done. Result: {result_reason}"
            )
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_status_request_preserves_browser_session_continuity_across_repeated_turns(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Check TOD status and report it back"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD status request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_1 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_1)

            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            request_id_1 = str(payload1.get("request_id", "")).strip()
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch1.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                initial_bubble_count + 4,
            )
            session_id_2 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_2, session_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()

            self.assertNotEqual(request_id_2, request_id_1)
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch2.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_1)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), initial_bubble_count + 4)
            self.assertEqual(str(bubbles[-4].text).strip(), message)
            self.assertEqual(str(bubbles[-3].text).strip(), expected_reply1)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_status_request_preserves_browser_session_continuity_after_reload(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Check TOD status and report it back"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD status request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, _ = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_reload)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch1.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_reload)

            driver.refresh()
            self._install_text_chat_result_capture(driver)
            wait.until(EC.presence_of_element_located((By.ID, "chatInput")))

            session_id_after_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_reload, session_id_before_reload)

            reloaded_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(reloaded_bubbles), 1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(reloaded_bubbles) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch2.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_reload)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(reloaded_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_status_request_preserves_browser_session_continuity_after_clear(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Check TOD status and report it back"
        clear_reply = "Text chat cleared. Ready for your next message."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD status request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_clear)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch1.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_clear)

            clear_btn = wait.until(EC.element_to_be_clickable((By.ID, "chatClearBtn")))
            clear_btn.click()
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == clear_reply
            )

            bubbles_after_clear = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertEqual(len(bubbles_after_clear), 1)
            self.assertEqual(str(bubbles_after_clear[-1].text).strip(), clear_reply)

            session_id_after_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_clear, session_id_before_clear)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(bubbles_after_clear) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("action_name", "")).strip(), "tod_status_check")
            self.assertFalse(str(dispatch2.get("dispatch_kind", "")).strip())
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_clear)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), 3)
            self.assertEqual(str(bubbles[-3].text).strip(), clear_reply)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
            self.assertNotEqual(expected_reply2, expected_reply1)
        finally:
            driver.quit()

    def test_bounded_tod_objective_summary_request_preserves_request_id_across_bridge(self) -> None:
        message = "Summarize the current objective for TOD"
        session_id = f"objective78-tod-objective-summary-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        primary_dispatch = (
            metadata.get("tod_primary_dispatch", {})
            if isinstance(metadata.get("tod_primary_dispatch"), dict)
            else {}
        )
        selected_next_step = (
            metadata.get("tod_selected_next_step", {})
            if isinstance(metadata.get("tod_selected_next_step"), dict)
            else {}
        )
        controlled_continuation = (
            metadata.get("tod_controlled_continuation", {})
            if isinstance(metadata.get("tod_controlled_continuation"), dict)
            else {}
        )
        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(primary_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(primary_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_objective_summary_request",
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        self.assertEqual(
            str(selected_next_step.get("selected_dispatch_kind", "")).strip(),
            "bounded_recent_changes_request",
        )
        self.assertEqual(int(controlled_continuation.get("step_count", 0) or 0), 3)
        self.assertEqual(
            str(controlled_continuation.get("stop_reason", "")).strip(),
            "max_depth_reached",
        )
        selection_reason = str(selected_next_step.get("selection_reason", "")).strip()
        self.assertTrue(selection_reason)
        self.assertIn("materially moving that objective", selection_reason.lower())

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
        )
        primary_result_reason = str(primary_dispatch.get("result_reason", "")).strip()
        self.assertIn("current objective", primary_result_reason.lower())
        followup_result_reason = str(result_artifact.get("result_reason", "")).strip()
        self.assertEqual(
            followup_result_reason,
            str(dispatch.get("result_reason", "")).strip(),
        )
        expected_reply = self._expected_objective_summary_next_step_reply(
            request_id=request_id,
            message=message,
            continuation=controlled_continuation,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)
        self.assertIn(primary_result_reason, str(interface.get("result", "")))
        self.assertIn(followup_result_reason, str(interface.get("result", "")))

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_objective_summary_request_preserves_browser_session_continuity_with_enter_submit(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize the current objective for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            session_id_before_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_submit)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_input.send_keys(Keys.ENTER)

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(
                previous_request_id=previous_request_id,
            )
            self.assertEqual(ui_request_id, request_id)

            request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            session_id_after_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_submit, session_id_before_submit)
            self.assertEqual(str(request_artifact.get("session_key", "")).strip(), session_id_before_submit)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            dispatch = (
                metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
            )
            primary_dispatch = (
                metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
            )
            controlled_continuation = (
                metadata.get("tod_controlled_continuation", {}) if isinstance(metadata.get("tod_controlled_continuation"), dict) else {}
            )
            self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")

            expected_reply = self._expected_objective_summary_next_step_reply(
                request_id=request_id,
                message=message,
                continuation=controlled_continuation,
            )
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_objective_summary_request_preserves_browser_session_continuity_across_repeated_turns(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize the current objective for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                controlled_continuation = (
                    metadata.get("tod_controlled_continuation", {}) if isinstance(metadata.get("tod_controlled_continuation"), dict) else {}
                )
                expected_reply = self._expected_objective_summary_next_step_reply(
                    request_id=request_id,
                    message=message,
                    continuation=controlled_continuation,
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_1 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_1)

            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            request_id_1 = str(payload1.get("request_id", "")).strip()
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                initial_bubble_count + 4,
            )
            session_id_2 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_2, session_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()

            self.assertNotEqual(request_id_2, request_id_1)
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_1)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), initial_bubble_count + 4)
            self.assertEqual(str(bubbles[-4].text).strip(), message)
            self.assertEqual(str(bubbles[-3].text).strip(), expected_reply1)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_objective_summary_request_preserves_browser_session_continuity_after_reload(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize the current objective for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                controlled_continuation = (
                    metadata.get("tod_controlled_continuation", {}) if isinstance(metadata.get("tod_controlled_continuation"), dict) else {}
                )
                expected_reply = self._expected_objective_summary_next_step_reply(
                    request_id=request_id,
                    message=message,
                    continuation=controlled_continuation,
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, _ = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_reload)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_reload)

            driver.refresh()
            self._install_text_chat_result_capture(driver)
            wait.until(EC.presence_of_element_located((By.ID, "chatInput")))

            session_id_after_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_reload, session_id_before_reload)

            reloaded_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(reloaded_bubbles), 1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(reloaded_bubbles) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_reload)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(reloaded_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_objective_summary_request_preserves_browser_session_continuity_after_clear(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize the current objective for TOD"
        clear_reply = "Text chat cleared. Ready for your next message."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                controlled_continuation = (
                    metadata.get("tod_controlled_continuation", {}) if isinstance(metadata.get("tod_controlled_continuation"), dict) else {}
                )
                expected_reply = self._expected_objective_summary_next_step_reply(
                    request_id=request_id,
                    message=message,
                    continuation=controlled_continuation,
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_clear)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_clear)

            clear_btn = wait.until(EC.element_to_be_clickable((By.ID, "chatClearBtn")))
            clear_btn.click()
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == clear_reply
            )

            bubbles_after_clear = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertEqual(len(bubbles_after_clear), 1)
            self.assertEqual(str(bubbles_after_clear[-1].text).strip(), clear_reply)

            session_id_after_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_clear, session_id_before_clear)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(bubbles_after_clear) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_clear)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), 3)
            self.assertEqual(str(bubbles[-3].text).strip(), clear_reply)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
            self.assertNotEqual(expected_reply2, expected_reply1)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_request_preserves_request_id_across_bridge(self) -> None:
        session_id = f"objective78-tod-bridge-warning-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Explain bridge warning for TOD",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}
        primary_dispatch = (
            metadata.get("tod_primary_dispatch", {})
            if isinstance(metadata.get("tod_primary_dispatch"), dict)
            else {}
        )
        selected_next_step = (
            metadata.get("tod_selected_next_step", {})
            if isinstance(metadata.get("tod_selected_next_step"), dict)
            else {}
        )
        controlled_continuation = (
            metadata.get("tod_controlled_continuation", {})
            if isinstance(metadata.get("tod_controlled_continuation"), dict)
            else {}
        )

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(resolution.get("reason", "")).strip(), "tod_bridge_warning_next_step_dispatch")
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_bridge_warning_recommendation_request",
        )
        self.assertEqual(
            str(primary_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_bridge_warning_request",
        )
        self.assertEqual(
            str(selected_next_step.get("selected_dispatch_kind", "")).strip(),
            "bounded_bridge_warning_recommendation_request",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            "bounded_bridge_warning_recommendation_request",
        )
        primary_result_reason = str(primary_dispatch.get("result_reason", "")).strip()
        result_reason = str(result_artifact.get("result_reason", "")).lower()
        self.assertIn("bridge", primary_result_reason.lower())
        self.assertTrue(
            "publication_surface_divergence" in primary_result_reason.lower()
            or "publisher_objective_mismatch" in primary_result_reason.lower()
            or "stale" in primary_result_reason.lower()
        )
        selection_reason = str(selected_next_step.get("selection_reason", "")).strip()
        self.assertIn("concrete next step", selection_reason.lower())
        self.assertEqual(int(controlled_continuation.get("step_count", 0) or 0), 2)
        self.assertEqual(
            str(controlled_continuation.get("stop_reason", "")).strip(),
            "unclear_next_step",
        )

        expected_reply = self._expected_bridge_warning_next_step_reply(
            request_id=request_id,
            message="Explain bridge warning for TOD",
            continuation=controlled_continuation,
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_bridge_warning_request_preserves_browser_session_continuity_with_enter_submit(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Explain bridge warning for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            session_id_before_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_submit)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_input.send_keys(Keys.ENTER)

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(
                previous_request_id=previous_request_id,
            )
            self.assertEqual(ui_request_id, request_id)

            request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            session_id_after_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_submit, session_id_before_submit)
            self.assertEqual(str(request_artifact.get("session_key", "")).strip(), session_id_before_submit)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            dispatch = (
                metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
            )
            primary_dispatch = (
                metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
            )
            selected_next_step = (
                metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
            )
            self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")

            expected_reply = self._expected_bridge_warning_next_step_reply(
                request_id=request_id,
                message=message,
                bridge_warning_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
            )
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_request_preserves_browser_session_continuity_across_repeated_turns(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Explain bridge warning for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_bridge_warning_next_step_reply(
                    request_id=request_id,
                    message=message,
                    bridge_warning_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_1 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_1)

            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            request_id_1 = str(payload1.get("request_id", "")).strip()
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                initial_bubble_count + 4,
            )
            session_id_2 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_2, session_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()

            self.assertNotEqual(request_id_2, request_id_1)
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_1)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), initial_bubble_count + 4)
            self.assertEqual(str(bubbles[-4].text).strip(), message)
            self.assertEqual(str(bubbles[-3].text).strip(), expected_reply1)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_request_preserves_browser_session_continuity_after_reload(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Explain bridge warning for TOD"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_bridge_warning_next_step_reply(
                    request_id=request_id,
                    message=message,
                    bridge_warning_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, _ = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_reload)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_reload)

            driver.refresh()
            self._install_text_chat_result_capture(driver)
            wait.until(EC.presence_of_element_located((By.ID, "chatInput")))

            session_id_after_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_reload, session_id_before_reload)

            reloaded_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(reloaded_bubbles), 1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(reloaded_bubbles) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_reload)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(reloaded_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_request_preserves_browser_session_continuity_after_clear(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Explain bridge warning for TOD"
        clear_reply = "Text chat cleared. Ready for your next message."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_bridge_warning_next_step_reply(
                    request_id=request_id,
                    message=message,
                    bridge_warning_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_clear)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_clear)

            clear_btn = wait.until(EC.element_to_be_clickable((By.ID, "chatClearBtn")))
            clear_btn.click()
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == clear_reply
            )

            bubbles_after_clear = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertEqual(len(bubbles_after_clear), 1)
            self.assertEqual(str(bubbles_after_clear[-1].text).strip(), clear_reply)

            session_id_after_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_clear, session_id_before_clear)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(bubbles_after_clear) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_bridge_warning_recommendation_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_clear)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), 3)
            self.assertEqual(str(bubbles[-3].text).strip(), clear_reply)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
            self.assertNotEqual(expected_reply2, expected_reply1)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_request_id_across_bridge(self) -> None:
        message = "Summarize recent changes that materially affect the current objective."
        session_id = f"objective78-tod-recent-changes-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        primary_dispatch = (
            metadata.get("tod_primary_dispatch", {})
            if isinstance(metadata.get("tod_primary_dispatch"), dict)
            else {}
        )
        selected_next_step = (
            metadata.get("tod_selected_next_step", {})
            if isinstance(metadata.get("tod_selected_next_step"), dict)
            else {}
        )
        controlled_continuation = (
            metadata.get("tod_controlled_continuation", {})
            if isinstance(metadata.get("tod_controlled_continuation"), dict)
            else {}
        )
        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(primary_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(primary_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_recent_changes_request",
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        self.assertEqual(
            str(selected_next_step.get("selected_dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        selection_reason = str(selected_next_step.get("selection_reason", "")).strip()
        self.assertTrue(selection_reason)
        self.assertIn("matter operationally", selection_reason.lower())
        self.assertEqual(int(controlled_continuation.get("step_count", 0) or 0), 2)
        self.assertEqual(
            str(controlled_continuation.get("stop_reason", "")).strip(),
            "unclear_next_step",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
        )
        recent_changes_snapshot = (
            primary_dispatch.get("tod_recent_changes_snapshot", {})
            if isinstance(primary_dispatch.get("tod_recent_changes_snapshot"), dict)
            else {}
        )
        self.assertTrue(recent_changes_snapshot)
        self.assertTrue(str(recent_changes_snapshot.get("summary", "")).strip())
        warnings_snapshot = (
            dispatch.get("tod_warnings_summary_snapshot", {})
            if isinstance(dispatch.get("tod_warnings_summary_snapshot"), dict)
            else {}
        )
        self.assertTrue(warnings_snapshot)
        self.assertTrue(str(warnings_snapshot.get("summary", "")).strip())
        recent_changes_result_reason = str(primary_dispatch.get("result_reason", "")).strip()
        followup_result_reason = str(result_artifact.get("result_reason", "")).strip()
        self.assertEqual(
            followup_result_reason,
            str(dispatch.get("result_reason", "")).strip(),
        )
        expected_reply = self._expected_recent_changes_next_step_reply(
            request_id=request_id,
            message=message,
            continuation=controlled_continuation,
        )
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)
        self.assertIn(recent_changes_result_reason, str(interface.get("result", "")))
        self.assertIn(followup_result_reason, str(interface.get("result", "")))

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_warnings_summary_request_preserves_request_id_across_bridge(self) -> None:
        session_id = f"objective78-tod-warnings-summary-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Summarize current warnings for TOD.",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        warnings_snapshot = (
            result_artifact.get("tod_warnings_summary_snapshot", {})
            if isinstance(result_artifact.get("tod_warnings_summary_snapshot"), dict)
            else {}
        )
        self.assertTrue(warnings_snapshot)
        self.assertTrue(str(warnings_snapshot.get("summary", "")).strip())

        result_reason = str(result_artifact.get("result_reason", "")).lower()
        self.assertTrue("warning" in result_reason or "blocker" in result_reason or "alert" in result_reason)

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_warning_care_request_selects_single_recent_changes_followup_with_request_id_continuity(self) -> None:
        session_id = f"objective78-warning-care-{uuid.uuid4()}"
        message = "What warnings should I care about?"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}
        primary_dispatch = (
            metadata.get("tod_primary_dispatch", {})
            if isinstance(metadata.get("tod_primary_dispatch"), dict)
            else {}
        )
        selected_next_step = (
            metadata.get("tod_selected_next_step", {})
            if isinstance(metadata.get("tod_selected_next_step"), dict)
            else {}
        )
        controlled_continuation = (
            metadata.get("tod_controlled_continuation", {})
            if isinstance(metadata.get("tod_controlled_continuation"), dict)
            else {}
        )

        self.assertEqual(str(resolution.get("reason", "")).strip(), "tod_warning_care_next_step_dispatch")
        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
        self.assertEqual(
            str(primary_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        self.assertEqual(
            str(selected_next_step.get("selected_dispatch_kind", "")).strip(),
            "bounded_recent_changes_request",
        )
        selection_reason = str(selected_next_step.get("selection_reason", "")).strip()
        self.assertTrue(selection_reason)
        self.assertIn("actively affecting those warnings", selection_reason.lower())
        self.assertEqual(int(controlled_continuation.get("step_count", 0) or 0), 2)
        self.assertEqual(
            str(controlled_continuation.get("stop_reason", "")).strip(),
            "unclear_next_step",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            str(dispatch.get("dispatch_kind", "")).strip(),
        )

        warnings_result_reason = str(primary_dispatch.get("result_reason", "")).strip()
        followup_result_reason = str(result_artifact.get("result_reason", "")).strip()
        self.assertEqual(
            followup_result_reason,
            str(dispatch.get("result_reason", "")).strip(),
        )
        expected_reply = self._expected_warning_care_reply(
            request_id=request_id,
            message=message,
            continuation=controlled_continuation,
        )

        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertEqual(
            str(interface.get("next_action", "")).strip(),
            "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result",
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)
        self.assertIn(warnings_result_reason, str(interface.get("result", "")))
        self.assertIn(followup_result_reason, str(interface.get("result", "")))

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)

    def test_bounded_warning_care_request_renders_exact_browser_reply_text(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What warnings should I care about?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_send.click()

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(previous_request_id=previous_request_id)
            self.assertEqual(ui_request_id, request_id)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            primary_dispatch = (
                metadata.get("tod_primary_dispatch", {})
                if isinstance(metadata.get("tod_primary_dispatch"), dict)
                else {}
            )
            selected_next_step = (
                metadata.get("tod_selected_next_step", {})
                if isinstance(metadata.get("tod_selected_next_step"), dict)
                else {}
            )
            controlled_continuation = (
                metadata.get("tod_controlled_continuation", {})
                if isinstance(metadata.get("tod_controlled_continuation"), dict)
                else {}
            )
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            dispatch = (
                metadata.get("tod_dispatch", {})
                if isinstance(metadata.get("tod_dispatch"), dict)
                else {}
            )

            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(
                str(result_artifact.get("dispatch_kind", "")).strip(),
                str(dispatch.get("dispatch_kind", "")).strip(),
            )
            self.assertEqual(
                str(result_artifact.get("result_reason", "")).strip(),
                str(dispatch.get("result_reason", "")).strip(),
            )

            expected_reply = self._expected_warning_care_reply(
                request_id=request_id,
                message=message,
                continuation=controlled_continuation,
            )

            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_request_id_with_mim_text_chat_metadata_shape(self) -> None:
        session_id = f"objective78-tod-recent-changes-ui-shape-{uuid.uuid4()}"
        message = "Summarize recent changes that materially affect the current objective."
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": message,
                "parsed_intent": "discussion",
                "safety_flags": [],
                "metadata_json": {
                    "source": "mim_ui_text_chat",
                    "conversation_session_id": session_id,
                    "route_preference": "conversation_layer",
                },
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        primary_dispatch = (
            metadata.get("tod_primary_dispatch", {})
            if isinstance(metadata.get("tod_primary_dispatch"), dict)
            else {}
        )
        selected_next_step = (
            metadata.get("tod_selected_next_step", {})
            if isinstance(metadata.get("tod_selected_next_step"), dict)
            else {}
        )
        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        self.assertEqual(
            str(primary_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_recent_changes_request",
        )
        self.assertEqual(
            str(selected_next_step.get("selected_dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            "bounded_warnings_summary_request",
        )
        expected_reply = self._expected_recent_changes_next_step_reply(
            request_id=request_id,
            message=message,
            recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
            selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
            followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
        )
        self.assertEqual(str(interface.get("reply_text", "")).strip(), expected_reply)

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_recent_changes_request_renders_exact_browser_reply_text(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize recent changes that materially affect the current objective."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_send.click()

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            request_id = self._wait_for_ui_request_id(previous_request_id=previous_request_id)
            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            primary_dispatch = (
                metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
            )
            selected_next_step = (
                metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
            )
            expected_reply = self._expected_recent_changes_next_step_reply(
                request_id=request_id,
                message=message,
                recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
            )

            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )
            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_browser_session_continuity_across_repeated_turns(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize recent changes that materially affect the current objective."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_recent_changes_next_step_reply(
                    request_id=request_id,
                    message=message,
                    recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_1 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_1)

            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            primary_dispatch1 = (
                metadata1.get("tod_primary_dispatch", {}) if isinstance(metadata1.get("tod_primary_dispatch"), dict) else {}
            )
            request_id_1 = str(payload1.get("request_id", "")).strip()
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(primary_dispatch1.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                initial_bubble_count + 4,
            )
            session_id_2 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_2, session_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            primary_dispatch2 = (
                metadata2.get("tod_primary_dispatch", {}) if isinstance(metadata2.get("tod_primary_dispatch"), dict) else {}
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()

            self.assertNotEqual(request_id_2, request_id_1)
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(primary_dispatch2.get("dispatch_kind", "")).strip(), "bounded_recent_changes_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_1)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), initial_bubble_count + 4)
            self.assertEqual(str(bubbles[-4].text).strip(), message)
            self.assertEqual(str(bubbles[-3].text).strip(), expected_reply1)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_browser_session_continuity_after_reload(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize recent changes that materially affect the current objective."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_recent_changes_next_step_reply(
                    request_id=request_id,
                    message=message,
                    recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, _ = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_reload)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_reload)

            driver.refresh()
            self._install_text_chat_result_capture(driver)
            wait.until(EC.presence_of_element_located((By.ID, "chatInput")))

            session_id_after_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_reload, session_id_before_reload)

            reloaded_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(reloaded_bubbles), 1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(reloaded_bubbles) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_reload)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(reloaded_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_browser_session_continuity_after_clear(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize recent changes that materially affect the current objective."
        clear_reply = "Text chat cleared. Ready for your next message."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
                metadata = (
                    resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
                )
                primary_dispatch = (
                    metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
                )
                selected_next_step = (
                    metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
                )
                expected_reply = self._expected_recent_changes_next_step_reply(
                    request_id=request_id,
                    message=message,
                    recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                    selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                    followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_clear)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch1.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_clear)

            clear_button = wait.until(EC.element_to_be_clickable((By.ID, "chatClearBtn")))
            clear_button.click()
            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")) == 1
            )

            bubbles_after_clear = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertEqual(len(bubbles_after_clear), 1)
            self.assertEqual(str(bubbles_after_clear[0].text).strip(), clear_reply)
            rendered_after_clear = [str(b.text).strip() for b in bubbles_after_clear]
            self.assertNotIn(message, rendered_after_clear)
            self.assertNotIn(expected_reply1, rendered_after_clear)

            session_id_after_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_clear, session_id_before_clear)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(bubbles_after_clear) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch2.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_clear)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), 3)
            self.assertEqual(str(bubbles[-3].text).strip(), clear_reply)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_recent_changes_request_preserves_browser_session_continuity_with_enter_submit(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "Summarize recent changes that materially affect the current objective."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            session_id_before_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_submit)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_input.send_keys(Keys.ENTER)

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(
                previous_request_id=previous_request_id,
            )
            self.assertEqual(ui_request_id, request_id)

            request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            session_id_after_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_submit, session_id_before_submit)
            self.assertEqual(str(request_artifact.get("session_key", "")).strip(), session_id_before_submit)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            dispatch = (
                metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
            )
            primary_dispatch = (
                metadata.get("tod_primary_dispatch", {}) if isinstance(metadata.get("tod_primary_dispatch"), dict) else {}
            )
            selected_next_step = (
                metadata.get("tod_selected_next_step", {}) if isinstance(metadata.get("tod_selected_next_step"), dict) else {}
            )
            self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(str(dispatch.get("dispatch_kind", "")).strip(), "bounded_warnings_summary_request")

            expected_reply = self._expected_recent_changes_next_step_reply(
                request_id=request_id,
                message=message,
                recent_changes_result_reason=str(primary_dispatch.get("result_reason", "")).strip(),
                selection_reason=str(selected_next_step.get("selection_reason", "")).strip(),
                followup_result_reason=str(result_artifact.get("result_reason", "")).strip(),
            )
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_recommendation_request_preserves_request_id_across_bridge(self) -> None:
        session_id = f"objective78-tod-bridge-warning-next-step-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "What should TOD do next about the bridge warning?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        request_id = str(payload.get("request_id", "")).strip()
        self.assertTrue(request_id)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        interface = payload.get("mim_interface", {}) if isinstance(payload, dict) else {}
        dispatch = payload.get("tod_dispatch", {}) if isinstance(payload, dict) else {}

        self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertEqual(str(interface.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(interface.get("status", "")).strip(), "done")
        self.assertIn(request_id, str(interface.get("reply_text", "")))
        self.assertEqual(str(dispatch.get("request_id", "")).strip(), request_id)

        metadata_dispatch = (
            metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
        )
        self.assertEqual(str(metadata_dispatch.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(metadata_dispatch.get("dispatch_kind", "")).strip(),
            "bounded_bridge_warning_recommendation_request",
        )

        request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
        ack_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json")
        result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")

        self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(request_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(ack_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("task_id", "")).strip(), request_id)
        self.assertEqual(str(result_artifact.get("status", "")).strip(), "succeeded")
        self.assertEqual(
            str(result_artifact.get("dispatch_kind", "")).strip(),
            "bounded_bridge_warning_recommendation_request",
        )
        result_reason = str(result_artifact.get("result_reason", "")).lower()
        self.assertIn("tod should next", result_reason)
        self.assertTrue(
            "republish" in result_reason
            or "acknowledge" in result_reason
            or "next step" in result_reason
        )

        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)

        collaboration = (
            state_payload.get("collaboration_progress", {})
            if isinstance(state_payload.get("collaboration_progress", {}), dict)
            else {}
        )
        dispatch_telemetry = (
            state_payload.get("dispatch_telemetry", {})
            if isinstance(state_payload.get("dispatch_telemetry", {}), dict)
            else {}
        )

        self.assertEqual(str(collaboration.get("request_id", "")).strip(), request_id)
        self.assertEqual(str(dispatch_telemetry.get("request_id", "")).strip(), request_id)
        self.assertEqual(
            str(dispatch_telemetry.get("execution_lane", "")).strip(),
            "primitive_request_recovery",
        )

    def test_bounded_tod_bridge_warning_recommendation_request_preserves_browser_session_continuity_with_enter_submit(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What should TOD do next about the bridge warning?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")

            session_id_before_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_submit)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_input.clear()
            chat_input.send_keys(message)
            chat_input.send_keys(Keys.ENTER)

            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                >= len(initial_bubbles) + 2
            )

            payload = self._wait_for_captured_text_chat_result(
                driver,
                previous_request_id=previous_request_id,
            )
            request_id = str(payload.get("request_id", "")).strip()
            self.assertTrue(request_id)

            ui_request_id = self._wait_for_ui_request_id(
                previous_request_id=previous_request_id,
            )
            self.assertEqual(ui_request_id, request_id)

            request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
            result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
            self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
            self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

            session_id_after_submit = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_submit, session_id_before_submit)
            self.assertEqual(str(request_artifact.get("session_key", "")).strip(), session_id_before_submit)

            resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
            metadata = (
                resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
            )
            dispatch = (
                metadata.get("tod_dispatch", {}) if isinstance(metadata.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )

            result_reason = str(result_artifact.get("result_reason", "")).strip()
            self.assertTrue(result_reason)
            expected_reply = (
                f"Request {request_id}. I understood: {message}. "
                "Next action: dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result. "
                f"Status: done. Result: {result_reason}"
            )
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == expected_reply
            )

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(initial_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_recommendation_request_preserves_browser_session_continuity_across_repeated_turns(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What should TOD do next about the bridge warning?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)
            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)

            chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
            chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)

                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_1 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_1)

            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            request_id_1 = str(payload1.get("request_id", "")).strip()
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch1.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                initial_bubble_count + 4,
            )
            session_id_2 = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_2, session_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()

            self.assertNotEqual(request_id_2, request_id_1)
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch2.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_1)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), initial_bubble_count + 4)
            self.assertEqual(str(bubbles[-4].text).strip(), message)
            self.assertEqual(str(bubbles[-3].text).strip(), expected_reply1)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_recommendation_request_preserves_browser_session_continuity_after_reload(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What should TOD do next about the bridge warning?"
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, _ = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_reload)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch1.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_reload)

            driver.refresh()
            self._install_text_chat_result_capture(driver)
            wait.until(EC.presence_of_element_located((By.ID, "chatInput")))

            session_id_after_reload = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_reload, session_id_before_reload)

            reloaded_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(reloaded_bubbles), 1)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(reloaded_bubbles) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch2.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_reload)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), len(reloaded_bubbles) + 2)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
        finally:
            driver.quit()

    def test_bounded_tod_bridge_warning_recommendation_request_preserves_browser_session_continuity_after_clear(self) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.skipTest(f"selenium is unavailable: {exc}")

        message = "What should TOD do next about the bridge warning?"
        clear_reply = "Text chat cleared. Ready for your next message."
        state_status, state_payload = get_json("/mim/ui/state")
        self.assertEqual(state_status, 200, state_payload)
        self.assertIsInstance(state_payload, dict)
        previous_request_id = str(
            (
                state_payload.get("collaboration_progress", {})
                if isinstance(state_payload.get("collaboration_progress", {}), dict)
                else {}
            ).get("request_id", "")
        ).strip()

        driver = self._new_headless_firefox_driver()
        try:
            driver.get(f"{BASE_URL}/mim")
            self._install_text_chat_result_capture(driver)
            wait = WebDriverWait(driver, 20)

            def run_turn(prior_request_id: str, expected_bubble_count: int) -> tuple[dict, dict, str]:
                chat_input = wait.until(EC.presence_of_element_located((By.ID, "chatInput")))
                chat_send = wait.until(EC.element_to_be_clickable((By.ID, "chatSendBtn")))
                chat_input.clear()
                chat_input.send_keys(message)
                chat_send.click()

                wait.until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble"))
                    >= expected_bubble_count
                )

                payload = self._wait_for_captured_text_chat_result(
                    driver,
                    previous_request_id=prior_request_id,
                )
                request_id = str(payload.get("request_id", "")).strip()
                self.assertTrue(request_id)
                ui_request_id = self._wait_for_ui_request_id(
                    previous_request_id=prior_request_id,
                )
                self.assertEqual(ui_request_id, request_id)

                request_artifact = read_local_json(SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json")
                result_artifact = read_local_json(SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json")
                self.assertEqual(str(request_artifact.get("request_id", "")).strip(), request_id)
                self.assertEqual(str(result_artifact.get("request_id", "")).strip(), request_id)

                result_reason = str(result_artifact.get("result_reason", "")).strip()
                self.assertTrue(result_reason)
                expected_reply = (
                    f"Request {request_id}. I understood: {message}. "
                    "Next action: dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result. "
                    f"Status: done. Result: {result_reason}"
                )
                wait.until(
                    lambda d: str(
                        d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                    ).strip()
                    == expected_reply
                )
                return payload, request_artifact, expected_reply

            initial_bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            initial_bubble_count = len(initial_bubbles)
            payload1, request_artifact1, expected_reply1 = run_turn(
                previous_request_id,
                initial_bubble_count + 2,
            )
            session_id_before_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertTrue(session_id_before_clear)

            request_id_1 = str(payload1.get("request_id", "")).strip()
            resolution1 = payload1.get("resolution", {}) if isinstance(payload1, dict) else {}
            metadata1 = (
                resolution1.get("metadata_json", {}) if isinstance(resolution1, dict) else {}
            )
            dispatch1 = (
                metadata1.get("tod_dispatch", {}) if isinstance(metadata1.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata1.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch1.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact1.get("session_key", "")).strip(), session_id_before_clear)

            clear_btn = wait.until(EC.element_to_be_clickable((By.ID, "chatClearBtn")))
            clear_btn.click()
            wait.until(
                lambda d: str(
                    d.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")[-1].text
                ).strip()
                == clear_reply
            )

            bubbles_after_clear = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertEqual(len(bubbles_after_clear), 1)
            self.assertEqual(str(bubbles_after_clear[-1].text).strip(), clear_reply)

            session_id_after_clear = str(
                driver.execute_script(
                    "return localStorage.getItem('mim_text_chat_session_id') || '';"
                )
            ).strip()
            self.assertEqual(session_id_after_clear, session_id_before_clear)

            payload2, request_artifact2, expected_reply2 = run_turn(
                request_id_1,
                len(bubbles_after_clear) + 2,
            )
            request_id_2 = str(payload2.get("request_id", "")).strip()
            self.assertNotEqual(request_id_2, request_id_1)

            resolution2 = payload2.get("resolution", {}) if isinstance(payload2, dict) else {}
            metadata2 = (
                resolution2.get("metadata_json", {}) if isinstance(resolution2, dict) else {}
            )
            dispatch2 = (
                metadata2.get("tod_dispatch", {}) if isinstance(metadata2.get("tod_dispatch"), dict) else {}
            )
            self.assertEqual(str(metadata2.get("route_preference", "")).strip(), "goal_system")
            self.assertEqual(
                str(dispatch2.get("dispatch_kind", "")).strip(),
                "bounded_bridge_warning_recommendation_request",
            )
            self.assertEqual(str(request_artifact2.get("session_key", "")).strip(), session_id_before_clear)

            bubbles = driver.find_elements(By.CSS_SELECTOR, "#chatLog .chat-bubble")
            self.assertGreaterEqual(len(bubbles), 3)
            self.assertEqual(str(bubbles[-3].text).strip(), clear_reply)
            self.assertEqual(str(bubbles[-2].text).strip(), message)
            self.assertEqual(str(bubbles[-1].text).strip(), expected_reply2)
            self.assertNotEqual(expected_reply2, expected_reply1)
        finally:
            driver.quit()

    def test_tod_identity_question_returns_specific_tod_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "do you know who TOD is?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("tod", prompt)
        self.assertIn("task", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_function_question_without_question_mark_gets_direct_capability_answer(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what is your function MIM",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("summarize web pages", prompt)
        self.assertIn("runtime status", prompt)
        self.assertNotIn("got it:", prompt)

    def test_development_integration_request_returns_structured_reply_in_browser_surface(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "MIM, the goal/task for you is to leverage the existing mim_wall app on my mobile phone for direct interaction with you. How do we make this happen?",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("next action: inspect the existing mim_wall app", prompt)
        self.assertIn("steps:", prompt)
        self.assertIn("/mim", prompt)
        self.assertNotIn("ask for status", prompt)
        self.assertNotIn("i can answer that directly", prompt)

    def test_visibility_question_returns_camera_capability_boundary(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "can you see me MIM?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("camera", prompt)
        self.assertTrue(
            ("observations" in prompt) or ("i can currently see" in prompt),
            prompt,
        )
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_visibility_question_uses_live_camera_observation_context(self) -> None:
        session_id = f"objective78-camera-visible-{uuid.uuid4()}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-visible-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-visible"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what is visable?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("camera", prompt)
        self.assertIn("0.82", prompt)
        self.assertNotIn("i can answer that directly", prompt)

    def test_camera_presence_question_uses_live_camera_observation_context(
        self,
    ) -> None:
        session_id = f"objective78-camera-presence-{uuid.uuid4()}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-presence-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-presence"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Hi MIM, can you see me from the camera?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("camera", prompt)
        self.assertIn("0.82", prompt)
        self.assertIn("cannot confirm", prompt)
        self.assertNotIn("current camera observations", prompt)

    def test_visibility_question_summarizes_multiple_camera_feeds(self) -> None:
        session_id = f"objective78-camera-multi-{uuid.uuid4()}"

        status, front_camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-front-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-multi-front"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, front_camera)

        status, side_camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-side-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-multi-side"},
                "observations": [
                    {
                        "object_label": "operator-notebook",
                        "confidence": 0.77,
                        "zone": "side-left",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, side_camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what do you see from the camera?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("operator-notebook", prompt)
        self.assertIn("2 camera feeds", prompt)
        self.assertIn("0.82", prompt)

    def test_object_inquiry_reply_updates_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-inquiry-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-inquiry",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.88,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        first_prompt = (
            str(first_resolution.get("clarification_prompt", "")).strip().lower()
        )
        first_meta = (
            first_resolution.get("metadata_json", {})
            if isinstance(first_resolution, dict)
            else {}
        )
        inquiry_meta = (
            first_meta.get("object_inquiry", {}) if isinstance(first_meta, dict) else {}
        )

        self.assertIn(f"what is {object_label}".lower(), first_prompt)
        self.assertIn(f"what does {object_label} do".lower(), first_prompt)
        self.assertEqual(str(inquiry_meta.get("status", "")).strip(), "pending")

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = (
            str(second_resolution.get("clarification_prompt", "")).strip().lower()
        )

        self.assertIn("i will remember", second_prompt)
        self.assertIn("dock charger", second_prompt)
        self.assertIn("charging the handheld scanner", second_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(str(metadata.get("description", "")).strip(), "a dock charger")
        self.assertEqual(
            str(metadata.get("purpose", "")).strip(),
            "charging the handheld scanner",
        )
        self.assertIn("description", target.get("semantic_fields", []))
        self.assertIn("purpose", target.get("semantic_fields", []))

    def test_object_inquiry_followup_learns_owner_and_home_zone(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-enrichment-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-enrichment",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.87,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        first_prompt = (
            str(first_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn(f"what is {object_label}".lower(), first_prompt)
        self.assertIn(f"what does {object_label} do".lower(), first_prompt)

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = (
            str(second_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn(f"who owns {object_label}".lower(), second_prompt)
        self.assertIn(
            f"where should {object_label} normally live".lower(), second_prompt
        )

        third_status, third_payload = self._post_session_turn(
            session_id,
            "It belongs to Jordan. It should live on the charging bench.",
            parsed_intent="discussion",
        )
        self.assertEqual(third_status, 200, third_payload)

        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        third_prompt = (
            str(third_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn("belongs to jordan", third_prompt)
        self.assertIn("charging bench", third_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(str(metadata.get("owner", "")).strip(), "Jordan")
        self.assertEqual(
            str(metadata.get("expected_home_zone", "")).strip(),
            "charging bench",
        )
        self.assertIn("owner", target.get("semantic_fields", []))
        self.assertIn("expected_home_zone", target.get("semantic_fields", []))

    def test_object_inquiry_followup_learns_secondary_semantics(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-secondary-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-secondary",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.86,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn(f"who owns {object_label}".lower(), second_prompt)

        third_status, third_payload = self._post_session_turn(
            session_id,
            "It belongs to Jordan. It should live on the charging bench.",
            parsed_intent="discussion",
        )
        self.assertEqual(third_status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn(f"what kind of object is {object_label}".lower(), third_prompt)
        self.assertIn(
            f"what should i understand about {object_label}".lower(), third_prompt
        )
        self.assertIn(
            f"any notes i should remember about {object_label}".lower(), third_prompt
        )

        fourth_status, fourth_payload = self._post_session_turn(
            session_id,
            "Category is charging equipment. It means the scanner is ready for handoff. Note that the cable is loose.",
            parsed_intent="discussion",
        )
        self.assertEqual(fourth_status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("charging equipment", fourth_prompt)
        self.assertIn("ready for handoff", fourth_prompt)
        self.assertIn("cable is loose", fourth_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(
            str(metadata.get("category", "")).strip(), "charging equipment"
        )
        self.assertEqual(
            str(metadata.get("meaning", "")).strip(), "the scanner is ready for handoff"
        )
        self.assertEqual(
            str(metadata.get("user_notes", "")).strip(), "the cable is loose"
        )
        self.assertIn("category", target.get("semantic_fields", []))
        self.assertIn("meaning", target.get("semantic_fields", []))
        self.assertIn("user_notes", target.get("semantic_fields", []))

    def test_location_question_uses_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"bench_obj78_{run_id}"
        object_label = f"charger_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.93,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                    "expected_home_zone": scan_area,
                }
            ],
        )

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"where is Jordan's {object_label}?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("last recorded location", prompt)
        self.assertIn(scan_area.lower(), prompt)
        self.assertIn("jordan", prompt)
        self.assertNotIn("camera observation", prompt)

    def test_purpose_question_uses_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"desk_obj78_{run_id}"
        object_label = f"dock_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.91,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                }
            ],
        )

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"what is Jordan's {object_label} for?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("charging the handheld scanner", prompt)
        self.assertIn("jordan", prompt)

    def test_object_library_promotes_semantic_scan_objects(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"library_obj78_{run_id}"
        object_label = f"charger_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.94,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                    "description": "primary dock charger",
                    "expected_home_zone": scan_area,
                }
            ],
        )

        status, payload = get_json("/workspace/object-library", {"label": run_id})
        self.assertEqual(status, 200, payload)

        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        self.assertGreaterEqual(int(summary.get("promoted_objects", 0)), 1)

        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)
        self.assertTrue(bool(target.get("promoted")))
        self.assertGreaterEqual(float(target.get("library_score", 0.0)), 0.45)
        self.assertIn("purpose", target.get("semantic_fields", []))
        self.assertIn("owner", target.get("semantic_fields", []))
        self.assertIn("has semantic memory", target.get("promotion_reasons", []))
        self.assertEqual(str(target.get("zone", "")), scan_area)

    def test_noisy_general_questions_return_specific_answers(self) -> None:
        cases = [
            (
                "quickly can you tell me what time is it for me right now??",
                ["current time is", "utc"],
            ),
            (
                "honestly can you tell me what day is it today??",
                ["today is", "utc"],
            ),
            (
                "can you tell me where are we right now?",
                ["runtime environment", "chat session context"],
            ),
            (
                "honestly what is your primary mission for me?",
                ["primary mission", "assist safely"],
            ),
            (
                "can you tell me what is happening in camera feed right now?",
                ["camera"],
            ),
        ]

        for text, required_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                self.assertNotIn("direct answer: i heard your question", prompt)
                self.assertNotIn("got it:", prompt)
                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                if "camera feed" in text:
                    self.assertTrue(
                        ("observations" in prompt) or ("i can currently see" in prompt),
                        prompt,
                    )

    def test_tod_working_now_and_relationship_questions_are_specific(self) -> None:
        cases = [
            (
                "do you know what TOD is working on right now",
                ["tod", "active objective", "task state"],
            ),
            (
                "how do you and tod work together?",
                ["mim", "tod", "orchestrates"],
            ),
        ]

        for text, required_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                self.assertNotIn("direct answer: i heard your question", prompt)
                self.assertNotIn("got it:", prompt)
                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)

    def test_help_prompt_returns_capability_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "help??",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("summarize web pages", prompt)
        self.assertIn("runtime status", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_app_creation_prompt_returns_specific_planning_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "let's create an application today...",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("scope the application", prompt)
        self.assertIn("mvp", prompt)
        self.assertNotIn("got it:", prompt)

    def test_tod_social_media_capability_check_prompt_is_contextual(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "can you have TOD check the current capability of agentMIM app to run social media posts...",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()
        reason = str(resolution.get("reason", "")).strip()

        self.assertIn(
            reason,
            {
                "conversation_optional_escalation",
                "conversation_optional_escalation_followup",
            },
        )
        self.assertIn("tod", prompt)
        self.assertIn("social media", prompt)
        self.assertIn("create goal", prompt)

    def test_tod_social_media_capability_variant_is_contextual(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "have tod verify if agentmim can post to social media",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("tod", prompt)
        self.assertIn("social media", prompt)
        self.assertNotIn("got it:", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_next_slice_prompt_returns_direct_local_answer_without_web_research(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "MIM, current bounded slice is direct-answer quality and clarification behavior. Return the next bounded slice and acceptance criteria.",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        web_research = (
            metadata.get("web_research", {}) if isinstance(metadata, dict) else {}
        )

        self.assertIn("next bounded slice", prompt)
        self.assertTrue(
            ("acceptance checks" in prompt) or ("acceptance criteria" in prompt),
            prompt,
        )
        self.assertIn("direct local answer", prompt)
        self.assertNotIn("timed out", prompt)
        self.assertNotIn("reliable public sources", prompt)
        self.assertEqual(web_research, {})

    def test_chat_transcript_prompts_route_to_specific_answers(self) -> None:
        cases = [
            (
                "hi mim, how are you today",
                ["online", "operating normally"],
                ["got it:"],
            ),
            (
                "great! should we add anything to TODs tasks to improve the system",
                ["tod", "summary"],
                ["got it:"],
            ),
            (
                "what is the system?",
                ["mim", "tod", "objectives", "tasks"],
                ["i can answer that directly"],
            ),
            (
                "what is our objective?",
                ["objective", "reliability", "handoff"],
                ["i can answer that directly"],
            ),
            (
                "what are we working on today?",
                ["top priority today", "reliability"],
                ["i can answer that directly"],
            ),
            (
                "are you ready to start a project?",
                ["ready", "scope", "first tasks"],
                ["operating normally"],
            ),
            (
                "what is TODs status",
                ["tod status", "summary"],
                ["operating normally"],
            ),
            (
                "what is your mission?",
                ["primary mission", "assist safely"],
                ["i can answer that directly"],
            ),
            (
                "what is todays top news",
                ["top ai and tech themes today", "guardrails", "bot authenticity"],
                ["tod is your task and execution orchestration partner"],
            ),
        ]

        for text, required_fragments, forbidden_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, prompt)

    def test_casual_and_followup_conversation_prompts_stay_specific(self) -> None:
        cases = [
            (
                "hows TOD doing right now",
                ["tod status", "summary"],
                ["got it:", "i can answer that directly"],
            ),
            (
                "whats your mission",
                ["primary mission", "assist safely"],
                ["got it:"],
            ),
            (
                "just answer yes or no, are you healthy",
                ["yes", "healthy", "online"],
                ["got it:"],
            ),
            (
                "can we have a normal conversation?",
                ["direct", "conversational"],
                ["got it:"],
            ),
            (
                "you keep repeating yourself",
                ["avoid repeating", "direct"],
                ["got it:"],
            ),
            (
                "what exactly do you need from me",
                ["one concrete request", "question", "action"],
                ["got it:"],
            ),
            (
                "what is your top risk",
                ["top risk", "handoff"],
                ["got it:"],
            ),
            (
                "how do we reduce that risk",
                ["regression checks", "handoff verification"],
                ["got it:"],
            ),
        ]

        for text, required_fragments, forbidden_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "discussion",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, prompt)

    def test_session_followups_use_recent_conversation_context(self) -> None:
        session_id = f"objective78-followup-{uuid.uuid4()}"

        status, first_payload = self._post_session_turn(
            session_id,
            "what should we prioritize next?",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, first_payload)
        first_prompt = (
            str(
                (first_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("top priority today", first_prompt)

        status, second_payload = self._post_session_turn(
            session_id,
            "and after that?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("after that", second_prompt)
        self.assertIn("regression", second_prompt)

        status, third_payload = self._post_session_turn(
            session_id,
            "repeat that as a checklist",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("checklist:", third_prompt)
        self.assertIn("stabilize routing", third_prompt)

        status, fourth_payload = self._post_session_turn(
            session_id,
            "short final recap",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("one line", fourth_prompt)
        self.assertIn("stabilize routing", fourth_prompt)

    def test_session_followups_answer_why_dependencies_and_anything_else(self) -> None:
        session_id = f"objective78-followup-why-{uuid.uuid4()}"

        status, first_payload = self._post_session_turn(
            session_id,
            "what should we prioritize next?",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, first_payload)

        status, second_payload = self._post_session_turn(
            session_id,
            "why that?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("reliability", second_prompt)
        self.assertIn("handoff stability", second_prompt)

        status, third_payload = self._post_session_turn(
            session_id,
            "any dependencies?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("routing", third_prompt)
        self.assertIn("handoff", third_prompt)

        status, fourth_payload = self._post_session_turn(
            session_id,
            "anything else before we proceed?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("stale processes", fourth_prompt)
        self.assertIn("runtime", fourth_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
