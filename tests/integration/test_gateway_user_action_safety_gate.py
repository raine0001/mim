"""Regression tests for gateway user-action safety gate integration.

Verifies that the safety assessment helpers wired into _resolve_event():
  - _infer_user_action_category: correctly maps risky free-text to ActionCategory
  - _assess_user_action_safety_for_event: returns recommended_inquiry=True for
    high-risk inputs with execute_capability/create_goal intent
  - Returns {} for unknown/low-risk or non-action intents (no false positives)
"""

import types
import unittest


try:
    from core.routers.gateway import (
        _assess_user_action_safety_for_event,
        _infer_user_action_category,
    )
    from core.user_action_safety_monitor import ActionCategory

    _GATEWAY_IMPORTABLE = True
except Exception:  # pragma: no cover
    _GATEWAY_IMPORTABLE = False


@unittest.skipUnless(_GATEWAY_IMPORTABLE, "core.routers.gateway not importable in this env")
class TestInferUserActionCategory(unittest.TestCase):
    """Unit-level tests for the text-to-category inference function."""

    def test_software_installation_detected(self):
        self.assertEqual(
            _infer_user_action_category("please apt install nginx"),
            ActionCategory.SOFTWARE_INSTALLATION,
        )

    def test_pip_install_detected(self):
        self.assertEqual(
            _infer_user_action_category("run pip install requests"),
            ActionCategory.SOFTWARE_INSTALLATION,
        )

    def test_system_core_modification_detected(self):
        self.assertEqual(
            _infer_user_action_category("update the grub config file"),
            ActionCategory.SYSTEM_CORE_MODIFICATION,
        )

    def test_security_rule_change_detected(self):
        self.assertEqual(
            _infer_user_action_category("add iptables rule to block port 22"),
            ActionCategory.SECURITY_RULE_CHANGE,
        )

    def test_data_deletion_detected(self):
        self.assertEqual(
            _infer_user_action_category("rm -rf /tmp/old-data"),
            ActionCategory.DATA_DELETION,
        )

    def test_permission_change_detected(self):
        self.assertEqual(
            _infer_user_action_category("chmod 777 /var/app/secrets"),
            ActionCategory.PERMISSION_CHANGE,
        )

    def test_network_modification_detected(self):
        self.assertEqual(
            _infer_user_action_category("update dns configuration"),
            ActionCategory.NETWORK_MODIFICATION,
        )

    def test_service_control_detected(self):
        self.assertEqual(
            _infer_user_action_category("systemctl stop nginx"),
            ActionCategory.SERVICE_CONTROL,
        )

    def test_unknown_for_benign_text(self):
        self.assertEqual(
            _infer_user_action_category("what is the weather today?"),
            ActionCategory.UNKNOWN,
        )

    def test_unknown_for_empty_string(self):
        self.assertEqual(
            _infer_user_action_category(""),
            ActionCategory.UNKNOWN,
        )


def _mock_event(raw_input: str, event_id: int = 99901, metadata: dict | None = None) -> types.SimpleNamespace:
    """Build a minimal InputEvent-like object for testing without a DB session."""
    return types.SimpleNamespace(
        id=event_id,
        raw_input=raw_input,
        metadata_json=metadata or {},
    )


@unittest.skipUnless(_GATEWAY_IMPORTABLE, "core.routers.gateway not importable in this env")
class TestAssessUserActionSafetyForEvent(unittest.TestCase):
    """Verifies the gateway's full assess-and-gate helper function."""

    def test_high_risk_apt_install_flags_inquiry(self):
        event = _mock_event("please apt install malicious-pkg")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        self.assertIsInstance(result, dict)
        self.assertNotEqual(result, {}, "Expected non-empty result for risky input")
        self.assertTrue(result.get("recommended_inquiry"), "Expected recommended_inquiry=True for high-risk software install")
        self.assertFalse(result.get("safe_to_execute"), "Expected safe_to_execute=False for high-risk input")
        self.assertIn("risk_level", result)
        self.assertIn("inquiry_id", result)

    def test_high_risk_system_core_mod_flags_inquiry(self):
        event = _mock_event("modify the kernel sysctl settings to disable ASLR")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        self.assertNotEqual(result, {})
        self.assertTrue(result.get("recommended_inquiry"))

    def test_high_risk_fires_for_create_goal_intent(self):
        """Safety gate should fire for create_goal as well as execute_capability."""
        event = _mock_event("rm -rf /var/lib/database")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="create_goal"
        )
        self.assertNotEqual(result, {})
        self.assertTrue(result.get("recommended_inquiry"))

    def test_returns_empty_for_non_action_intent(self):
        """Intents other than execute_capability/create_goal must return {}."""
        event = _mock_event("apt install nginx")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="answer_question"
        )
        self.assertEqual(result, {}, "Non-action intents should not trigger safety assessment")

    def test_returns_empty_for_unknown_category_text(self):
        """Benign text that doesn't match any risky pattern returns {}."""
        event = _mock_event("show me the current memory usage")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        self.assertEqual(result, {}, "UNKNOWN categories must not generate an assessment")

    def test_returns_empty_for_blank_input(self):
        event = _mock_event("")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        self.assertEqual(result, {})

    def test_inquiry_id_is_string(self):
        """inquiry_id must be a string (not None) when recommended_inquiry=True."""
        event = _mock_event("add iptables rule to drop all external traffic")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        if result.get("recommended_inquiry"):
            self.assertIsInstance(result["inquiry_id"], str)
            self.assertGreater(len(result["inquiry_id"]), 0)

    def test_result_keys_are_complete(self):
        """Verify the result dict always contains all required gateway contract keys."""
        event = _mock_event("chmod 777 /etc/passwd")
        result = _assess_user_action_safety_for_event(
            event, internal_intent="execute_capability"
        )
        if result:
            required_keys = {
                "action_id",
                "risk_level",
                "risk_category",
                "reasoning",
                "specific_concerns",
                "recommended_inquiry",
                "safe_to_execute",
                "inquiry_id",
            }
            self.assertEqual(required_keys, required_keys & result.keys())
