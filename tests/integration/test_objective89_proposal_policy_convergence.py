import asyncio
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg

from tests.integration.operator_resolution_test_utils import objective85_database_url
from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


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
        return exc.code, json.loads(body) if body else {}


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
            return exc.code, {"detail": body}


def get_workspace_proposal(proposal_id: int) -> dict:
    status, payload = get_json("/workspace/proposals", {"limit": 200})
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError({"status": status, "payload": payload})
    proposals = payload.get("proposals", []) if isinstance(payload.get("proposals", []), list) else []
    for proposal in proposals:
        if isinstance(proposal, dict) and int(proposal.get("proposal_id", 0) or 0) == int(proposal_id):
            return proposal
    raise AssertionError({"proposal_id": proposal_id, "available_ids": [item.get("proposal_id") for item in proposals if isinstance(item, dict)]})


def list_workspace_proposals(*, limit: int = 200) -> list[dict]:
    status, payload = get_json("/workspace/proposals", {"limit": limit})
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError({"status": status, "payload": payload})
    proposals = payload.get("proposals", []) if isinstance(payload.get("proposals", []), list) else []
    return [item for item in proposals if isinstance(item, dict)]


def cleanup_objective89_rows() -> None:
    asyncio.run(_cleanup_objective89_rows_async())


async def _cleanup_objective89_rows_async() -> None:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM workspace_proposal_policy_preference_profiles")
        await conn.execute(
            "DELETE FROM workspace_proposal_arbitration_outcomes WHERE source = 'objective89'"
        )
        await conn.execute("DELETE FROM workspace_proposals WHERE source = 'objective89'")
    finally:
        await conn.close()


def seed_workspace_proposal(*, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int) -> int:
    return asyncio.run(
        _seed_workspace_proposal_async(
            run_id=run_id,
            proposal_type=proposal_type,
            related_zone=related_zone,
            confidence=confidence,
            age_seconds=age_seconds,
        )
    )


async def _seed_workspace_proposal_async(*, run_id: str, proposal_type: str, related_zone: str, confidence: float, age_seconds: int) -> int:
    dsn = objective85_database_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        created_at = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(age_seconds)))
        row = await conn.fetchrow(
            """
            INSERT INTO workspace_proposals (
                proposal_type, title, description, status, confidence, priority_score,
                priority_reason, source, related_zone, related_object_id, source_execution_id,
                trigger_json, metadata_json, created_at
            ) VALUES (
                $1, $2, $3, 'pending', $4, 0.0,
                '', 'objective89', $5, NULL, NULL,
                $6::jsonb, $7::jsonb, $8
            )
            RETURNING id
            """,
            str(proposal_type),
            f"objective89 {proposal_type} {run_id}",
            f"seeded proposal {proposal_type} for objective89 {run_id}",
            float(confidence),
            str(related_zone),
            json.dumps({"run_id": run_id}),
            json.dumps({"run_id": run_id}),
            created_at,
        )
        return int(row["id"])
    finally:
        await conn.close()


