import json
import os
import unittest
import urllib.error
import urllib.request
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


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective40HumanPreferenceRoutineMemoryTest(unittest.TestCase):
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

    def _run_scan(self, *, text: str, scan_area: str, observations: list[dict]) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": "observe_workspace",
                "confidence": 0.96,
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

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {"observations": observations},
            },
        )
        self.assertEqual(status, 200, done)

    def test_objective40_preference_memory_and_policy_integration(self) -> None:
        run_id = uuid4().hex[:8]
        zone_a = f"front-center-obj40-{run_id}"
        zone_b = f"rear-center-obj40-{run_id}"
        label = f"obj40-target-{run_id}"

        self._register_workspace_scan()

        for pref_type, value in [
            ("preferred_confirmation_threshold", 0.88),
            ("preferred_scan_zones", [zone_a]),
            ("auto_exec_tolerance", 0.92),
            ("notification_verbosity", "high"),
            ("auto_exec_safe_tasks", True),
        ]:
            status, upserted = post_json(
                "/preferences",
                {
                    "user_id": "operator",
                    "preference_type": pref_type,
                    "value": value,
                    "confidence": 0.86,
                    "source": "objective40-test",
                },
            )
            self.assertEqual(status, 200, upserted)
            self.assertEqual(upserted.get("preference_type"), pref_type)

        status, all_prefs = get_json("/preferences?user_id=operator")
        self.assertEqual(status, 200, all_prefs)
        self.assertGreaterEqual(len(all_prefs.get("preferences", [])), 5)

        status, one_pref = get_json("/preferences/preferred_confirmation_threshold?user_id=operator")
        self.assertEqual(status, 200, one_pref)
        self.assertAlmostEqual(float(one_pref.get("value", 0.0)), 0.88, places=2)

        status, _ = post_json(
            "/workspace/autonomy/override",
            {
                "actor": "operator",
                "reason": "objective40 keep proposals pending",
                "force_manual_approval": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200)

        self._run_scan(
            text=f"objective40 baseline {run_id}",
            scan_area=zone_a,
            observations=[{"label": label, "zone": zone_a, "confidence": 0.95}],
        )
        self._run_scan(
            text=f"objective40 moved {run_id}",
            scan_area=zone_b,
            observations=[{"label": label, "zone": zone_b, "confidence": 0.94}],
        )

        status, nxt = get_json("/workspace/proposals/next?actor=operator&reason=objective40-check&status=pending")
        self.assertEqual(status, 200, nxt)
        self.assertTrue(nxt.get("selected"))
        self.assertEqual(nxt.get("notification", {}).get("verbosity"), "high")

        selected = nxt.get("proposal", {})
        context = selected.get("metadata_json", {}).get("preference_context", {})
        self.assertIn(zone_a, context.get("preferred_scan_zones", []))
        tolerance_value = float(context.get("auto_exec_tolerance", 0.0))
        self.assertGreaterEqual(tolerance_value, 0.0)
        self.assertLessEqual(tolerance_value, 1.0)

        status, pending = get_json("/workspace/proposals?status=pending")
        self.assertEqual(status, 200, pending)
        pending_rows = pending.get("proposals", [])
        self.assertGreaterEqual(len(pending_rows), 2)

        first_id = int(pending_rows[0]["proposal_id"])
        second_id = int(pending_rows[1]["proposal_id"])

        status, accepted = post_json(
            f"/workspace/proposals/{first_id}/accept",
            {
                "actor": "operator",
                "reason": "objective40 learning accept",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("status"), "accepted")

        status, rejected = post_json(
            f"/workspace/proposals/{second_id}/reject",
            {
                "actor": "operator",
                "reason": "objective40 learning reject",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected.get("status"), "rejected")

        status, bias = get_json("/preferences/action_approval_bias?user_id=operator")
        self.assertEqual(status, 200, bias)
        bias_value = bias.get("value", {})
        self.assertGreaterEqual(int(bias_value.get("approvals", 0)), 1)
        self.assertGreaterEqual(int(bias_value.get("rejections", 0)), 1)
        self.assertGreater(float(bias.get("confidence", 0.0)), 0.2)

        status, tolerance = get_json("/preferences/auto_exec_tolerance?user_id=operator")
        self.assertEqual(status, 200, tolerance)
        self.assertGreaterEqual(float(tolerance.get("confidence", 0.0)), 0.25)

        status, resolved = post_json(
            "/workspace/targets/resolve",
            {
                "target_label": label,
                "preferred_zone": zone_b,
                "source": "objective40-test",
                "unsafe_zones": [],
                "create_proposal": False,
            },
        )
        self.assertEqual(status, 200, resolved)
        self.assertIn("applied_confirmation_threshold", resolved)
        self.assertLessEqual(float(resolved.get("applied_confirmation_threshold", 1.0)), 0.88)


if __name__ == "__main__":
    unittest.main(verbosity=2)
