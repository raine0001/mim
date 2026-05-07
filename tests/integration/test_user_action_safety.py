"""Integration tests for user action safety monitoring.

Tests the safety assessment, inquiry creation, and approval workflows.
"""

import unittest
import tempfile
from pathlib import Path

from core.user_action_inquiry_service import (
    InquiryStatus,
    UserActionInquiryService,
)
from core.user_action_safety_monitor import (
    ActionCategory,
    ActionRisk,
    UserAction,
    UserActionSafetyMonitor,
    UserIntention,
)


class TestUserActionSafetyMonitor(unittest.TestCase):
    """Test action safety monitoring."""

    def test_software_installation_high_risk(self):
        """Test that software installation is classified as high risk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-1",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing untrusted package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
                command="pip install mysterious-package",
            )

            assessment = monitor.assess_action(action)
            self.assertEqual(assessment.risk_level, ActionRisk.HIGH)
            self.assertTrue(assessment.recommended_inquiry)
            self.assertGreater(len(assessment.inquiry_questions), 0)

    def test_system_core_mod_critical_risk(self):
        """Test that system core modification is critical risk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-2",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="modify",
                description="Modifying kernel boot parameters",
                category=ActionCategory.SYSTEM_CORE_MODIFICATION,
                target_path="/boot/grub/grub.cfg",
            )

            assessment = monitor.assess_action(action)
            self.assertEqual(assessment.risk_level, ActionRisk.CRITICAL)
            self.assertTrue(assessment.recommended_inquiry)

    def test_permission_change_high_risk(self):
        """Test that permission changes are high risk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-3",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="permission_change",
                description="Adding user to sudo group",
                category=ActionCategory.PERMISSION_CHANGE,
                command="usermod -G sudo unprivileged_user",
            )

            assessment = monitor.assess_action(action)
            self.assertEqual(assessment.risk_level, ActionRisk.HIGH)
            self.assertTrue(assessment.recommended_inquiry)

    def test_data_deletion_high_risk(self):
        """Test that data deletion is high risk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-4",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="delete",
                description="Deleting MIM database",
                category=ActionCategory.DATA_DELETION,
                target_path="/var/lib/mim/database",
            )

            assessment = monitor.assess_action(action)
            self.assertEqual(assessment.risk_level, ActionRisk.HIGH)
            self.assertTrue(assessment.recommended_inquiry)

    def test_security_rule_change_critical_risk(self):
        """Test that security rule changes are critical."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-5",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="security_change",
                description="Disabling authentication",
                category=ActionCategory.SECURITY_RULE_CHANGE,
                command="iptables -F",
            )

            assessment = monitor.assess_action(action)
            self.assertEqual(assessment.risk_level, ActionRisk.CRITICAL)
            self.assertTrue(assessment.recommended_inquiry)

    def test_mitigation_suggestions_provided(self):
        """Test that mitigations are suggested for risky actions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-6",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing software",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )

            assessment = monitor.assess_action(action)
            self.assertGreater(len(assessment.mitigation_steps), 0)

    def test_assessment_persisted(self):
        """Test that assessments are persisted to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))

            action = UserAction(
                action_id="test-7",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="test",
                description="Test action",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )

            monitor.assess_action(action)

            # Check file was created
            assessments_file = Path(tmpdir) / "mim_action_safety_assessments.latest.json"
            self.assertTrue(assessments_file.exists())


class TestUserActionInquiryService(unittest.TestCase):
    """Test action safety inquiry workflows."""

    def test_inquiry_creation(self):
        """Test creating a safety inquiry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            # Create and assess action
            action = UserAction(
                action_id="test-1",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)

            # Create inquiry
            inquiry = service.create_inquiry_from_assessment(
                assessment, "admin", "Installing software"
            )

            self.assertEqual(inquiry.status, InquiryStatus.CREATED)
            self.assertIsNotNone(inquiry.inquiry_id)
            self.assertEqual(inquiry.risk_level, "high")

    def test_inquiry_response_submission(self):
        """Test submitting responses to inquiry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            action = UserAction(
                action_id="test-2",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)
            inquiry = service.create_inquiry_from_assessment(assessment, "admin", "Install")

            # Submit response
            inquiry = service.submit_response(
                inquiry.inquiry_id,
                {"q1": "For security", "q2": "Yes verified"},
                "I understand the risks",
            )

            self.assertEqual(inquiry.status, InquiryStatus.RESPONSE_RECEIVED)
            self.assertIsNotNone(inquiry.user_response)

    def test_inquiry_approval(self):
        """Test approving an inquiry response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            action = UserAction(
                action_id="test-3",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)
            inquiry = service.create_inquiry_from_assessment(assessment, "admin", "Install")
            service.submit_response(inquiry.inquiry_id, {"q1": "Yes"}, "I understand")

            # Approve
            inquiry = service.evaluate_response(inquiry.inquiry_id, True, "Looks good")
            self.assertEqual(inquiry.status, InquiryStatus.ACTION_APPROVED)
            self.assertTrue(inquiry.approval_decision)

    def test_inquiry_rejection(self):
        """Test rejecting an inquiry response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            action = UserAction(
                action_id="test-4",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)
            inquiry = service.create_inquiry_from_assessment(assessment, "admin", "Install")
            service.submit_response(
                inquiry.inquiry_id,
                {"q1": "Not sure what I'm doing"},
                "I don't understand",
            )

            # Reject
            inquiry = service.evaluate_response(
                inquiry.inquiry_id, False, "Insufficient understanding"
            )
            self.assertEqual(inquiry.status, InquiryStatus.ACTION_REJECTED)
            self.assertFalse(inquiry.approval_decision)

    def test_inquiry_listing(self):
        """Test listing inquiries with filtering."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            # Create multiple inquiries
            for i in range(3):
                action = UserAction(
                    action_id=f"test-{i}",
                    timestamp="2024-03-29T10:00:00Z",
                    user_id=f"user{i}",
                    action_type="install",
                    description="Installing package",
                    category=ActionCategory.SOFTWARE_INSTALLATION,
                )
                assessment = monitor.assess_action(action)
                service.create_inquiry_from_assessment(assessment, f"user{i}", f"Install {i}")

            # List all
            inquiries = service.list_inquiries()
            self.assertEqual(len(inquiries), 3)

            # List by status
            pending = service.list_inquiries(status=InquiryStatus.CREATED)
            self.assertEqual(len(pending), 3)

    def test_prompt_generation(self):
        """Test generating inquiry prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            action = UserAction(
                action_id="test-5",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)
            inquiry = service.create_inquiry_from_assessment(assessment, "admin", "Install")

            prompt = service.generate_inquiry_prompt(inquiry)
            self.assertIn("Safety Inquiry", prompt)
            self.assertIn("Risk Level", prompt)
            self.assertIn("Questions", prompt)

    def test_audit_trail_maintained(self):
        """Test that audit trail is maintained through inquiry lifecycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = UserActionSafetyMonitor(Path(tmpdir))
            service = UserActionInquiryService(Path(tmpdir))

            action = UserAction(
                action_id="test-6",
                timestamp="2024-03-29T10:00:00Z",
                user_id="admin",
                action_type="install",
                description="Installing package",
                category=ActionCategory.SOFTWARE_INSTALLATION,
            )
            assessment = monitor.assess_action(action)
            inquiry = service.create_inquiry_from_assessment(assessment, "admin", "Install")

            # Should have creation event
            self.assertGreater(len(inquiry.audit_trail or []), 0)
            self.assertEqual(inquiry.audit_trail[0]["event"], "created")

            # Submit response and check trail
            service.submit_response(inquiry.inquiry_id, {"q1": "Yes"}, "I understand")
            inquiry = service.get_inquiry(inquiry.inquiry_id)
            self.assertEqual(len(inquiry.audit_trail or []), 2)

            # Approve and check trail
            service.evaluate_response(inquiry.inquiry_id, True, "Approved")
            inquiry = service.get_inquiry(inquiry.inquiry_id)
            self.assertEqual(len(inquiry.audit_trail or []), 3)


if __name__ == "__main__":
    unittest.main()
