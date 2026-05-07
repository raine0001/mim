import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.routers import mim_ui, tod_ui


class TodUiPublicAuthTest(unittest.TestCase):
    def _build_client(self, *, base_url: str) -> TestClient:
        app = FastAPI()
        app.include_router(mim_ui.router)
        app.include_router(tod_ui.router)
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

    def test_public_tod_redirects_to_login_when_not_authenticated(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.get("/tod", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/mim/login?next=/tod")

    def test_public_tod_state_requires_auth(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            response = client.get("/tod/ui/state")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "mimtod_login_required"})

    def test_public_login_allows_tod_page_and_state(self) -> None:
        with self._auth_settings(), self._build_client(base_url="https://mim.mimtod.com") as client:
            login_response = client.post(
                "/mim/login",
                data={"username": "dave", "password": "secret-pass", "next": "/tod"},
                follow_redirects=False,
            )
            page_response = client.get("/tod")
            state_response = client.get("/tod/ui/state")

        self.assertEqual(login_response.status_code, 303)
        self.assertEqual(login_response.headers.get("location"), "/tod")
        self.assertIn("mimtod_operator_session=", login_response.headers.get("set-cookie", ""))
        self.assertEqual(page_response.status_code, 200)
        self.assertIn("TOD Console", page_response.text)
        self.assertEqual(state_response.status_code, 200)
        self.assertIsInstance(state_response.json(), dict)

    def test_loopback_tod_bypasses_auth(self) -> None:
        with self._auth_settings(), self._build_client(base_url="http://127.0.0.1:18001") as client:
            response = client.get("/tod")

        self.assertEqual(response.status_code, 200)
        self.assertIn("TOD Console", response.text)


if __name__ == "__main__":
    unittest.main()