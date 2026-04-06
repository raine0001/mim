"""User action safety inquiry service with operator confirmation workflow.

Generates inquiries about potentially harmful user actions and collects
explicit intent confirmation before proceeding with dangerous operations.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from core.user_action_safety_monitor import ActionRisk, SafetyAssessment, UserIntention

logger = logging.getLogger(__name__)


class InquiryStatus(str, Enum):
    """Lifecycle status of a safety inquiry."""
    CREATED = "created"
    AWAITING_RESPONSE = "awaiting_response"
    RESPONSE_RECEIVED = "response_received"
    ACTION_APPROVED = "action_approved"
    ACTION_REJECTED = "action_rejected"
    EXPIRED = "expired"


@dataclasses.dataclass
class SafetyInquiry:
    """Inquiry about user intentions before risky action."""
    inquiry_id: str
    action_id: str
    user_id: str
    risk_level: str
    risk_category: str
    action_description: str
    concerns: list[str]
    questions: list[str]
    mitigations_required: list[str]
    status: InquiryStatus = InquiryStatus.CREATED
    created_at: str | None = None
    response_received_at: str | None = None
    user_response: dict[str, Any] | None = None
    approval_decision: bool | None = None
    approval_reason: str | None = None
    audit_trail: list[dict[str, Any]] | None = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.datetime.utcnow().isoformat() + "Z"
        if self.audit_trail is None:
            self.audit_trail = []


class UserActionInquiryService:
    """Manages safety inquiry workflows for potentially harmful user actions."""

    def __init__(self, state_dir: Path = Path("runtime/shared")):
        """Initialize inquiry service."""
        self.state_dir = state_dir
        self.inquiries: dict[str, SafetyInquiry] = {}
        self._load_inquiries()

    def create_inquiry_from_assessment(
        self, assessment: SafetyAssessment, user_id: str, action_description: str
    ) -> SafetyInquiry:
        """Create a safety inquiry based on action assessment."""
        inquiry_id = f"inquiry-{assessment.action_id}-{int(datetime.datetime.utcnow().timestamp() * 1000)}"

        inquiry = SafetyInquiry(
            inquiry_id=inquiry_id,
            action_id=assessment.action_id,
            user_id=user_id,
            risk_level=assessment.risk_level.value,
            risk_category=assessment.risk_category,
            action_description=action_description,
            concerns=assessment.specific_concerns,
            questions=assessment.inquiry_questions,
            mitigations_required=assessment.mitigation_steps,
        )

        self.inquiries[inquiry_id] = inquiry
        self._audit(inquiry, "created")
        self._persist()
        return inquiry

    def submit_response(
        self, inquiry_id: str, answers: dict[str, str], understanding: str = ""
    ) -> SafetyInquiry:
        """Submit user's response to inquiry."""
        inquiry = self.inquiries.get(inquiry_id)
        if not inquiry:
            raise ValueError(f"Inquiry {inquiry_id} not found")

        if inquiry.status not in [InquiryStatus.CREATED, InquiryStatus.AWAITING_RESPONSE]:
            raise ValueError(f"Cannot submit response to inquiry in status {inquiry.status}")

        inquiry.status = InquiryStatus.RESPONSE_RECEIVED
        inquiry.response_received_at = datetime.datetime.utcnow().isoformat() + "Z"
        inquiry.user_response = {
            "answers": answers,
            "understanding": understanding,
            "timestamp": inquiry.response_received_at,
        }

        self._audit(inquiry, "response_received", {"answer_count": len(answers)})
        self._persist()
        return inquiry

    def evaluate_response(
        self, inquiry_id: str, approval: bool, reason: str = ""
    ) -> SafetyInquiry:
        """Evaluate user response and make approval decision."""
        inquiry = self.inquiries.get(inquiry_id)
        if not inquiry:
            raise ValueError(f"Inquiry {inquiry_id} not found")

        if inquiry.status != InquiryStatus.RESPONSE_RECEIVED:
            raise ValueError(f"Cannot evaluate response to inquiry in status {inquiry.status}")

        inquiry.approval_decision = approval
        inquiry.approval_reason = reason
        inquiry.status = InquiryStatus.ACTION_APPROVED if approval else InquiryStatus.ACTION_REJECTED

        self._audit(
            inquiry,
            "approval_decision",
            {
                "approved": approval,
                "reason": reason,
            },
        )

        self._persist()
        return inquiry

    def get_inquiry(self, inquiry_id: str) -> SafetyInquiry | None:
        """Retrieve inquiry by ID."""
        return self.inquiries.get(inquiry_id)

    def list_inquiries(
        self, status: InquiryStatus | None = None, user_id: str | None = None
    ) -> list[SafetyInquiry]:
        """List inquiries, optionally filtered."""
        inquiries = list(self.inquiries.values())
        if status:
            inquiries = [i for i in inquiries if i.status == status]
        if user_id:
            inquiries = [i for i in inquiries if i.user_id == user_id]
        return sorted(inquiries, key=lambda i: i.created_at or "", reverse=True)

    def get_pending_inquiries_for_user(self, user_id: str) -> list[SafetyInquiry]:
        """Get all pending inquiries awaiting user response."""
        return self.list_inquiries(
            status=InquiryStatus.AWAITING_RESPONSE, user_id=user_id
        )

    def get_pending_approvals(self) -> list[SafetyInquiry]:
        """Get all inquiries with responses awaiting approval decision."""
        return [
            i
            for i in self.inquiries.values()
            if i.status == InquiryStatus.RESPONSE_RECEIVED
        ]

    def generate_inquiry_prompt(self, inquiry: SafetyInquiry) -> str:
        """Generate human-readable prompt for inquiry."""
        prompt = f"""
# Safety Inquiry: Confirm Action Intent

**Action:** {inquiry.action_description}
**Risk Level:** {inquiry.risk_level.upper()}
**Category:** {inquiry.risk_category}

## Why We're Asking

MIM has detected that this action carries {inquiry.risk_level} risk and could potentially harm system operations. Before proceeding, we need to understand your intentions.

### Potential Concerns
{self._format_list(inquiry.concerns)}

## Questions for You
{self._format_numbered_list(inquiry.questions)}

## Required Mitigations
If you proceed, please ensure these steps are taken:
{self._format_list(inquiry.mitigations_required)}

## What Happens Next

1. You provide answers to the questions above
2. MIM reviews your responses for understanding and preparation
3. An operator or automated system approves or rejects the action
4. If approved: Action proceeds with full audit trail
5. If rejected: Action is blocked and reason recorded

---

**Inquiry ID:** {inquiry.inquiry_id}
**Created:** {inquiry.created_at}
"""
        return prompt

    def _format_list(self, items: list[str]) -> str:
        """Format list as markdown bullet points."""
        return "\n".join(f"- {item}" for item in items)

    def _format_numbered_list(self, items: list[str]) -> str:
        """Format list as numbered items."""
        return "\n".join(f"{i+1}. {item}" for i, item in enumerate(items))

    def _audit(self, inquiry: SafetyInquiry, event: str, data: dict[str, Any] | None = None) -> None:
        """Log audit event."""
        if inquiry.audit_trail is None:
            inquiry.audit_trail = []
        inquiry.audit_trail.append(
            {
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "event": event,
                "data": data or {},
            }
        )

    def _persist(self) -> None:
        """Persist inquiries to disk."""
        inquiries_file = self.state_dir / "mim_safety_inquiries.latest.json"
        inquiries_file.parent.mkdir(parents=True, exist_ok=True)

        artifact = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "inquiries": [
                {
                    **dataclasses.asdict(i),
                    "status": i.status.value,
                    "created_at": i.created_at,
                    "response_received_at": i.response_received_at,
                }
                for i in self.inquiries.values()
            ],
        }

        inquiries_file.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    def _load_inquiries(self) -> None:
        """Load previously stored inquiries."""
        inquiries_file = self.state_dir / "mim_safety_inquiries.latest.json"
        if inquiries_file.exists():
            try:
                data = json.loads(inquiries_file.read_text(encoding="utf-8"))
                for inquiry_data in data.get("inquiries", []):
                    # Load inquiry from persisted state
                    inquiry = SafetyInquiry(
                        inquiry_id=inquiry_data["inquiry_id"],
                        action_id=inquiry_data["action_id"],
                        user_id=inquiry_data["user_id"],
                        risk_level=inquiry_data["risk_level"],
                        risk_category=inquiry_data["risk_category"],
                        action_description=inquiry_data["action_description"],
                        concerns=inquiry_data["concerns"],
                        questions=inquiry_data["questions"],
                        mitigations_required=inquiry_data["mitigations_required"],
                        status=InquiryStatus(inquiry_data.get("status", "created")),
                    )
                    self.inquiries[inquiry.inquiry_id] = inquiry
            except Exception as e:
                logger.warning(f"Failed to load inquiries: {e}")