class Objective89ProposalPolicyConvergenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 89",
            base_url=BASE_URL,
            require_ui_state=True,
        )
        cleanup_objective89_rows()

    def setUp(self) -> None:
        cleanup_objective89_rows()

    def tearDown(self) -> None:
        cleanup_objective89_rows()

    def test_repeated_losses_converge_to_bounded_suppression_before_selection(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"objective89-zone-{run_id}"
        losing_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="rescan_zone",
            related_zone=zone,
            confidence=0.84,
            age_seconds=15,
        )
        winning_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="confirm_target_ready",
            related_zone=zone,
            confidence=0.73,
            age_seconds=20,
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective89",
                    "proposal_id": losing_id,
                    "arbitration_decision": "lost",
                    "arbitration_posture": "isolate",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "rejected",
                    "reason": "rescan repeatedly lost arbitration in this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective89",
                    "proposal_id": winning_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "confirm target repeatedly wins arbitration in this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        losing_payload = get_workspace_proposal(losing_id)
        winning_payload = get_workspace_proposal(winning_id)

        losing_policy = (
            losing_payload.get("proposal_policy_convergence", {})
            if isinstance(losing_payload.get("proposal_policy_convergence", {}), dict)
            else {}
        )
        winning_policy = (
            winning_payload.get("proposal_policy_convergence", {})
            if isinstance(winning_payload.get("proposal_policy_convergence", {}), dict)
            else {}
        )

        self.assertEqual(str(losing_policy.get("policy_state", "")), "suppressed", losing_policy)
        self.assertTrue(bool(losing_policy.get("suppression_threshold_met", False)), losing_policy)
        self.assertTrue(
            bool(
                (losing_policy.get("policy_effects_json", {}) if isinstance(losing_policy.get("policy_effects_json", {}), dict) else {}).get(
                    "suppress_before_arbitration", False
                )
            ),
            losing_policy,
        )
        self.assertIn(
            "Repeated arbitration losses",
            str((losing_policy.get("policy_effects_json", {}) if isinstance(losing_policy.get("policy_effects_json", {}), dict) else {}).get("why_this_proposal_was_deprioritized_before_emission", "")),
        )
        self.assertEqual(str(winning_policy.get("policy_state", "")), "preferred", winning_policy)
        self.assertGreater(
            float(winning_payload.get("priority_score", 0.0) or 0.0),
            float(losing_payload.get("priority_score", 0.0) or 0.0),
            {"losing": losing_payload, "winning": winning_payload},
        )

        ordered = [
            proposal
            for proposal in list_workspace_proposals(limit=200)
            if int(proposal.get("proposal_id", 0) or 0) in {losing_id, winning_id}
        ]
        self.assertEqual(len(ordered), 2, ordered)
        self.assertEqual(int(ordered[0].get("proposal_id", 0) or 0), winning_id, ordered)
        self.assertEqual(int(ordered[1].get("proposal_id", 0) or 0), losing_id, ordered)

        status, preferences = get_json(
            "/workspace/proposals/policy-preferences",
            {"related_zone": zone},
        )
        self.assertEqual(status, 200, preferences)
        rows = preferences.get("preferences", []) if isinstance(preferences, dict) else []
        by_type = {str(item.get("proposal_type", "")): item for item in rows if isinstance(item, dict)}
        self.assertEqual(str((by_type.get("rescan_zone") or {}).get("policy_state", "")), "suppressed", rows)
        self.assertEqual(str((by_type.get("confirm_target_ready") or {}).get("policy_state", "")), "preferred", rows)

        status, ui_state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, ui_state)
        operator_reasoning = ui_state.get("operator_reasoning", {}) if isinstance(ui_state, dict) else {}
        proposal_policy = (
            operator_reasoning.get("proposal_policy", {})
            if isinstance(operator_reasoning.get("proposal_policy", {}), dict)
            else {}
        )
        self.assertIn("proposal_policy", operator_reasoning, operator_reasoning)
        self.assertIn("active_policy_count", proposal_policy, proposal_policy)
        self.assertIn("items", proposal_policy, proposal_policy)

    def test_contradictory_fresh_evidence_reopens_existing_policy(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"objective89-zone-{run_id}"
        proposal_id = seed_workspace_proposal(
            run_id=run_id,
            proposal_type="rescan_zone",
            related_zone=zone,
            confidence=0.8,
            age_seconds=10,
        )

        for _ in range(4):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective89",
                    "proposal_id": proposal_id,
                    "arbitration_decision": "lost",
                    "arbitration_posture": "isolate",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "rejected",
                    "reason": "rescan repeatedly lost arbitration in this scope",
                    "metadata_json": {"run_id": run_id},
                },
            )
            self.assertEqual(status, 200, outcome)

        for _ in range(2):
            status, outcome = post_json(
                "/workspace/proposals/arbitration-outcomes",
                {
                    "actor": "tod",
                    "source": "objective89",
                    "proposal_id": proposal_id,
                    "arbitration_decision": "won",
                    "arbitration_posture": "merge",
                    "trust_chain_status": "verified",
                    "downstream_execution_outcome": "accepted",
                    "reason": "fresh evidence says rescan is now aligned",
                    "metadata_json": {"run_id": run_id, "fresh": True},
                },
            )
            self.assertEqual(status, 200, outcome)

        proposal_payload = get_workspace_proposal(proposal_id)
        policy = (
            proposal_payload.get("proposal_policy_convergence", {})
            if isinstance(proposal_payload.get("proposal_policy_convergence", {}), dict)
            else {}
        )
        self.assertEqual(str(policy.get("policy_state", "")), "reopened", policy)
        self.assertTrue(bool(policy.get("contradictory_recent_signal", False)), policy)
        effects = policy.get("policy_effects_json", {}) if isinstance(policy.get("policy_effects_json", {}), dict) else {}
        self.assertFalse(bool(effects.get("suppress_before_arbitration", False)), effects)
        self.assertEqual(float(effects.get("priority_delta", 0.0) or 0.0), 0.0, effects)


if __name__ == "__main__":
    unittest.main()