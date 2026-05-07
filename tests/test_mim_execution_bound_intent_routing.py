import unittest
from types import SimpleNamespace

from core.intent_routing_service import (
    classify_console_intent,
    robotics_web_guard_blocks_search,
    route_console_text_input,
)
from core.routers.gateway import (
    _infer_intent,
    _intent_capability,
    _should_use_web_research,
    _text_route_preference,
)


SERVO_PROBE_DIRECTIVE = "MIM-ARM-MULTI-SERVO-ENVELOPE-PROBE-PREP"


class MimExecutionBoundIntentRoutingHarnessTest(unittest.TestCase):
    def test_servo_probe_directive_routes_to_local_robotics_execution_path(self) -> None:
        route = route_console_text_input(SERVO_PROBE_DIRECTIVE, "discussion")

        self.assertEqual(route.classifier_outcome, "robotics_supervised_probe")
        self.assertEqual(route.route_preference, "goal_system")
        self.assertEqual(route.internal_intent, "execute_capability")
        self.assertEqual(route.capability_name, "mim_arm.supervised_probe")
        self.assertFalse(route.web_search_allowed)
        self.assertEqual(
            route.routing_path,
            (
                "input_gateway",
                "intent_classifier",
                "capability_to_goal_bridge",
                "robotics_capability_registry",
                "execution_binding",
            ),
        )

        self.assertEqual(
            _text_route_preference(
                text=SERVO_PROBE_DIRECTIVE,
                parsed_intent="discussion",
                safety_flags=[],
            ),
            "goal_system",
        )

        event = SimpleNamespace(
            parsed_intent="robotics_supervised_probe",
            raw_input=SERVO_PROBE_DIRECTIVE,
            metadata_json={},
            source="text",
        )
        self.assertEqual(_infer_intent(event), "execute_capability")
        self.assertEqual(_intent_capability(event, "execute_capability"), "mim_arm.supervised_probe")

    def test_bounded_arm_command_does_not_trigger_web_search(self) -> None:
        text = "Prepare a bounded arm safe_home probe with motion_allowed and estop_ok checked."
        route = route_console_text_input(text, "discussion")

        self.assertIn(route.classifier_outcome, {"execution_capability_request", "robotics_supervised_probe"})
        self.assertFalse(route.web_search_allowed)
        self.assertTrue(robotics_web_guard_blocks_search(text))
        self.assertFalse(_should_use_web_research(text.lower()))

    def test_true_research_question_still_triggers_web_research(self) -> None:
        text = "Search the web for public ROS 2 gripper calibration examples."
        route = route_console_text_input(text, "discussion")

        self.assertEqual(route.classifier_outcome, "web_research_request")
        self.assertTrue(route.web_search_allowed)
        self.assertIn("web_search_fallback", route.routing_path)

    def test_ambiguous_command_asks_for_clarification(self) -> None:
        self.assertEqual(classify_console_intent("probe it", "discussion"), "unclear_requires_clarification")
        route = route_console_text_input("probe it", "discussion")

        self.assertEqual(route.internal_intent, "request_clarification")
        self.assertEqual(route.classifier_outcome, "unclear_requires_clarification")
        self.assertFalse(route.web_search_allowed)


if __name__ == "__main__":
    unittest.main()
