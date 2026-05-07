import json
import os
import urllib.error
import urllib.parse
import urllib.request
import unittest
from uuid import uuid4


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


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective173SelfEvolutionProgressTest(unittest.TestCase):
    def test_objective173_self_evolution_progress_promotes_and_holds(self) -> None:
        run_id = uuid4().hex[:8]
        actor = f"objective173-test-{run_id}"
        source = "objective173-focused"

        status, payload = post_json(
            "/improvement/self-evolution/natural-language/reset",
            {
                "actor": actor,
                "source": source,
            },
        )
        self.assertEqual(status, 200, payload)
        progress = payload.get("progress", {}) if isinstance(payload, dict) else {}
        self.assertEqual(str(progress.get("active_slice_id", "")).strip(), "slice_01")
        self.assertEqual(str(progress.get("status", "")).strip(), "running")

        status, payload = post_json(
            "/improvement/self-evolution/natural-language/evaluate",
            {
                "actor": actor,
                "source": source,
                "metrics_json": {
                    "overall": 0.83,
                    "relevance": 0.86,
                    "task_completion": 0.81,
                },
                "failure_tags": [],
                "proof_summary": "Slice 1 pass proof recorded.",
                "discovered_skill_candidates": ["short_followup_intent_lock"],
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(str(payload.get("outcome", "")).strip(), "pass")
        progress = payload.get("progress", {}) if isinstance(payload.get("progress", {}), dict) else {}
        self.assertEqual(str(progress.get("active_slice_id", "")).strip(), "slice_02")
        self.assertEqual(str(progress.get("status", "")).strip(), "running")
        self.assertIn("slice_01", progress.get("completed_slice_ids", []))

        status, payload = get_json(
            "/improvement/self-evolution/briefing",
            {
                "actor": actor,
                "source": source,
                "limit": 10,
            },
        )
        self.assertEqual(status, 200, payload)
        briefing = payload.get("briefing", {}) if isinstance(payload, dict) else {}
        natural_language_development = (
            briefing.get("natural_language_development", {})
            if isinstance(briefing.get("natural_language_development", {}), dict)
            else {}
        )
        self.assertEqual(str(natural_language_development.get("active_slice_id", "")).strip(), "slice_02")
        self.assertEqual(str(natural_language_development.get("selected_skill_title", "")).strip(), "Decision Flow")
        self.assertIn("Decision Flow Control", str(natural_language_development.get("active_slice_summary", "")))

        status, payload = post_json(
            "/improvement/self-evolution/natural-language/evaluate",
            {
                "actor": actor,
                "source": source,
                "metrics_json": {
                    "overall": 0.7,
                    "task_completion": 0.7,
                    "initiative": 0.7,
                },
                "failure_tags": ["explicit_request_missed"],
                "proof_summary": "Slice 2 failed pass gate.",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(str(payload.get("outcome", "")).strip(), "fail")
        progress = payload.get("progress", {}) if isinstance(payload.get("progress", {}), dict) else {}
        self.assertEqual(str(progress.get("active_slice_id", "")).strip(), "slice_02")
        self.assertEqual(str(progress.get("status", "")).strip(), "repairing")
        self.assertGreaterEqual(int(progress.get("repair_count", 0) or 0), 1)

        status, payload = post_json(
            "/improvement/self-evolution/natural-language/evaluate",
            {
                "actor": actor,
                "source": source,
                "outcome_mode": "blocked",
                "blocked_reason": "validation_untrusted",
                "proof_summary": "Held because validation could not be trusted.",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(str(payload.get("outcome", "")).strip(), "blocked")
        progress = payload.get("progress", {}) if isinstance(payload.get("progress", {}), dict) else {}
        self.assertEqual(str(progress.get("active_slice_id", "")).strip(), "slice_02")
        self.assertEqual(str(progress.get("status", "")).strip(), "blocked")
        self.assertEqual(str(progress.get("blocked_reason", "")).strip(), "validation_untrusted")


if __name__ == "__main__":
    unittest.main(verbosity=2)