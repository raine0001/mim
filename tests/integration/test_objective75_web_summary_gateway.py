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
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective75WebSummaryGatewayTest(unittest.TestCase):
    def test_web_summary_endpoint_guard_or_success(self) -> None:
        status, payload = post_json(
            "/gateway/web/summarize",
            {
                "url": "https://example.com",
                "timeout_seconds": 10,
                "max_summary_sentences": 3,
            },
        )

        # Supports both deployment modes:
        # - secure default: web access disabled (403)
        # - runtime enabled: summary succeeds (200)
        self.assertIn(status, (200, 403), payload)
        if status == 403:
            self.assertEqual(payload.get("detail"), "web_access_disabled")
            return

        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload.get("url"), "https://example.com")
        self.assertIn("summary", payload)
        self.assertIn("excerpt", payload)
        self.assertIsInstance(payload.get("memory_id"), int)

    def test_web_summary_rejects_unsafe_or_invalid_url(self) -> None:
        status, payload = post_json(
            "/gateway/web/summarize",
            {
                "url": "file:///etc/passwd",
            },
        )

        # When web access is disabled globally, guard should trigger first.
        # When enabled, this URL must still be rejected by URL policy.
        self.assertIn(status, (403, 422), payload)
        if status == 403:
            self.assertEqual(payload.get("detail"), "web_access_disabled")
        else:
            self.assertEqual(payload.get("detail"), "unsupported_or_unsafe_url")


if __name__ == "__main__":
    unittest.main(verbosity=2)
