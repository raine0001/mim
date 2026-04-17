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
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": body}


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if not body:
            return exc.code, {}
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw_body": body}


class Objective118BoundaryGovernedCapabilityChainsTest(unittest.TestCase):
    def _recompute_operator_required_boundary(self, *, scope: str, run_id: str) -> dict:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective118-test",
                "source": "objective118-boundary-governed-capability-chains",
                "scope": scope,
                "lookback_hours": 72,
                "min_samples": 5,
                "apply_recommended_boundaries": True,
                "hard_ceiling_overrides": {
                    "human_safety": True,
                    "legality": True,
                    "system_integrity": True,
                },
                "evidence_inputs_override": {
                    "sample_count": 18,
                    "success_rate": 0.29,
                    "escalation_rate": 0.58,
                    "retry_rate": 0.37,
                    "interruption_rate": 0.31,
                    "memory_delta_rate": 0.18,
                    "override_rate": 0.45,
                    "replan_rate": 0.35,
                    "environment_stability": 0.24,
                    "development_confidence": 0.33,
                    "constraint_reliability": 0.39,
                    "experiment_confidence": 0.26,
                },
                "metadata_json": {"run_id": run_id, "objective": "118"},
            },
        )
        self.assertEqual(status, 200, payload)
        boundary = payload.get("boundary", {}) if isinstance(payload, dict) else {}
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)
        return boundary

    def _find_journal_entry(self, *, action: str, run_id: str) -> dict:
        status, journal = get_json("/journal")
        self.assertEqual(status, 200, journal)
        rows = journal if isinstance(journal, list) else []
        match = next(
            (
                entry
                for entry in rows
                if isinstance(entry, dict)
                and str(entry.get("action", "")) == action
                and str(
                    (
                        entry.get("metadata_json", {})
                        if isinstance(entry.get("metadata_json", {}), dict)
                        else {}
                    ).get("run_id", "")
                )
                == run_id
            ),
            None,
        )
        self.assertIsNotNone(match, rows[:20])
        return match or {}

    def test_objective118_boundary_envelope_flows_through_capability_chains(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"front-center-objective118-{run_id}"
        label = f"objective118 target {run_id}"

        boundary = self._recompute_operator_required_boundary(scope=scope, run_id=run_id)
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)

        status, created = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective118-test",
                "reason": "objective118 capability chain create",
                "chain_name": f"objective118-{run_id}",
                "chain_type": "safe_capability_chain",
                "steps": [
                    {
                        "step_id": "scan",
                        "capability": "workspace_scan",
                        "depends_on": [],
                        "params": {"zone": scope, "label": label, "confidence": 0.95},
                    },
                    {
                        "step_id": "resolve",
                        "capability": "target_resolution",
                        "depends_on": ["scan"],
                        "params": {"target_label": label, "preferred_zone": scope},
                    },
                ],
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, created)
        chain_id = int(created.get("chain_id", 0) or 0)
        self.assertGreater(chain_id, 0, created)
        self.assertEqual(str(created.get("managed_scope", "")), scope, created)
        self.assertEqual(str(created.get("boundary_profile", "")), "operator_required", created)
        self.assertEqual(bool(created.get("approval_required", False)), True, created)
        self.assertIn(
            "boundary = operator_required",
            str(created.get("decision_basis", {}).get("why_not_automatic", "")),
            created,
        )

        status, first_advance = post_json(
            f"/workspace/capability-chains/{chain_id}/advance",
            {
                "actor": "objective118-test",
                "reason": "objective118 scan advance",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, first_advance)
        self.assertEqual(first_advance.get("status"), "active", first_advance)
        self.assertEqual(first_advance.get("last_step", {}).get("step_id"), "scan", first_advance)
        self.assertEqual(str(first_advance.get("boundary_profile", "")), "operator_required", first_advance)

        status, blocked = post_json(
            f"/workspace/capability-chains/{chain_id}/advance",
            {
                "actor": "objective118-test",
                "reason": "objective118 boundary block",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, blocked)
        self.assertEqual(blocked.get("status"), "pending_confirmation", blocked)
        self.assertEqual(
            blocked.get("last_step", {}).get("result"),
            "operator_confirmation_required",
            blocked,
        )
        self.assertEqual(str(blocked.get("boundary_profile", "")), "operator_required", blocked)
        self.assertIn(
            "boundary = operator_required",
            str(blocked.get("decision_basis", {}).get("why_not_automatic", "")),
            blocked,
        )

        status, resumed = post_json(
            f"/workspace/capability-chains/{chain_id}/advance",
            {
                "actor": "operator",
                "reason": "objective118 operator resume",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed.get("status"), "completed", resumed)
        self.assertEqual(resumed.get("last_step", {}).get("step_id"), "resolve", resumed)
        self.assertEqual(str(resumed.get("boundary_profile", "")), "operator_required", resumed)
        self.assertIn(
            "boundary = operator_required",
            str(resumed.get("decision_basis", {}).get("why_not_automatic", "")),
            resumed,
        )

        status, audit = get_json(f"/workspace/capability-chains/{chain_id}/audit")
        self.assertEqual(status, 200, audit)
        audit_entries = audit.get("audit_trail", []) if isinstance(audit.get("audit_trail", []), list) else []
        created_audit = next((item for item in audit_entries if str(item.get("event", "")) == "capability_chain_created"), None)
        blocked_audit = next((item for item in audit_entries if str(item.get("event", "")) == "capability_step_blocked_boundary_policy"), None)
        completed_audit = next((item for item in audit_entries if str(item.get("event", "")) == "capability_step_completed" and str((item.get("metadata_json", {}) if isinstance(item.get("metadata_json", {}), dict) else {}).get("step_id", "")) == "resolve"), None)
        self.assertIsNotNone(created_audit, audit_entries)
        self.assertIsNotNone(blocked_audit, audit_entries)
        self.assertIsNotNone(completed_audit, audit_entries)
        for entry in [created_audit, blocked_audit, completed_audit]:
            metadata = entry.get("metadata_json", {}) if isinstance(entry.get("metadata_json", {}), dict) else {}
            boundary_context = metadata.get("boundary_profile", {}) if isinstance(metadata.get("boundary_profile", {}), dict) else {}
            self.assertEqual(str(boundary_context.get("current_level", "")), "operator_required", entry)

        create_journal = self._find_journal_entry(
            action="workspace_capability_chain_create",
            run_id=run_id,
        )
        boundary_journal = self._find_journal_entry(
            action="workspace_capability_chain_boundary_policy",
            run_id=run_id,
        )
        advance_journal = self._find_journal_entry(
            action="workspace_capability_chain_advance",
            run_id=run_id,
        )
        for entry in [create_journal, boundary_journal, advance_journal]:
            boundary_context = entry.get("boundary_profile", {}) if isinstance(entry.get("boundary_profile", {}), dict) else {}
            self.assertEqual(str(boundary_context.get("current_level", "")), "operator_required", entry)
            self.assertIn(
                "boundary = operator_required",
                str(entry.get("decision_basis", {}).get("why_not_automatic", "")),
                entry,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)