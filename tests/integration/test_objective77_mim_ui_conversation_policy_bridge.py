import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid

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


def get_text(path: str) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.status, resp.read().decode("utf-8")


class Objective77MimUiConversationPolicyBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 77",
            base_url=BASE_URL,
            require_mim=True,
            require_ui_state=True,
        )

    def _run_workspace_scan(self, zone: str, observations: list[dict]) -> int:
        status, _ = post_json(
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
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace {zone}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": zone,
                    "confidence_threshold": 0.65,
                },
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = event["execution"]["execution_id"]
        for step in ["accepted", "running"]:
            status, step_resp = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                {"status": step, "reason": step, "actor": "tod", "feedback_json": {}},
            )
            self.assertEqual(status, 200, step_resp)

        status, succeeded = post_json(
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": "succeeded",
                "reason": "scan complete",
                "actor": "tod",
                "feedback_json": {
                    "observations": observations,
                    "observation_confidence": 0.9,
                },
            },
        )
        self.assertEqual(status, 200, succeeded)
        return execution_id

    def test_mim_page_contains_object_memory_panel_hooks(self) -> None:
        status, html = get_text("/mim")
        self.assertEqual(status, 200)
        self.assertIn('id="objectMemoryPanel"', html)
        self.assertIn('id="objectMemoryList"', html)
        self.assertIn(
            "function renderObjectMemoryPanel(conversationContext = {})", html
        )
        self.assertIn("function objectMemorySortRank(details = {})", html)

    def test_state_exposes_profile_and_avoids_repeated_clarifier_loop(self) -> None:
        first_transcript = "do something with that thing over there"
        second_transcript = "still do it around there now"

        status, turn1 = post_json(
            "/gateway/voice/input",
            {
                "transcript": first_transcript,
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
            },
        )
        self.assertEqual(status, 200, turn1)
        res1 = turn1["resolution"]
        prompt1 = str(res1.get("clarification_prompt", "")).strip()
        self.assertTrue(prompt1)
        prompt1_l = prompt1.lower()
        self.assertTrue(
            ("missing one detail" in prompt1_l) or ("options: 1)" in prompt1_l),
            prompt1,
        )

        # Mirror front-end behavior where spoken clarifiers are queued as speech output actions.
        status, output1 = post_json(
            "/gateway/voice/output",
            {
                "message": prompt1,
                "voice_profile": "assistant",
                "priority": "normal",
                "channel": "speaker",
            },
        )
        self.assertEqual(status, 200, output1)

        status, turn2 = post_json(
            "/gateway/voice/input",
            {
                "transcript": second_transcript,
                "parsed_intent": "execute_capability",
                "confidence": 0.72,
            },
        )
        self.assertEqual(status, 200, turn2)
        res2 = turn2["resolution"]
        prompt2 = str(res2.get("clarification_prompt", "")).strip()
        self.assertTrue(prompt2)
        prompt2_l = prompt2.lower()
        self.assertIn("options: 1)", prompt2_l)
        self.assertNotIn("do you want me to answer a question", prompt2_l)
        if "missing one detail" in prompt1_l:
            self.assertNotEqual(prompt1, prompt2)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(
            str(state.get("conversation_policy_profile", "")), "tightened_v1"
        )
        self.assertTrue(str(state.get("runtime_build", "")).strip())
        features = state.get("runtime_features", [])
        self.assertIsInstance(features, list)
        self.assertIn("voice_listen_hint", features)

        inquiry_prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        self.assertTrue(inquiry_prompt)
        self.assertIn("options: 1)", inquiry_prompt)
        self.assertNotIn("do you want me to answer a question", inquiry_prompt)

    def test_state_surfaces_voice_hint_for_no_wake_heartbeat(self) -> None:
        observed_wake_hint = ""
        for _ in range(4):
            status, mic_event = post_json(
                "/gateway/perception/mic/events",
                {
                    "device_id": "mim-ui-mic",
                    "source_type": "microphone",
                    "session_id": "mim-ui-session",
                    "is_remote": False,
                    "transcript": "",
                    "confidence": 0.73,
                    "min_interval_seconds": 0,
                    "duplicate_window_seconds": 2,
                    "transcript_confidence_floor": 0.2,
                    "discard_low_confidence": False,
                    "metadata_json": {
                        "source": "mim_ui_sketch",
                        "mode": "always_listening_heartbeat_no_wake",
                    },
                },
            )
            self.assertEqual(status, 200, mic_event)
            self.assertEqual(
                str(mic_event.get("status", "")), "heartbeat_no_transcript"
            )

            status, state = get_json("/mim/ui/state")
            self.assertEqual(status, 200, state)
            hint = str(state.get("voice_listen_hint", "")).strip().lower()
            if "wake word" in hint and "mim" in hint:
                observed_wake_hint = hint
                break
            time.sleep(0.2)

        self.assertTrue(observed_wake_hint)

    def test_state_greets_known_person_visible_on_camera(self) -> None:
        session_id = f"objective77-known-{uuid.uuid4()}"

        status, learned = post_json(
            "/gateway/intake/text",
            {
                "text": "call me Jordan",
                "parsed_intent": "discussion",
                "confidence": 0.93,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "user_id": "operator",
                },
            },
        )
        self.assertEqual(status, 200, learned)

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-known-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-known-person"},
                "observations": [
                    {
                        "object_label": "Jordan",
                        "confidence": 0.91,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertIn("jordan", prompt)
        self.assertTrue(
            ("good to see you" in prompt) or ("recognize you on camera" in prompt),
            prompt,
        )
        self.assertEqual(str(context.get("recognized_person", "")).strip(), "Jordan")

    def test_state_exposes_dual_camera_scene_context(self) -> None:
        session_id = f"objective77-dual-{uuid.uuid4()}"
        label = f"mystery-widget-{uuid.uuid4().hex[:6]}"

        status, learned = post_json(
            "/gateway/intake/text",
            {
                "text": "call me Jordan",
                "parsed_intent": "discussion",
                "confidence": 0.93,
                "metadata_json": {
                    "conversation_session_id": session_id,
                    "user_id": "operator",
                },
            },
        )
        self.assertEqual(status, 200, learned)

        status, camera_one = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-front-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-dual-front"},
                "observations": [
                    {
                        "object_label": "Jordan",
                        "confidence": 0.93,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera_one)

        status, camera_two = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-side-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-dual-side"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.81,
                        "zone": "side-left",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera_two)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertIn("jordan", prompt)
        self.assertEqual(int(state.get("camera_source_count", 0)), 2)
        self.assertIn(
            "2 camera feeds", str(state.get("camera_scene_summary", "")).lower()
        )
        self.assertIn("jordan", str(context.get("camera_scene_summary", "")).lower())
        self.assertIn(
            label.lower(), str(context.get("camera_scene_summary", "")).lower()
        )
        self.assertEqual(str(context.get("recognized_person", "")).strip(), "Jordan")
        self.assertIn("Jordan", context.get("recognized_people", []))
        self.assertIn(label, context.get("unknown_camera_labels", []))

    def test_state_is_curious_about_unknown_camera_object(self) -> None:
        session_id = f"objective77-unknown-{uuid.uuid4()}"
        label = f"mystery-widget-{uuid.uuid4().hex[:6]}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-unknown-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-unknown-object"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.88,
                        "zone": "front-right",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )
        details = (context.get("camera_object_details", {}) or {}).get(label, {})

        self.assertIn(label.lower(), prompt)
        self.assertIn(f"what is {label.lower()}", prompt)
        self.assertIn(f"what does {label.lower()} do", prompt)
        self.assertIn("explain more if needed", prompt)
        self.assertEqual(str(context.get("unknown_camera_label", "")).strip(), label)
        self.assertEqual(str(details.get("state", "")).strip(), "novel")
        self.assertEqual(
            details.get("inquiry_questions", []),
            [
                f"What is {label}?",
                f"What does {label} do?",
                "Explain more if needed.",
            ],
        )

    def test_state_uses_persistent_object_memory_to_avoid_reasking_about_known_object(
        self,
    ) -> None:
        session_id = f"objective77-known-object-{uuid.uuid4()}"
        label = f"stable-widget-{uuid.uuid4().hex[:6]}"
        zone = f"front-left-{uuid.uuid4().hex[:6]}"

        self._run_workspace_scan(
            zone,
            [{"label": label, "zone": zone, "confidence": 0.93}],
        )

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-known-object-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-known-object"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.89,
                        "zone": zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertEqual(str(context.get("unknown_camera_label", "")).strip(), "")
        self.assertIn(label, context.get("known_camera_objects", []))
        self.assertEqual(
            str((context.get("camera_object_states", {}) or {}).get(label, "")), "known"
        )
        self.assertNotIn("what should i know", prompt)
        self.assertNotIn("want to learn", prompt)
        self.assertNotIn("what should i understand about it", prompt)

    def test_state_asks_for_confirmation_when_known_object_moves(self) -> None:
        session_id = f"objective77-moved-object-{uuid.uuid4()}"
        label = f"mobile-widget-{uuid.uuid4().hex[:6]}"
        original_zone = f"front-left-{uuid.uuid4().hex[:6]}"
        moved_zone = f"side-right-{uuid.uuid4().hex[:6]}"

        self._run_workspace_scan(
            original_zone,
            [{"label": label, "zone": original_zone, "confidence": 0.94}],
        )

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-moved-object-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-moved-object"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.9,
                        "zone": moved_zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertEqual(str(context.get("uncertain_camera_label", "")).strip(), label)
        self.assertIn(label, context.get("uncertain_camera_objects", []))
        self.assertEqual(
            str((context.get("camera_object_states", {}) or {}).get(label, "")),
            "uncertain",
        )
        self.assertIn(label.lower(), prompt)
        self.assertTrue(
            ("confirm" in prompt)
            or ("intentional" in prompt)
            or ("seems to have moved" in prompt),
            prompt,
        )
        self.assertNotIn("what should i know", prompt)
        self.assertNotIn("want to learn", prompt)

    def test_state_asks_where_known_object_went_when_missing_from_camera_view(
        self,
    ) -> None:
        session_id = f"objective77-missing-object-{uuid.uuid4()}"
        label = f"anchor-widget-{uuid.uuid4().hex[:6]}"
        zone = f"front-shelf-{uuid.uuid4().hex[:6]}"
        filler_label = f"filler-item-{uuid.uuid4().hex[:6]}"

        self._run_workspace_scan(
            zone,
            [
                {
                    "label": label,
                    "zone": zone,
                    "confidence": 0.95,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                    "expected_home_zone": zone,
                }
            ],
        )

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-missing-object-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-missing-object"},
                "observations": [
                    {
                        "object_label": filler_label,
                        "confidence": 0.88,
                        "zone": zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertEqual(str(context.get("missing_camera_label", "")).strip(), label)
        self.assertIn(label, context.get("missing_camera_objects", []))
        self.assertEqual(
            str((context.get("camera_object_states", {}) or {}).get(label, "")),
            "missing",
        )
        details = (context.get("camera_object_details", {}) or {}).get(label, {})
        self.assertEqual(str(details.get("state", "")).strip(), "missing")
        self.assertIn("Jordan", str(details.get("semantic_note", "")))
        self.assertIn(label.lower(), prompt)
        self.assertIn("jordan", prompt)
        self.assertIn("charging the handheld scanner", prompt)
        self.assertTrue(
            ("where did it go" in prompt)
            or ("did it get moved" in prompt)
            or ("cannot find" in prompt),
            prompt,
        )
        self.assertNotIn("what should i know", prompt)
        self.assertNotIn("want to learn", prompt)

    def test_state_uses_semantic_context_when_known_object_moves(self) -> None:
        session_id = f"objective77-moved-semantic-{uuid.uuid4()}"
        label = f"dock-widget-{uuid.uuid4().hex[:6]}"
        original_zone = f"front-bench-{uuid.uuid4().hex[:6]}"
        moved_zone = f"rear-bench-{uuid.uuid4().hex[:6]}"

        self._run_workspace_scan(
            original_zone,
            [
                {
                    "label": label,
                    "zone": original_zone,
                    "confidence": 0.94,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                }
            ],
        )

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-moved-semantic-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-moved-semantic"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.89,
                        "zone": moved_zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        prompt = str(state.get("inquiry_prompt", "")).strip().lower()
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )

        self.assertIn(label.lower(), prompt)
        self.assertIn("jordan", prompt)
        self.assertIn("charging the handheld scanner", prompt)
        details = (context.get("camera_object_details", {}) or {}).get(label, {})
        self.assertEqual(str(details.get("state", "")).strip(), "uncertain")
        self.assertIn("Jordan", str(details.get("semantic_note", "")))

    def test_state_exposes_explicit_semantic_fields_for_known_camera_object(
        self,
    ) -> None:
        session_id = f"objective77-semantic-fields-{uuid.uuid4()}"
        label = f"semantic-widget-{uuid.uuid4().hex[:6]}"
        zone = f"charging-shelf-{uuid.uuid4().hex[:6]}"

        self._run_workspace_scan(
            zone,
            [
                {
                    "label": label,
                    "zone": zone,
                    "confidence": 0.95,
                    "description": "a dock charger",
                    "purpose": "charging the handheld scanner",
                    "owner": "Jordan",
                    "category": "charging equipment",
                    "meaning": "the scanner is ready for handoff",
                    "user_notes": "the cable is loose",
                    "expected_home_zone": zone,
                }
            ],
        )

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-semantic-fields-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective77-semantic-fields"},
                "observations": [
                    {
                        "object_label": label,
                        "confidence": 0.9,
                        "zone": zone,
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, state = get_json("/mim/ui/state")
        self.assertEqual(status, 200, state)
        context = (
            state.get("conversation_context", {}) if isinstance(state, dict) else {}
        )
        details = (context.get("camera_object_details", {}) or {}).get(label, {})
        semantic_fields = details.get("semantic_fields", [])
        semantic_memory = details.get("semantic_memory", {})

        self.assertEqual(str(details.get("state", "")).strip(), "known")
        self.assertIn("description", semantic_fields)
        self.assertIn("purpose", semantic_fields)
        self.assertIn("owner", semantic_fields)
        self.assertIn("category", semantic_fields)
        self.assertIn("meaning", semantic_fields)
        self.assertIn("user_notes", semantic_fields)
        self.assertEqual(
            str(semantic_memory.get("description", "")).strip(), "a dock charger"
        )
        self.assertEqual(
            str(semantic_memory.get("purpose", "")).strip(),
            "charging the handheld scanner",
        )
        self.assertEqual(str(semantic_memory.get("owner", "")).strip(), "Jordan")
        self.assertEqual(
            str(semantic_memory.get("category", "")).strip(), "charging equipment"
        )
        self.assertEqual(
            str(semantic_memory.get("meaning", "")).strip(),
            "the scanner is ready for handoff",
        )
        self.assertEqual(
            str(semantic_memory.get("user_notes", "")).strip(), "the cable is loose"
        )
        self.assertEqual(str(details.get("expected_home_zone", "")).strip(), zone)


if __name__ == "__main__":
    unittest.main(verbosity=2)
