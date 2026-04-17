import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid


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
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, dict) else {"data": parsed}


def get_json(path: str, query: dict | None = None) -> tuple[int, dict | list]:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed if isinstance(parsed, (dict, list)) else {
            "data": parsed
        }


class Objective78ConversationIntakeOverrideTest(unittest.TestCase):
    def _post_session_turn(
        self, session_id: str, text: str, parsed_intent: str = "discussion"
    ) -> tuple[int, dict]:
        return post_json(
            "/gateway/intake/text",
            {
                "text": text,
                "parsed_intent": parsed_intent,
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )

    def _post_camera_event(
        self,
        *,
        session_id: str,
        device_suffix: str,
        observations: list[dict],
    ) -> tuple[int, dict]:
        return post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-{device_suffix}-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": f"objective78-{device_suffix}"},
                "observations": observations,
            },
        )

    def _run_workspace_scan(self, scan_area: str, observations: list[dict]) -> int:
        status, capability = post_json(
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
        self.assertEqual(status, 200, capability)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": f"scan workspace {scan_area}",
                "parsed_intent": "observe_workspace",
                "confidence": 0.95,
                "metadata_json": {
                    "scan_mode": "full",
                    "scan_area": scan_area,
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

    def test_conversation_intent_routes_to_conversation_layer_without_execution(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "How are you doing today?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )

        self.assertEqual(str(resolution.get("outcome", "")), "store_only")
        self.assertEqual(str(resolution.get("safety_decision", "")), "store_only")
        self.assertEqual(str(resolution.get("reason", "")), "conversation_override")
        self.assertEqual(
            str(metadata.get("route_preference", "")), "conversation_layer"
        )
        self.assertTrue(bool(metadata.get("conversation_override")))
        self.assertTrue(str(resolution.get("clarification_prompt", "")).strip())
        self.assertFalse("execution" in payload)

    def test_conversation_with_action_gets_optional_escalation(self) -> None:
        session_id = f"objective78-action-{uuid.uuid4()}"
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        escalation = str(metadata.get("optional_escalation", ""))

        self.assertEqual(str(resolution.get("outcome", "")), "requires_confirmation")
        self.assertEqual(
            str(resolution.get("safety_decision", "")), "requires_confirmation"
        )
        self.assertEqual(
            str(resolution.get("reason", "")), "conversation_optional_escalation"
        )
        self.assertIn("create goal", escalation.lower())
        self.assertIn(
            "create goal", str(resolution.get("clarification_prompt", "")).lower()
        )
        self.assertFalse("execution" in payload)

    def test_explicit_action_request_keeps_goal_routing(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Execute workspace scan now",
                "parsed_intent": "unknown",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )

        self.assertEqual(str(metadata.get("route_preference", "")), "goal_system")
        self.assertFalse(bool(metadata.get("conversation_override")))
        self.assertTrue("execution" in payload)

    def test_repeated_conversation_action_prompt_uses_options_fallback(self) -> None:
        session_id = f"objective78-repeat-{uuid.uuid4()}"
        first_status, first_payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(first_status, 200, first_payload)
        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        self.assertEqual(
            str(first_resolution.get("reason", "")), "conversation_optional_escalation"
        )

        second_status, second_payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Can you execute a workspace scan now?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = str(second_resolution.get("clarification_prompt", "")).lower()

        self.assertEqual(
            str(second_resolution.get("reason", "")),
            "conversation_optional_escalation_followup",
        )
        self.assertIn(
            "clarification_limit_reached",
            second_resolution.get("escalation_reasons", []),
        )
        self.assertIn("options: 1)", second_prompt)
        self.assertFalse("execution" in second_payload)

    def test_greeting_turn_does_not_trigger_precision_prompt(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "hi MIM",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertEqual(str(resolution.get("reason", "")), "conversation_override")
        self.assertIn("ready to help", prompt)
        self.assertNotIn("one specific request", prompt)

    def test_identity_question_returns_direct_identity_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "do you know who you are?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip()

        self.assertEqual(prompt, "Yes. I am MIM.")

    def test_tod_identity_question_returns_specific_tod_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "do you know who TOD is?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("tod", prompt)
        self.assertIn("task", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_function_question_without_question_mark_gets_direct_capability_answer(
        self,
    ) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what is your function MIM",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("summarize web pages", prompt)
        self.assertIn("runtime status", prompt)
        self.assertNotIn("got it:", prompt)

    def test_visibility_question_returns_camera_capability_boundary(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "can you see me MIM?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("camera", prompt)
        self.assertTrue(
            ("observations" in prompt) or ("i can currently see" in prompt),
            prompt,
        )
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_visibility_question_uses_live_camera_observation_context(self) -> None:
        session_id = f"objective78-camera-visible-{uuid.uuid4()}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-visible-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-visible"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what is visable?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("camera", prompt)
        self.assertIn("0.82", prompt)
        self.assertNotIn("i can answer that directly", prompt)

    def test_camera_presence_question_uses_live_camera_observation_context(
        self,
    ) -> None:
        session_id = f"objective78-camera-presence-{uuid.uuid4()}"

        status, camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-presence-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-presence"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "Hi MIM, can you see me from the camera?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("camera", prompt)
        self.assertIn("0.82", prompt)
        self.assertIn("cannot confirm", prompt)
        self.assertNotIn("current camera observations", prompt)

    def test_visibility_question_summarizes_multiple_camera_feeds(self) -> None:
        session_id = f"objective78-camera-multi-{uuid.uuid4()}"

        status, front_camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-front-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-multi-front"},
                "observations": [
                    {
                        "object_label": "target-marker",
                        "confidence": 0.82,
                        "zone": "front-center",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, front_camera)

        status, side_camera = post_json(
            "/gateway/perception/camera/events",
            {
                "device_id": f"cam-side-{session_id}",
                "source_type": "camera",
                "session_id": session_id,
                "is_remote": False,
                "min_interval_seconds": 0,
                "duplicate_window_seconds": 2,
                "observation_confidence_floor": 0.2,
                "metadata_json": {"source": "objective78-camera-multi-side"},
                "observations": [
                    {
                        "object_label": "operator-notebook",
                        "confidence": 0.77,
                        "zone": "side-left",
                    }
                ],
            },
        )
        self.assertEqual(status, 200, side_camera)

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "what do you see from the camera?",
                "parsed_intent": "question",
                "confidence": 0.9,
                "metadata_json": {"conversation_session_id": session_id},
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("target-marker", prompt)
        self.assertIn("operator-notebook", prompt)
        self.assertIn("2 camera feeds", prompt)
        self.assertIn("0.82", prompt)

    def test_object_inquiry_reply_updates_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-inquiry-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-inquiry",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.88,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        first_prompt = (
            str(first_resolution.get("clarification_prompt", "")).strip().lower()
        )
        first_meta = (
            first_resolution.get("metadata_json", {})
            if isinstance(first_resolution, dict)
            else {}
        )
        inquiry_meta = (
            first_meta.get("object_inquiry", {}) if isinstance(first_meta, dict) else {}
        )

        self.assertIn(f"what is {object_label}".lower(), first_prompt)
        self.assertIn(f"what does {object_label} do".lower(), first_prompt)
        self.assertEqual(str(inquiry_meta.get("status", "")).strip(), "pending")

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = (
            str(second_resolution.get("clarification_prompt", "")).strip().lower()
        )

        self.assertIn("i will remember", second_prompt)
        self.assertIn("dock charger", second_prompt)
        self.assertIn("charging the handheld scanner", second_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(str(metadata.get("description", "")).strip(), "a dock charger")
        self.assertEqual(
            str(metadata.get("purpose", "")).strip(),
            "charging the handheld scanner",
        )
        self.assertIn("description", target.get("semantic_fields", []))
        self.assertIn("purpose", target.get("semantic_fields", []))

    def test_object_inquiry_followup_learns_owner_and_home_zone(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-enrichment-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-enrichment",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.87,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        first_resolution = (
            first_payload.get("resolution", {})
            if isinstance(first_payload, dict)
            else {}
        )
        first_prompt = (
            str(first_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn(f"what is {object_label}".lower(), first_prompt)
        self.assertIn(f"what does {object_label} do".lower(), first_prompt)

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)

        second_resolution = (
            second_payload.get("resolution", {})
            if isinstance(second_payload, dict)
            else {}
        )
        second_prompt = (
            str(second_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn(f"who owns {object_label}".lower(), second_prompt)
        self.assertIn(
            f"where should {object_label} normally live".lower(), second_prompt
        )

        third_status, third_payload = self._post_session_turn(
            session_id,
            "It belongs to Jordan. It should live on the charging bench.",
            parsed_intent="discussion",
        )
        self.assertEqual(third_status, 200, third_payload)

        third_resolution = (
            third_payload.get("resolution", {})
            if isinstance(third_payload, dict)
            else {}
        )
        third_prompt = (
            str(third_resolution.get("clarification_prompt", "")).strip().lower()
        )
        self.assertIn("belongs to jordan", third_prompt)
        self.assertIn("charging bench", third_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(str(metadata.get("owner", "")).strip(), "Jordan")
        self.assertEqual(
            str(metadata.get("expected_home_zone", "")).strip(),
            "charging bench",
        )
        self.assertIn("owner", target.get("semantic_fields", []))
        self.assertIn("expected_home_zone", target.get("semantic_fields", []))

    def test_object_inquiry_followup_learns_secondary_semantics(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        session_id = f"objective78-object-secondary-{run_id}"
        object_label = f"dock_obj78_{run_id}"

        status, camera = self._post_camera_event(
            session_id=session_id,
            device_suffix="object-secondary",
            observations=[
                {
                    "object_label": object_label,
                    "confidence": 0.86,
                    "zone": "bench-front",
                }
            ],
        )
        self.assertEqual(status, 200, camera)

        first_status, first_payload = self._post_session_turn(
            session_id,
            "what do you see from the camera?",
            parsed_intent="question",
        )
        self.assertEqual(first_status, 200, first_payload)

        second_status, second_payload = self._post_session_turn(
            session_id,
            "It is a dock charger. It is used for charging the handheld scanner.",
            parsed_intent="discussion",
        )
        self.assertEqual(second_status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn(f"who owns {object_label}".lower(), second_prompt)

        third_status, third_payload = self._post_session_turn(
            session_id,
            "It belongs to Jordan. It should live on the charging bench.",
            parsed_intent="discussion",
        )
        self.assertEqual(third_status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn(f"what kind of object is {object_label}".lower(), third_prompt)
        self.assertIn(
            f"what should i understand about {object_label}".lower(), third_prompt
        )
        self.assertIn(
            f"any notes i should remember about {object_label}".lower(), third_prompt
        )

        fourth_status, fourth_payload = self._post_session_turn(
            session_id,
            "Category is charging equipment. It means the scanner is ready for handoff. Note that the cable is loose.",
            parsed_intent="discussion",
        )
        self.assertEqual(fourth_status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("charging equipment", fourth_prompt)
        self.assertIn("ready for handoff", fourth_prompt)
        self.assertIn("cable is loose", fourth_prompt)

        status, payload = get_json("/workspace/object-library", {"label": object_label})
        self.assertEqual(status, 200, payload)

        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)

        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        self.assertEqual(
            str(metadata.get("category", "")).strip(), "charging equipment"
        )
        self.assertEqual(
            str(metadata.get("meaning", "")).strip(), "the scanner is ready for handoff"
        )
        self.assertEqual(
            str(metadata.get("user_notes", "")).strip(), "the cable is loose"
        )
        self.assertIn("category", target.get("semantic_fields", []))
        self.assertIn("meaning", target.get("semantic_fields", []))
        self.assertIn("user_notes", target.get("semantic_fields", []))

    def test_location_question_uses_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"bench_obj78_{run_id}"
        object_label = f"charger_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.93,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                    "expected_home_zone": scan_area,
                }
            ],
        )

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"where is Jordan's {object_label}?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("last recorded location", prompt)
        self.assertIn(scan_area.lower(), prompt)
        self.assertIn("jordan", prompt)
        self.assertNotIn("camera observation", prompt)

    def test_purpose_question_uses_durable_object_memory(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"desk_obj78_{run_id}"
        object_label = f"dock_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.91,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                }
            ],
        )

        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": f"what is Jordan's {object_label} for?",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("charging the handheld scanner", prompt)
        self.assertIn("jordan", prompt)

    def test_object_library_promotes_semantic_scan_objects(self) -> None:
        run_id = uuid.uuid4().hex[:8]
        scan_area = f"library_obj78_{run_id}"
        object_label = f"charger_obj78_{run_id}"

        self._run_workspace_scan(
            scan_area,
            [
                {
                    "label": object_label,
                    "zone": scan_area,
                    "confidence": 0.94,
                    "owner": "Jordan",
                    "purpose": "charging the handheld scanner",
                    "description": "primary dock charger",
                    "expected_home_zone": scan_area,
                }
            ],
        )

        status, payload = get_json("/workspace/object-library", {"label": run_id})
        self.assertEqual(status, 200, payload)

        summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
        objects = payload.get("objects", []) if isinstance(payload, dict) else []
        self.assertGreaterEqual(int(summary.get("promoted_objects", 0)), 1)

        target = next(
            (
                item
                for item in objects
                if isinstance(item, dict)
                and str(item.get("canonical_name", "")) == object_label
            ),
            None,
        )
        self.assertIsNotNone(target, objects)
        self.assertTrue(bool(target.get("promoted")))
        self.assertGreaterEqual(float(target.get("library_score", 0.0)), 0.45)
        self.assertIn("purpose", target.get("semantic_fields", []))
        self.assertIn("owner", target.get("semantic_fields", []))
        self.assertIn("has semantic memory", target.get("promotion_reasons", []))
        self.assertEqual(str(target.get("zone", "")), scan_area)

    def test_noisy_general_questions_return_specific_answers(self) -> None:
        cases = [
            (
                "quickly can you tell me what time is it for me right now??",
                ["current time is", "utc"],
            ),
            (
                "honestly can you tell me what day is it today??",
                ["today is", "utc"],
            ),
            (
                "can you tell me where are we right now?",
                ["runtime environment", "chat session context"],
            ),
            (
                "honestly what is your primary mission for me?",
                ["primary mission", "assist safely"],
            ),
            (
                "can you tell me what is happening in camera feed right now?",
                ["camera"],
            ),
        ]

        for text, required_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                self.assertNotIn("direct answer: i heard your question", prompt)
                self.assertNotIn("got it:", prompt)
                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                if "camera feed" in text:
                    self.assertTrue(
                        ("observations" in prompt) or ("i can currently see" in prompt),
                        prompt,
                    )

    def test_tod_working_now_and_relationship_questions_are_specific(self) -> None:
        cases = [
            (
                "do you know what TOD is working on right now",
                ["tod", "active objective", "task state"],
            ),
            (
                "how do you and tod work together?",
                ["mim", "tod", "orchestrates"],
            ),
        ]

        for text, required_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                self.assertNotIn("direct answer: i heard your question", prompt)
                self.assertNotIn("got it:", prompt)
                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)

    def test_help_prompt_returns_capability_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "help??",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("summarize web pages", prompt)
        self.assertIn("runtime status", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_app_creation_prompt_returns_specific_planning_answer(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "let's create an application today...",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("scope the application", prompt)
        self.assertIn("mvp", prompt)
        self.assertNotIn("got it:", prompt)

    def test_tod_social_media_capability_check_prompt_is_contextual(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "can you have TOD check the current capability of agentMIM app to run social media posts...",
                "parsed_intent": "question",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()
        reason = str(resolution.get("reason", "")).strip()

        self.assertIn(
            reason,
            {
                "conversation_optional_escalation",
                "conversation_optional_escalation_followup",
            },
        )
        self.assertIn("tod", prompt)
        self.assertIn("social media", prompt)
        self.assertIn("create goal", prompt)

    def test_tod_social_media_capability_variant_is_contextual(self) -> None:
        status, payload = post_json(
            "/gateway/intake/text",
            {
                "text": "have tod verify if agentmim can post to social media",
                "parsed_intent": "discussion",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 200, payload)

        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

        self.assertIn("tod", prompt)
        self.assertIn("social media", prompt)
        self.assertNotIn("got it:", prompt)
        self.assertNotIn("direct answer: i heard your question", prompt)

    def test_chat_transcript_prompts_route_to_specific_answers(self) -> None:
        cases = [
            (
                "hi mim, how are you today",
                ["online", "operating normally"],
                ["got it:"],
            ),
            (
                "great! should we add anything to TODs tasks to improve the system",
                ["tod", "summary"],
                ["got it:"],
            ),
            (
                "what is the system?",
                ["mim", "tod", "objectives", "tasks"],
                ["i can answer that directly"],
            ),
            (
                "what is our objective?",
                ["objective", "reliability", "handoff"],
                ["i can answer that directly"],
            ),
            (
                "what are we working on today?",
                ["top priority today", "reliability"],
                ["i can answer that directly"],
            ),
            (
                "are you ready to start a project?",
                ["ready", "scope", "first tasks"],
                ["operating normally"],
            ),
            (
                "what is TODs status",
                ["tod status", "summary"],
                ["operating normally"],
            ),
            (
                "what is your mission?",
                ["primary mission", "assist safely"],
                ["i can answer that directly"],
            ),
            (
                "what is todays top news",
                ["top ai and tech themes today", "guardrails", "bot authenticity"],
                ["tod is your task and execution orchestration partner"],
            ),
        ]

        for text, required_fragments, forbidden_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "question",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, prompt)

    def test_casual_and_followup_conversation_prompts_stay_specific(self) -> None:
        cases = [
            (
                "hows TOD doing right now",
                ["tod status", "summary"],
                ["got it:", "i can answer that directly"],
            ),
            (
                "whats your mission",
                ["primary mission", "assist safely"],
                ["got it:"],
            ),
            (
                "just answer yes or no, are you healthy",
                ["yes", "healthy", "online"],
                ["got it:"],
            ),
            (
                "can we have a normal conversation?",
                ["direct", "conversational"],
                ["got it:"],
            ),
            (
                "you keep repeating yourself",
                ["avoid repeating", "direct"],
                ["got it:"],
            ),
            (
                "what exactly do you need from me",
                ["one concrete request", "question", "action"],
                ["got it:"],
            ),
            (
                "what is your top risk",
                ["top risk", "handoff"],
                ["got it:"],
            ),
            (
                "how do we reduce that risk",
                ["regression checks", "handoff verification"],
                ["got it:"],
            ),
        ]

        for text, required_fragments, forbidden_fragments in cases:
            with self.subTest(text=text):
                status, payload = post_json(
                    "/gateway/intake/text",
                    {
                        "text": text,
                        "parsed_intent": "discussion",
                        "confidence": 0.9,
                    },
                )
                self.assertEqual(status, 200, payload)

                resolution = (
                    payload.get("resolution", {}) if isinstance(payload, dict) else {}
                )
                prompt = str(resolution.get("clarification_prompt", "")).strip().lower()

                for fragment in required_fragments:
                    self.assertIn(fragment, prompt)
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, prompt)

    def test_session_followups_use_recent_conversation_context(self) -> None:
        session_id = f"objective78-followup-{uuid.uuid4()}"

        status, first_payload = self._post_session_turn(
            session_id,
            "what should we prioritize next?",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, first_payload)
        first_prompt = (
            str(
                (first_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("top priority today", first_prompt)

        status, second_payload = self._post_session_turn(
            session_id,
            "and after that?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("after that", second_prompt)
        self.assertIn("regression", second_prompt)

        status, third_payload = self._post_session_turn(
            session_id,
            "repeat that as a checklist",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("checklist:", third_prompt)
        self.assertIn("stabilize routing", third_prompt)

        status, fourth_payload = self._post_session_turn(
            session_id,
            "short final recap",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("one line", fourth_prompt)
        self.assertIn("stabilize routing", fourth_prompt)

    def test_session_followups_answer_why_dependencies_and_anything_else(self) -> None:
        session_id = f"objective78-followup-why-{uuid.uuid4()}"

        status, first_payload = self._post_session_turn(
            session_id,
            "what should we prioritize next?",
            parsed_intent="question",
        )
        self.assertEqual(status, 200, first_payload)

        status, second_payload = self._post_session_turn(
            session_id,
            "why that?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, second_payload)
        second_prompt = (
            str(
                (second_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("reliability", second_prompt)
        self.assertIn("handoff stability", second_prompt)

        status, third_payload = self._post_session_turn(
            session_id,
            "any dependencies?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, third_payload)
        third_prompt = (
            str(
                (third_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("routing", third_prompt)
        self.assertIn("handoff", third_prompt)

        status, fourth_payload = self._post_session_turn(
            session_id,
            "anything else before we proceed?",
            parsed_intent="discussion",
        )
        self.assertEqual(status, 200, fourth_payload)
        fourth_prompt = (
            str(
                (fourth_payload.get("resolution", {}) or {}).get(
                    "clarification_prompt", ""
                )
            )
            .strip()
            .lower()
        )
        self.assertIn("stale processes", fourth_prompt)
        self.assertIn("runtime", fourth_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
