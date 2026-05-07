import base64
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.db import get_db
from core.routers import gateway, mim_ui


async def _fake_db_override():
    yield SimpleNamespace()


class MimUiPublicAuthTest(unittest.TestCase):
    def _basic_auth_header(self) -> dict[str, str]:
        credentials = base64.b64encode(b"dave:secret-pass").decode("ascii")
        return {"Authorization": f"Basic {credentials}"}

    def _build_client(self, *, base_url: str) -> TestClient:
        app = FastAPI()
        app.include_router(mim_ui.router)
        app.include_router(gateway.router, prefix="/gateway")
        app.dependency_overrides[get_db] = _fake_db_override
        return TestClient(app, base_url=base_url)

    def _auth_settings(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(settings, "mimtod_login_enabled", True))
        stack.enter_context(patch.object(settings, "mimtod_user", "dave"))
        stack.enter_context(patch.object(settings, "mimtod_password", "secret-pass"))
        stack.enter_context(patch.object(settings, "mimtod_session_hours", 12))
        stack.enter_context(patch.object(settings, "remote_shell_domain", "https://mim.mimtod.com"))
        stack.enter_context(patch.object(settings, "remote_shell_hostname", "mim.mimtod.com"))
        stack.enter_context(patch.object(settings, "remote_shell_zone", "mimtod.com"))
        return stack

    def test_public_mim_redirects_to_login_when_not_authenticated(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.get("/mim", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/mim/login?next=/mim")

    def test_public_login_sets_cookie_and_allows_mim_page(self) -> None:
        with self._auth_settings(), patch(
            "core.routers.mim_ui._load_mim_ui_chat_thread",
            new=AsyncMock(return_value={"session": {}, "messages": []}),
        ), self._build_client(base_url="https://mim.mimtod.com") as client:
            login_response = client.post(
                "/mim/login",
                data={"username": "dave", "password": "secret-pass", "next": "/mim"},
                follow_redirects=False,
            )
            page_response = client.get("/mim")

        self.assertEqual(login_response.status_code, 303)
        self.assertEqual(login_response.headers.get("location"), "/mim")
        self.assertIn("mimtod_operator_session=", login_response.headers.get("set-cookie", ""))
        self.assertEqual(page_response.status_code, 200)
        self.assertIn("<title>MIM</title>", page_response.text)

    def test_apex_login_redirects_to_dedicated_mim_host(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mimtod.com") as client:
            response = client.get("/mim/login?next=/mim", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "https://mim.mimtod.com/mim/login?next=/mim")

    def test_apex_mim_redirects_to_dedicated_mim_host(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mimtod.com") as client:
            response = client.get("/mim", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers.get("location"), "https://mim.mimtod.com/mim")

    def test_public_gateway_text_requires_auth(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.post(
                "/gateway/intake/text",
                json={
                    "text": "check status",
                    "parsed_intent": "question",
                    "confidence": 0.8,
                    "target_system": "mim",
                    "requested_goal": "conversation",
                    "safety_flags": [],
                    "metadata_json": {},
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "mimtod_login_required"})

    def test_public_gateway_text_accepts_basic_auth(self) -> None:
        with self._auth_settings(), patch(
            "core.routers.gateway._store_normalized",
            new=AsyncMock(return_value={"status": "accepted"}),
        ), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.post(
                "/gateway/intake/text",
                headers=self._basic_auth_header(),
                json={
                    "text": "check status",
                    "parsed_intent": "question",
                    "confidence": 0.8,
                    "target_system": "mim",
                    "requested_goal": "conversation",
                    "safety_flags": [],
                    "metadata_json": {},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("status"), "accepted")
        self.assertEqual(
            response.json().get("route_diagnostics", {}).get("route_name"),
            "gateway.intake_text",
        )

    def test_mim_console_text_route_keeps_servo_probe_on_local_execution_path(self) -> None:
        captured_payloads = []

        async def fake_store(payload, db):
            captured_payloads.append(payload)
            return {
                "status": "accepted",
                "resolution": {
                    "internal_intent": "execute_capability",
                    "capability_name": "mim_arm.supervised_probe",
                    "metadata_json": {},
                },
            }

        with self._auth_settings(), patch(
            "core.routers.gateway._store_normalized",
            new=AsyncMock(side_effect=fake_store),
        ), patch(
            "core.routers.mim_ui._load_mim_ui_chat_thread",
            new=AsyncMock(return_value={"session": {}, "messages": []}),
        ), self._build_client(base_url="https://mim.mimtod.com") as client:
            page_response = client.get("/mim", headers=self._basic_auth_header())
            response = client.post(
                "/gateway/intake/text",
                headers=self._basic_auth_header(),
                json={
                    "text": "MIM-ARM-MULTI-SERVO-ENVELOPE-PROBE-PREP",
                    "parsed_intent": "discussion",
                    "confidence": 0.95,
                    "target_system": "mim",
                    "requested_goal": "",
                    "safety_flags": [],
                    "metadata_json": {
                        "source": "mim_ui_text_chat",
                        "interaction_mode": "text",
                        "message_type": "user",
                        "conversation_session_id": "test-console-route",
                        "route_preference": "conversation_layer",
                    },
                },
            )

        self.assertEqual(page_response.status_code, 200)
        self.assertIn("fetch('/gateway/intake/text'", page_response.text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_payloads[0].parsed_intent, "robotics_supervised_probe")
        self.assertEqual(
            captured_payloads[0].metadata_json.get("capability"),
            "mim_arm.supervised_probe",
        )
        self.assertEqual(
            captured_payloads[0].metadata_json.get("route_preference"),
            "goal_system",
        )
        diagnostics = response.json().get("route_diagnostics", {})
        self.assertEqual(diagnostics.get("route_name"), "gateway.intake_text")
        self.assertEqual(diagnostics.get("classifier_outcome"), "robotics_supervised_probe")
        self.assertEqual(diagnostics.get("capability_name"), "mim_arm.supervised_probe")
        self.assertTrue(diagnostics.get("web_fallback_blocked"))
        self.assertFalse(diagnostics.get("web_search_attempted"))

    def test_training_action_requires_auth(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.post("/mim/ui/training/action", json={"action": "restart"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "mimtod_login_required"})

    def test_training_action_forwards_requested_action(self) -> None:
        expected = {"ok": True, "action": "restart", "services": []}
        with self._auth_settings(), patch(
            "core.routers.mim_ui.control_training_routine",
            return_value=expected,
        ) as control_mock, self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.post(
                "/mim/ui/training/action",
                headers=self._basic_auth_header(),
                json={"action": "restart"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        control_mock.assert_called_once_with("restart")

    def test_training_action_returns_bad_request_for_invalid_action(self) -> None:
        with self._auth_settings(), patch(
            "core.routers.mim_ui.control_training_routine",
            side_effect=ValueError("unsupported action"),
        ) as control_mock, self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.post(
                "/mim/ui/training/action",
                headers=self._basic_auth_header(),
                json={"action": "launch"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "unsupported action"})
        control_mock.assert_called_once_with("launch")

    def test_loopback_mim_page_bypasses_auth(self) -> None:
        with self._auth_settings(), patch(
            "core.routers.mim_ui._load_mim_ui_chat_thread",
            new=AsyncMock(return_value={"session": {}, "messages": []}),
        ), self._build_client(base_url="http://127.0.0.1:18001") as client:
            response = client.get("/mim")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>MIM</title>", response.text)


if __name__ == "__main__":
    unittest.main()
