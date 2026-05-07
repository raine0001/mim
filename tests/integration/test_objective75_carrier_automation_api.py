import base64
import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


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
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, object]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return resp.status, parsed


class Objective75CarrierAutomationApiTest(unittest.TestCase):
    def test_live_session_creation_is_graceful_when_unavailable(self) -> None:
        status, payload = post_json(
            "/automation/web/sessions",
            {
                "carrier_id": "carrier-live-smoke",
                "session_key": "",
                "start_url": "https://example.com",
                "simulation_mode": False,
                "headless": True,
            },
        )

        # Runtime may be disabled (503), live path may be unavailable (422),
        # or fully available and create a live session (200). It must never 500.
        self.assertIn(status, (200, 422, 503), payload)
        if status == 422:
            self.assertIn(
                payload.get("detail"),
                {"playwright_not_installed", "live_browser_disabled"},
                payload,
            )

    def test_automation_end_to_end_simulation(self) -> None:
        status, session_payload = post_json(
            "/automation/web/sessions",
            {
                "carrier_id": "carrier-alpha",
                "session_key": "",
                "start_url": "https://example.com",
                "simulation_mode": True,
            },
        )
        if status == 503:
            self.assertEqual(session_payload.get("detail"), "automation_disabled")
            return

        self.assertEqual(status, 200, session_payload)
        session_id = int(session_payload["session_id"])

        status, nav_payload = post_json(
            f"/automation/web/sessions/{session_id}/navigate",
            {"url": "https://example.com", "timeout_seconds": 10},
        )
        self.assertEqual(status, 200, nav_payload)
        self.assertTrue(nav_payload.get("ok"), nav_payload)

        status, action_payload = post_json(
            f"/automation/web/sessions/{session_id}/actions",
            {
                "action": "detect",
                "selector": "#report-link",
                "timeout_seconds": 5,
            },
        )
        self.assertEqual(status, 200, action_payload)
        self.assertTrue(action_payload.get("ok"), action_payload)

        status, auth_payload = post_json(
            "/automation/auth/resolve",
            {
                "session_id": session_id,
                "carrier_id": "carrier-alpha",
                "username": "user@example.com",
                "password": "secret",
                "pause_if_mfa_detected": True,
            },
        )
        self.assertEqual(status, 200, auth_payload)
        self.assertIn(
            auth_payload.get("status"),
            {"paused_for_mfa", "authenticated"},
            auth_payload,
        )

        if auth_payload.get("status") == "paused_for_mfa":
            challenge_key = str(auth_payload.get("challenge_key"))
            status, mfa_extract_payload = post_json(
                "/automation/email/extract-mfa",
                {
                    "carrier_id": "carrier-alpha",
                    "challenge_key": challenge_key,
                    "lookback_minutes": 15,
                },
            )
            self.assertEqual(status, 200, mfa_extract_payload)

            code = str(mfa_extract_payload.get("latest_code") or "123456")
            status, resume_payload = post_json(
                f"/automation/auth/challenges/{challenge_key}/resume",
                {
                    "actor": "operator",
                    "mfa_code": code,
                    "reason": "integration-test",
                },
            )
            self.assertEqual(status, 200, resume_payload)
            self.assertEqual(resume_payload.get("status"), "resolved", resume_payload)

        status, nav_exec_payload = post_json(
            "/automation/navigation/execute",
            {
                "session_id": session_id,
                "carrier_id": "carrier-alpha",
                "steps": [
                    {"action": "detect", "selector": "#home"},
                    {"action": "wait_for", "selector": "#reports"},
                ],
            },
        )
        self.assertEqual(status, 200, nav_exec_payload)
        self.assertIn("results", nav_exec_payload)

        status, detect_payload = post_json(
            "/automation/files/detect",
            {
                "session_id": session_id,
                "carrier_id": "carrier-alpha",
                "expected_name_pattern": "carrier-alpha-report.csv",
                "source_url": "https://example.com/report",
            },
        )
        self.assertEqual(status, 200, detect_payload)
        artifact_id = int(detect_payload["artifact_id"])

        content = "a,b\n1,2\n"
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        status, download_payload = post_json(
            "/automation/files/download",
            {
                "artifact_id": artifact_id,
                "carrier_id": "carrier-alpha",
                "url": "https://example.com/report.csv",
                "file_name": "carrier-alpha-report.csv",
                "content_base64": content_b64,
            },
        )
        self.assertEqual(status, 200, download_payload)
        self.assertTrue(download_payload.get("ok"), download_payload)

        status, playbook_payload = post_json(
            "/automation/playbooks",
            {
                "carrier_id": "carrier-alpha",
                "enabled": True,
                "navigation_steps": [{"action": "detect", "selector": "#reports"}],
                "report_location_logic": {"type": "table", "selector": "#reports"},
                "parsing_rules": {"format": "csv"},
                "recovery_rules": {"max_retries": 3},
            },
        )
        self.assertEqual(status, 200, playbook_payload)
        self.assertTrue(playbook_payload.get("ok"), playbook_payload)

        status, run_payload = post_json(
            "/automation/runs",
            {
                "run_key": "",
                "triggered_by": "integration-test",
                "carriers": ["carrier-alpha"],
            },
        )
        self.assertEqual(status, 200, run_payload)
        run_id = int(run_payload["run_id"])

        status, carrier_status_payload = post_json(
            f"/automation/runs/{run_id}/carriers",
            {
                "carrier_id": "carrier-alpha",
                "status": "success",
                "retries": 1,
                "requires_human_action": False,
                "last_error": "",
                "last_step_index": 2,
            },
        )
        self.assertEqual(status, 200, carrier_status_payload)
        self.assertEqual(
            carrier_status_payload.get("status"), "success", carrier_status_payload
        )

        status, monitor_payload = get_json("/automation/status/monitor")
        self.assertEqual(status, 200, monitor_payload)
        self.assertIn("carrier_status_counts", monitor_payload)

    def test_email_poll_simulation_and_reconciliation(self) -> None:
        status, poll_payload = post_json(
            "/automation/email/poll",
            {
                "source": "simulation",
                "mailbox": "INBOX",
                "simulation_messages": [
                    {
                        "sender": "no-reply@carrier.com",
                        "recipient": "ops@example.com",
                        "subject": "Your MFA code",
                        "body": "Your code is 654321",
                    }
                ],
            },
        )
        if status == 503:
            self.assertEqual(poll_payload.get("detail"), "automation_disabled")
            return

        self.assertEqual(status, 200, poll_payload)
        self.assertGreaterEqual(int(poll_payload.get("count", 0)), 1)

        status, recon_payload = post_json(
            "/automation/reconciliation/evaluate",
            {
                "carrier_id": "carrier-alpha",
                "current_totals": {"total_premium": 1400.0},
                "previous_totals": {"total_premium": 1000.0},
                "expected_carriers": ["carrier-alpha", "carrier-beta"],
                "present_carriers": ["carrier-alpha"],
                "anomaly_threshold_pct": 20.0,
            },
        )
        self.assertEqual(status, 200, recon_payload)
        self.assertGreaterEqual(int(recon_payload.get("anomaly_count", 0)), 1)
        self.assertGreaterEqual(int(recon_payload.get("missing_carrier_count", 0)), 1)

    def test_calendar_auth_url_and_simulated_reminder(self) -> None:
        status, auth_payload = post_json(
            "/automation/calendar/google/auth-url",
            {
                "state": "objective75-calendar-test",
                "scopes": ["https://www.googleapis.com/auth/calendar.events"],
                "access_type": "offline",
                "prompt": "consent",
            },
        )
        if status == 503:
            self.assertEqual(auth_payload.get("detail"), "automation_disabled")
            return

        # In environments without calendar credentials this can be 422.
        self.assertIn(status, (200, 422), auth_payload)
        if status == 200:
            self.assertTrue(auth_payload.get("ok"), auth_payload)
            self.assertIn("accounts.google.com", str(auth_payload.get("auth_url", "")))

        start_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        end_at = start_at + timedelta(minutes=30)
        status, reminder_payload = post_json(
            "/automation/calendar/reminders",
            {
                "source": "simulation",
                "title": "MIM Project Goals Review",
                "description": "Review MIM/TOD goals and next milestones.",
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "calendar_id": "primary",
                "reminder_minutes": [30, 10],
                "attendees": ["mim@agentmim.com"],
            },
        )
        self.assertEqual(status, 200, reminder_payload)
        self.assertTrue(reminder_payload.get("ok"), reminder_payload)
        self.assertEqual(
            reminder_payload.get("provider"), "simulation", reminder_payload
        )
        self.assertTrue(str(reminder_payload.get("event_id", "")).startswith("sim-"))
        self.assertEqual(
            str(reminder_payload.get("timezone", "")),
            "America/Los_Angeles",
            reminder_payload,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
