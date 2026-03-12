import json
import os
import unittest
import urllib.error
import urllib.request
from uuid import uuid4


BASE_URL = os.getenv("MIM_TEST_BASE_URL", "http://127.0.0.1:8001")


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
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective29DirectedTargetingTest(unittest.TestCase):
    def _register_workspace_scan(self) -> None:
        status, payload = post_json(
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
        self.assertEqual(status, 200, payload)

    def _run_scan(self, *, run_id: str, observations: list[dict], scan_area: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scan_area,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)

        execution_id = event["execution"]["execution_id"]
        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": state, "reason": state, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, updated)

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {"observations": observations},
            },
        )
        self.assertEqual(status, 200, succeeded)

    def test_directed_target_resolution_policy_paths(self) -> None:
        run_id = uuid4().hex[:8]
        self._register_workspace_scan()

        exact_zone = f"front-center-obj29-exact-{run_id}"
        exact_label = f"obj29 exact target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=exact_zone,
            observations=[{"label": exact_label, "zone": exact_zone, "confidence": 0.96}],
        )

        status, exact_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": exact_label,
                "preferred_zone": exact_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": True,
            },
        )
        self.assertEqual(status, 200, exact_resolve)
        self.assertEqual(exact_resolve.get("match_outcome"), "exact_match")
        self.assertEqual(exact_resolve.get("policy_outcome"), "target_confirmed")
        self.assertEqual(exact_resolve.get("status"), "confirmed")
        self.assertIsNotNone(exact_resolve.get("related_object_id"))

        target_resolution_id = exact_resolve["target_resolution_id"]
        status, target_detail = get_json(f"/workspace/targets/{target_resolution_id}")
        self.assertEqual(status, 200, target_detail)
        self.assertEqual(target_detail.get("target_resolution_id"), target_resolution_id)

        ambiguous_zone = f"rear-center-obj29-amb-{run_id}"
        amb_a = f"amber cube alpha {run_id}"
        amb_b = f"amber cube beta {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=ambiguous_zone,
            observations=[
                {"label": amb_a, "zone": ambiguous_zone, "confidence": 0.9},
                {"label": amb_b, "zone": ambiguous_zone, "confidence": 0.91},
            ],
        )

        status, ambiguous_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": "amber cube",
                "preferred_zone": "",
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, ambiguous_resolve)
        self.assertEqual(ambiguous_resolve.get("match_outcome"), "ambiguous_candidates")
        self.assertEqual(ambiguous_resolve.get("policy_outcome"), "target_requires_confirmation")
        self.assertEqual(ambiguous_resolve.get("status"), "pending_confirmation")

        ambiguous_id = ambiguous_resolve["target_resolution_id"]
        status, confirmed = post_json(
            f"/workspace/targets/{ambiguous_id}/confirm",
            {"actor": "operator", "reason": "visual confirmation", "metadata_json": {"run_id": run_id}},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed.get("status"), "confirmed")
        self.assertEqual(confirmed.get("policy_outcome"), "target_confirmed")
        self.assertIsNotNone(confirmed.get("proposal_id"))

        stale_zone = f"front-left-obj29-stale-{run_id}"
        stale_label = f"obj29 stale target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=stale_zone,
            observations=[{"label": stale_label, "zone": stale_zone, "confidence": 0.88}],
        )
        self._run_scan(
            run_id=run_id,
            scan_area=stale_zone,
            observations=[{"label": f"different object {run_id}", "zone": stale_zone, "confidence": 0.93}],
        )

        status, stale_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": stale_label,
                "preferred_zone": stale_zone,
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, stale_resolve)
        self.assertEqual(stale_resolve.get("policy_outcome"), "target_stale_reobserve")
        self.assertEqual(stale_resolve.get("status"), "pending_confirmation")

        unsafe_zone = f"rear-right-obj29-unsafe-{run_id}"
        unsafe_label = f"obj29 unsafe target {run_id}"
        self._run_scan(
            run_id=run_id,
            scan_area=unsafe_zone,
            observations=[{"label": unsafe_label, "zone": unsafe_zone, "confidence": 0.92}],
        )

        status, unsafe_resolve = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": unsafe_label,
                "preferred_zone": unsafe_zone,
                "source": "integration-test",
                "unsafe_zones": [unsafe_zone],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, unsafe_resolve)
        self.assertEqual(unsafe_resolve.get("policy_outcome"), "target_blocked_unsafe_zone")
        self.assertEqual(unsafe_resolve.get("status"), "blocked")

        status, no_match = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": f"unfindable target marker {uuid4().hex}",
                "preferred_zone": "front-center",
                "source": "integration-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, no_match)
        self.assertEqual(no_match.get("match_outcome"), "no_match")
        self.assertEqual(no_match.get("policy_outcome"), "target_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
