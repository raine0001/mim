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


class Objective43HumanAwareWorkspaceBehaviorTest(unittest.TestCase):
    def _set_human_signals(self, *, actor: str, reason: str, **signals: object) -> dict:
        status, body = post_json(
            "/workspace/human-aware/signals",
            {
                "actor": actor,
                "reason": reason,
                **signals,
            },
        )
        self.assertEqual(status, 200, body)
        return body

    def test_objective43_human_aware_behavior_and_inspectability(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center-obj43-{run_id}"
        label = f"obj43-target-{run_id}"

        self._set_human_signals(
            actor="objective43-test",
            reason="reset to clear",
            human_in_workspace=False,
            human_near_target_zone=False,
            human_near_motion_path=False,
            shared_workspace_active=False,
            operator_present=False,
            occupied_zones=[],
            high_proximity_zones=[],
        )

        # Scenario 1: human enters during autonomous chain -> pause
        status, chain_pause = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective43-test",
                "reason": "pause scenario",
                "chain_name": f"obj43-pause-{run_id}",
                "steps": [
                    {
                        "step_id": "scan",
                        "capability": "workspace_scan",
                        "depends_on": [],
                        "params": {"zone": zone, "label": label, "confidence": 0.95},
                    },
                    {
                        "step_id": "memory",
                        "capability": "observation_update",
                        "depends_on": ["scan"],
                        "params": {"zone": zone, "label": label},
                    },
                ],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, chain_pause)
        pause_chain_id = int(chain_pause.get("chain_id", 0))
        self.assertGreater(pause_chain_id, 0)

        self._set_human_signals(
            actor="objective43-test",
            reason="human entered shared workspace",
            human_in_workspace=True,
            shared_workspace_active=True,
            occupied_zones=[zone],
        )

        status, paused = post_json(
            f"/workspace/capability-chains/{pause_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "expect pause",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, paused)
        self.assertEqual(paused.get("status"), "paused")
        self.assertEqual(paused.get("last_step", {}).get("result"), "paused_by_human_presence")

        # Scenario 2: human near target zone -> require confirmation for physical step
        self._set_human_signals(
            actor="objective43-test",
            reason="safe first step",
            human_in_workspace=False,
            shared_workspace_active=False,
            human_near_target_zone=False,
            occupied_zones=[],
        )

        status, chain_confirm = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective43-test",
                "reason": "confirmation scenario",
                "chain_name": f"obj43-confirm-{run_id}",
                "steps": [
                    {
                        "step_id": "scan",
                        "capability": "workspace_scan",
                        "depends_on": [],
                        "params": {"zone": zone, "label": label, "confidence": 0.93},
                    },
                    {
                        "step_id": "resolve",
                        "capability": "target_resolution",
                        "depends_on": ["scan"],
                        "params": {"target_label": label, "preferred_zone": zone},
                    },
                ],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, chain_confirm)
        confirm_chain_id = int(chain_confirm.get("chain_id", 0))
        self.assertGreater(confirm_chain_id, 0)

        status, step1 = post_json(
            f"/workspace/capability-chains/{confirm_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "execute non-physical scan",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, step1)
        self.assertEqual(step1.get("last_step", {}).get("step_id"), "scan")

        self._set_human_signals(
            actor="objective43-test",
            reason="human near target zone",
            human_in_workspace=True,
            human_near_target_zone=True,
            high_proximity_zones=[zone],
        )

        status, blocked = post_json(
            f"/workspace/capability-chains/{confirm_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "expect confirmation",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, blocked)
        self.assertEqual(blocked.get("status"), "pending_confirmation")
        self.assertEqual(blocked.get("last_step", {}).get("result"), "operator_confirmation_required")

        # Scenario 3: human leaves and workspace safe -> resume allowed
        self._set_human_signals(
            actor="objective43-test",
            reason="workspace clear",
            human_in_workspace=False,
            human_near_target_zone=False,
            human_near_motion_path=False,
            shared_workspace_active=False,
            operator_present=False,
            occupied_zones=[],
            high_proximity_zones=[],
        )

        status, resumed = post_json(
            f"/workspace/capability-chains/{confirm_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "resume after clear",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, resumed)
        self.assertEqual(resumed.get("status"), "completed")
        self.assertEqual(resumed.get("last_step", {}).get("step_id"), "resolve")
        self.assertTrue(bool(resumed.get("last_step", {}).get("verification", {}).get("target_resolution_id")))

        # Scenario 4: non-physical safe action may continue with operator present (speech suppressed)
        self._set_human_signals(
            actor="objective43-test",
            reason="operator present speech etiquette",
            human_in_workspace=False,
            operator_present=True,
            human_near_target_zone=False,
            human_near_motion_path=False,
            shared_workspace_active=False,
            occupied_zones=[],
            high_proximity_zones=[],
        )

        status, chain_speech = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective43-test",
                "reason": "speech etiquette scenario",
                "chain_name": f"obj43-speech-{run_id}",
                "steps": [
                    {
                        "step_id": "resolve",
                        "capability": "target_resolution",
                        "depends_on": [],
                        "params": {"target_label": label, "preferred_zone": zone},
                    },
                    {
                        "step_id": "speak",
                        "capability": "speech_output",
                        "depends_on": ["resolve"],
                        "params": {"message": f"Objective43 update {run_id}"},
                    },
                ],
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, chain_speech)
        speech_chain_id = int(chain_speech.get("chain_id", 0))
        self.assertGreater(speech_chain_id, 0)

        status, speech_step1 = post_json(
            f"/workspace/capability-chains/{speech_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "resolve target",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, speech_step1)
        self.assertEqual(speech_step1.get("last_step", {}).get("step_id"), "resolve")

        status, speech_step2 = post_json(
            f"/workspace/capability-chains/{speech_chain_id}/advance",
            {
                "actor": "objective43-test",
                "reason": "operator present speech handling",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, speech_step2)
        self.assertEqual(speech_step2.get("status"), "completed")
        self.assertEqual(speech_step2.get("last_step", {}).get("result"), "speech_suppressed")
        self.assertTrue(bool(speech_step2.get("last_step", {}).get("verification", {}).get("suppressed", False)))

        status, human_state = get_json("/workspace/human-aware/state")
        self.assertEqual(status, 200, human_state)
        self.assertIn("signals", human_state)
        self.assertIn(human_state.get("last_policy_decision", {}).get("outcome"), {"continue", "slow_suppress", "pause", "require_operator_confirmation", "stop_replan"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
