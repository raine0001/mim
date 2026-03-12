import json
import os
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
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


class Objective52ConceptPatternMemoryTest(unittest.TestCase):
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

    def _create_scan_success(self, *, zone: str, run_id: str, index: int) -> None:
        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"objective52 scan {run_id}-{index}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.97,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.6,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int((event.get("execution", {}) or {}).get("execution_id", 0))
        self.assertGreater(execution_id, 0)

        for state in ["accepted", "running"]:
            status, _ = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {
                    "status": state,
                    "reason": state,
                    "actor": "tod",
                    "feedback_json": {},
                },
            )
            self.assertEqual(status, 200)

        status, done = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": [
                        {
                            "label": f"obj52-zone-pattern-{run_id}",
                            "zone": zone,
                            "confidence": 0.94,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ]
                },
            },
        )
        self.assertEqual(status, 200, done)

    def test_objective52_concept_extraction_and_influence(self) -> None:
        run_id = uuid4().hex[:8]
        seed_zone = f"front-left-obj52-{run_id}"

        self._register_workspace_scan()
        self._create_scan_success(zone=seed_zone, run_id=run_id, index=1)
        self._create_scan_success(zone=seed_zone, run_id=run_id, index=2)

        status, extracted = post_json(
            "/memory/concepts/extract",
            {
                "actor": "objective52-test",
                "source": "objective52-focused",
                "lookback_hours": 24,
                "min_evidence_count": 2,
                "max_concepts": 10,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, extracted)
        concepts = extracted.get("concepts", []) if isinstance(extracted.get("concepts", []), list) else []
        concept = next(
            (
                item
                for item in concepts
                if isinstance(item, dict)
                and item.get("concept_type") == "rescan_success_zone_pattern"
            ),
            None,
        )
        self.assertIsNotNone(concept, concepts)

        affected_zones = (concept or {}).get("affected_zones", []) if isinstance((concept or {}).get("affected_zones", []), list) else []
        zone = str(affected_zones[0]).strip() if affected_zones else "workspace"

        concept_id = int((concept or {}).get("concept_id", 0))
        self.assertGreater(concept_id, 0)
        before_evidence = int((concept or {}).get("evidence_count", 0))
        before_confidence = float((concept or {}).get("confidence", 0.0))
        self.assertGreaterEqual(before_evidence, 2)

        status, listed = get_json("/memory/concepts?limit=100")
        self.assertEqual(status, 200, listed)
        rows = listed.get("concepts", []) if isinstance(listed, dict) else []
        self.assertTrue(any(int(item.get("concept_id", 0)) == concept_id for item in rows if isinstance(item, dict)))

        status, detail = get_json(f"/memory/concepts/{concept_id}")
        self.assertEqual(status, 200, detail)
        detail_concept = detail.get("concept", {}) if isinstance(detail, dict) else {}
        self.assertEqual(int(detail_concept.get("concept_id", 0)), concept_id)
        self.assertTrue(bool(detail_concept.get("evidence_summary", "")))

        self._create_scan_success(zone=zone, run_id=run_id, index=3)
        status, extracted2 = post_json(
            "/memory/concepts/extract",
            {
                "actor": "objective52-test",
                "source": "objective52-focused",
                "lookback_hours": 24,
                "min_evidence_count": 2,
                "max_concepts": 10,
                "metadata_json": {"run_id": run_id, "phase": "increment"},
            },
        )
        self.assertEqual(status, 200, extracted2)

        status, detail2 = get_json(f"/memory/concepts/{concept_id}")
        self.assertEqual(status, 200, detail2)
        concept2 = detail2.get("concept", {}) if isinstance(detail2, dict) else {}
        self.assertGreater(int(concept2.get("evidence_count", 0)), before_evidence)
        self.assertGreaterEqual(float(concept2.get("confidence", 0.0)), before_confidence)

        status, acked = post_json(
            f"/memory/concepts/{concept_id}/acknowledge",
            {
                "actor": "operator",
                "reason": "validated concept usefulness",
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, acked)
        ack_concept = acked.get("concept", {}) if isinstance(acked, dict) else {}
        self.assertEqual(str(ack_concept.get("status", "")), "acknowledged")

        status, plan = post_json(
            "/planning/horizon/plans",
            {
                "actor": "objective52-test",
                "source": "objective52-focused",
                "planning_horizon_minutes": 90,
                "goal_candidates": [
                    {
                        "goal_key": f"refresh:{zone}",
                        "title": "Refresh concept-influenced zone",
                        "priority": "normal",
                        "goal_type": "workspace_refresh",
                        "dependencies": [],
                        "estimated_steps": 2,
                        "expected_value": 0.5,
                        "urgency": 0.5,
                        "is_physical": False,
                        "metadata_json": {"scope": zone, "run_id": run_id},
                    }
                ],
                "priority_policy": {
                    "map_freshness_limit_seconds": 900,
                    "min_target_confidence": 0.75,
                },
                "map_freshness_seconds": 300,
                "object_confidence": 0.9,
                "human_aware_state": {
                    "human_in_workspace": False,
                    "shared_workspace_active": False,
                },
                "operator_preferences": {},
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, plan)
        ranked = plan.get("ranked_goals", []) if isinstance(plan.get("ranked_goals", []), list) else []
        self.assertGreaterEqual(len(ranked), 1)

        goal = ranked[0] if ranked else {}
        concept_influence = goal.get("concept_influence", {}) if isinstance(goal.get("concept_influence", {}), dict) else {}
        self.assertTrue(bool(concept_influence.get("applied", False)))
        self.assertGreaterEqual(len(concept_influence.get("concept_ids", [])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
