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


class Objective117BoundaryGovernedTaskChainsTest(unittest.TestCase):
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

    def _run_scan(self, *, scope: str, label: str, run_id: str) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective117 workspace scan {run_id}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scope,
                    "confidence_threshold": 0.6,
                    "run_id": run_id,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(event.get("execution", {}).get("execution_id", 0) or 0)
        self.assertGreater(execution_id, 0, event)

        for state in ["accepted", "running"]:
            status, updated = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, updated)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "run_id": run_id,
                    "observations": [
                        {
                            "label": label,
                            "zone": scope,
                            "confidence": 0.97,
                        }
                    ],
                },
            },
        )
        self.assertEqual(status, 200, done)

    def _recompute_operator_required_boundary(self, *, scope: str, run_id: str) -> dict:
        status, payload = post_json(
            "/autonomy/boundaries/recompute",
            {
                "actor": "objective117-test",
                "source": "objective117-boundary-governed-task-chains",
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
                    "sample_count": 21,
                    "success_rate": 0.31,
                    "escalation_rate": 0.64,
                    "retry_rate": 0.42,
                    "interruption_rate": 0.34,
                    "memory_delta_rate": 0.2,
                    "override_rate": 0.48,
                    "replan_rate": 0.37,
                    "environment_stability": 0.22,
                    "development_confidence": 0.35,
                    "constraint_reliability": 0.41,
                    "experiment_confidence": 0.27,
                },
                "metadata_json": {"run_id": run_id, "objective": "117"},
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

    def test_objective117_boundary_envelope_flows_through_autonomous_chains(self) -> None:
        run_id = uuid4().hex[:8]
        scope = f"front-center-objective117-{run_id}"
        label = f"objective117 target {run_id}"

        self._register_workspace_scan()
        boundary = self._recompute_operator_required_boundary(scope=scope, run_id=run_id)
        self.assertEqual(str(boundary.get("current_level", "")), "operator_required", boundary)
        self._run_scan(scope=scope, label=label, run_id=run_id)

        status, proposals = get_json("/workspace/proposals")
        self.assertEqual(status, 200, proposals)
        scoped_proposals = [
            item
            for item in proposals.get("proposals", [])
            if isinstance(item, dict)
            and str(item.get("related_zone", "")) == scope
            and int(item.get("proposal_id", 0) or 0) > 0
        ]
        self.assertTrue(scoped_proposals, proposals)
        proposal_id = int(scoped_proposals[0]["proposal_id"])

        status, created = post_json(
            "/workspace/chains",
            {
                "actor": "objective117-test",
                "reason": "objective117 chain create",
                "chain_type": "proposal_sequence",
                "proposal_ids": [proposal_id],
                "source": "objective117-test",
                "requires_approval": False,
                "cooldown_seconds": 0,
                "stop_on_failure": True,
                "metadata_json": {"run_id": run_id, "managed_scope": scope},
            },
        )
        self.assertEqual(status, 200, created)
        chain_id = int(created.get("chain_id", 0) or 0)
        self.assertGreater(chain_id, 0, created)
        self.assertEqual(created.get("status"), "pending_approval", created)
        self.assertEqual(bool(created.get("requires_approval", False)), True, created)
        self.assertEqual(bool(created.get("approval_required", False)), True, created)
        self.assertEqual(str(created.get("managed_scope", "")), scope, created)
        self.assertEqual(str(created.get("boundary_profile", "")), "operator_required", created)
        self.assertIn(
            "boundary = operator_required",
            str(created.get("decision_basis", {}).get("why_not_automatic", "")),
            created,
        )

        status, fetched = get_json(f"/workspace/chains/{chain_id}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(str(fetched.get("boundary_profile", "")), "operator_required", fetched)
        self.assertEqual(bool(fetched.get("requires_approval", False)), True, fetched)

        status, blocked = post_json(
            f"/workspace/chains/{chain_id}/advance",
            {
                "actor": "objective117-test",
                "reason": "advance before approval",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 422, blocked)

        status, approved = post_json(
            f"/workspace/chains/{chain_id}/approve",
            {
                "actor": "operator",
                "reason": "objective117 approval",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved.get("status"), "active", approved)
        self.assertEqual(str(approved.get("boundary_profile", "")), "operator_required", approved)
        self.assertIn(
            "boundary = operator_required",
            str(approved.get("decision_basis", {}).get("why_not_automatic", "")),
            approved,
        )

        status, advanced = post_json(
            f"/workspace/chains/{chain_id}/advance",
            {
                "actor": "objective117-test",
                "reason": "objective117 advance",
                "force": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, advanced)
        self.assertEqual(advanced.get("status"), "completed", advanced)
        self.assertEqual(str(advanced.get("boundary_profile", "")), "operator_required", advanced)
        self.assertIn(
            "boundary = operator_required",
            str(advanced.get("decision_basis", {}).get("why_not_automatic", "")),
            advanced,
        )

        status, audit = get_json(f"/workspace/chains/{chain_id}/audit")
        self.assertEqual(status, 200, audit)
        audit_entries = audit.get("audit_trail", []) if isinstance(audit.get("audit_trail", []), list) else []
        created_audit = next((item for item in audit_entries if str(item.get("event", "")) == "chain_created"), None)
        approved_audit = next((item for item in audit_entries if str(item.get("event", "")) == "chain_approved"), None)
        advanced_audit = next((item for item in audit_entries if str(item.get("event", "")) == "chain_advanced"), None)
        self.assertIsNotNone(created_audit, audit_entries)
        self.assertIsNotNone(approved_audit, audit_entries)
        self.assertIsNotNone(advanced_audit, audit_entries)
        for entry in [created_audit, approved_audit, advanced_audit]:
            metadata = entry.get("metadata_json", {}) if isinstance(entry.get("metadata_json", {}), dict) else {}
            boundary_context = metadata.get("boundary_profile", {}) if isinstance(metadata.get("boundary_profile", {}), dict) else {}
            self.assertEqual(str(boundary_context.get("current_level", "")), "operator_required", entry)

        create_journal = self._find_journal_entry(
            action="workspace_autonomous_chain_create",
            run_id=run_id,
        )
        approve_journal = self._find_journal_entry(
            action="workspace_autonomous_chain_approve",
            run_id=run_id,
        )
        advance_journal = self._find_journal_entry(
            action="workspace_autonomous_chain_advance",
            run_id=run_id,
        )
        for entry in [create_journal, approve_journal, advance_journal]:
            boundary_context = entry.get("boundary_profile", {}) if isinstance(entry.get("boundary_profile", {}), dict) else {}
            self.assertEqual(str(boundary_context.get("current_level", "")), "operator_required", entry)
            self.assertIn(
                "boundary = operator_required",
                str(entry.get("decision_basis", {}).get("why_not_automatic", "")),
                entry,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)