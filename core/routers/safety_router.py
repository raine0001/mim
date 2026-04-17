"""Router for MIM user action safety monitoring and inquiry.

Exposes endpoints for action assessment, safety inquiry creation and response,
and approval workflows for potentially harmful user actions.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.user_action_inquiry_service import (
    InquiryStatus,
    UserActionInquiryService,
)
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
from core.user_action_safety_monitor import (
    ActionCategory,
    UserAction,
    UserActionSafetyMonitor,
)

# Initialize services (shared instances)
safety_monitor = UserActionSafetyMonitor(Path("runtime/shared"))
inquiry_service = UserActionInquiryService(Path("runtime/shared"))

router = APIRouter(prefix="/mim/safety", tags=["safety"])


# ============================================================================
# Pydantic models for API contracts
# ============================================================================


class UserActionRequest(BaseModel):
    """Request to assess a user action."""
    user_id: str
    action_type: str
    description: str
    category: str  # ActionCategory value
    command: str | None = None
    target_path: str | None = None
    parameters: dict | None = None


class SafetyAssessmentResponse(BaseModel):
    """Safety assessment of a user action."""
    action_id: str
    risk_level: str
    risk_category: str
    reasoning: str
    specific_concerns: list[str]
    safe_to_execute: bool
    recommended_inquiry: bool
    inquiry_not_available: bool = False


class SafetyInquiryResponse(BaseModel):
    """Safety inquiry for user action."""
    inquiry_id: str
    action_id: str
    action_description: str
    risk_level: str
    concerns: list[str]
    questions: list[str]
    mitigations_required: list[str]
    status: str
    system_health_status: str = "healthy"
    governance_summary: str = ""
    operator_prompt: str = ""


class SubmitInquiryResponseRequest(BaseModel):
    """Submit responses to safety inquiry questions."""
    answers: dict[str, str] = Field(..., description="Answers keyed by question")
    understanding: str = Field(
        "", description="User's statement of intent and understanding"
    )


class ApproveInquiryRequest(BaseModel):
    """Approve or reject a responded inquiry."""
    approved: bool
    reason: str = ""


class RecordIntentionRequest(BaseModel):
    """Record user's stated intention before action."""
    stated_intent: str
    understanding: str
    risks_acknowledged: list[str]
    confirmation: bool


def _inquiry_governance_context() -> tuple[str, str, str]:
    status = "healthy"
    try:
        summary = _mim_health_monitor.get_health_summary()
        if isinstance(summary, dict):
            status = str(summary.get("status", "healthy")).strip().lower() or "healthy"
    except Exception:
        status = "healthy"

    if status in {"degraded", "critical"}:
        summary = (
            f"High-risk action inquiry remains open. System health is {status}, so automatic execution must stay paused until operator confirmation."
        )
        prompt = (
            f"System health is {status}. Review the inquiry, confirm the intent, and approve explicitly before execution."
        )
        return status, summary, prompt

    return (
        status,
        "High-risk action inquiry remains open pending operator review.",
        "Review the inquiry and approve explicitly before execution.",
    )


# ============================================================================
# Action Assessment Endpoints
# ============================================================================


@router.post("/assess-action", response_model=SafetyAssessmentResponse, tags=["assessment"])
async def assess_user_action(request: UserActionRequest) -> SafetyAssessmentResponse:
    """Assess the safety risk of a user action.

    Called before executing potentially harmful operations to get an objective
    risk assessment. High-risk actions will recommend an inquiry.
    """
    import datetime
    import uuid

    # Create action object
    action = UserAction(
        action_id=f"action-{uuid.uuid4().hex[:12]}",
        timestamp=datetime.datetime.utcnow().isoformat() + "Z",
        user_id=request.user_id,
        action_type=request.action_type,
        description=request.description,
        category=ActionCategory(request.category),
        command=request.command,
        target_path=request.target_path,
        parameters=request.parameters or {},
    )

    # Assess action
    assessment = safety_monitor.assess_action(action)

    return SafetyAssessmentResponse(
        action_id=assessment.action_id,
        risk_level=assessment.risk_level.value,
        risk_category=assessment.risk_category,
        reasoning=assessment.reasoning,
        specific_concerns=assessment.specific_concerns,
        safe_to_execute=assessment.safe_to_execute,
        recommended_inquiry=assessment.recommended_inquiry,
    )


@router.get("/assess-action/{action_id}", response_model=SafetyAssessmentResponse, tags=["assessment"])
async def get_action_assessment(action_id: str) -> SafetyAssessmentResponse:
    """Get the assessment for a previously assessed action."""
    assessment = safety_monitor.assessed_actions.get(action_id)
    if not assessment:
        raise HTTPException(status_code=404, detail=f"Action {action_id} not assessed")

    return SafetyAssessmentResponse(
        action_id=assessment.action_id,
        risk_level=assessment.risk_level.value,
        risk_category=assessment.risk_category,
        reasoning=assessment.reasoning,
        specific_concerns=assessment.specific_concerns,
        safe_to_execute=assessment.safe_to_execute,
        recommended_inquiry=assessment.recommended_inquiry,
    )


# ============================================================================
# Safety Inquiry Endpoints
# ============================================================================


@router.post("/inquiries", response_model=SafetyInquiryResponse, tags=["inquiry"])
async def create_inquiry_for_action(
    action_id: str, user_id: str, action_description: str
) -> SafetyInquiryResponse:
    """Create a safety inquiry for a high-risk user action.

    Generates questions about the user's intentions and required mitigations.
    User must respond with answers before action can be approved.
    """
    assessment = safety_monitor.assessed_actions.get(action_id)
    if not assessment:
        raise HTTPException(
            status_code=404, detail=f"No assessment found for action {action_id}"
        )

    inquiry = inquiry_service.create_inquiry_from_assessment(
        assessment, user_id, action_description
    )
    system_health_status, governance_summary, operator_prompt = _inquiry_governance_context()

    return SafetyInquiryResponse(
        inquiry_id=inquiry.inquiry_id,
        action_id=inquiry.action_id,
        action_description=inquiry.action_description,
        risk_level=inquiry.risk_level,
        concerns=inquiry.concerns,
        questions=inquiry.questions,
        mitigations_required=inquiry.mitigations_required,
        status=inquiry.status.value,
        system_health_status=system_health_status,
        governance_summary=governance_summary,
        operator_prompt=operator_prompt,
    )


@router.get("/inquiries", response_model=list[SafetyInquiryResponse], tags=["inquiry"])
async def list_inquiries(status: str | None = None) -> list[SafetyInquiryResponse]:
    """List all safety inquiries, optionally filtered by status."""
    status_enum = InquiryStatus(status) if status else None
    inquiries = inquiry_service.list_inquiries(status=status_enum)
    system_health_status, governance_summary, operator_prompt = _inquiry_governance_context()
    return [
        SafetyInquiryResponse(
            inquiry_id=i.inquiry_id,
            action_id=i.action_id,
            action_description=i.action_description,
            risk_level=i.risk_level,
            concerns=i.concerns,
            questions=i.questions,
            mitigations_required=i.mitigations_required,
            status=i.status.value,
            system_health_status=system_health_status,
            governance_summary=governance_summary,
            operator_prompt=operator_prompt,
        )
        for i in inquiries
    ]


@router.get("/inquiries/{inquiry_id}", response_model=SafetyInquiryResponse, tags=["inquiry"])
async def get_inquiry(inquiry_id: str) -> SafetyInquiryResponse:
    """Get details of a specific safety inquiry."""
    inquiry = inquiry_service.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail=f"Inquiry {inquiry_id} not found")
    system_health_status, governance_summary, operator_prompt = _inquiry_governance_context()

    return SafetyInquiryResponse(
        inquiry_id=inquiry.inquiry_id,
        action_id=inquiry.action_id,
        action_description=inquiry.action_description,
        risk_level=inquiry.risk_level,
        concerns=inquiry.concerns,
        questions=inquiry.questions,
        mitigations_required=inquiry.mitigations_required,
        status=inquiry.status.value,
        system_health_status=system_health_status,
        governance_summary=governance_summary,
        operator_prompt=operator_prompt,
    )


@router.get("/inquiries/{inquiry_id}/prompt", response_model=dict, tags=["inquiry"])
async def get_inquiry_prompt(inquiry_id: str) -> dict:
    """Get human-readable prompt for a safety inquiry.

    Returns formatted markdown suitable for displaying to operator.
    """
    inquiry = inquiry_service.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail=f"Inquiry {inquiry_id} not found")

    return {"inquiry_id": inquiry_id, "prompt": inquiry_service.generate_inquiry_prompt(inquiry)}


@router.post("/inquiries/{inquiry_id}/respond", response_model=SafetyInquiryResponse, tags=["inquiry"])
async def respond_to_inquiry(
    inquiry_id: str, request: SubmitInquiryResponseRequest
) -> SafetyInquiryResponse:
    """Submit responses to safety inquiry questions.

    User provides answers to questions about their intentions and understanding.
    """
    try:
        inquiry = inquiry_service.submit_response(
            inquiry_id, request.answers, request.understanding
        )
        return SafetyInquiryResponse(
            inquiry_id=inquiry.inquiry_id,
            action_id=inquiry.action_id,
            action_description=inquiry.action_description,
            risk_level=inquiry.risk_level,
            concerns=inquiry.concerns,
            questions=inquiry.questions,
            mitigations_required=inquiry.mitigations_required,
            status=inquiry.status.value,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/inquiries/pending/user/{user_id}", tags=["inquiry"])
async def get_user_pending_inquiries(user_id: str) -> dict:
    """Get all pending inquiries awaiting user response."""
    inquiries = inquiry_service.get_pending_inquiries_for_user(user_id)
    return {
        "user_id": user_id,
        "pending_count": len(inquiries),
        "inquiries": [
            {
                "inquiry_id": i.inquiry_id,
                "action_id": i.action_id,
                "action_description": i.action_description,
                "risk_level": i.risk_level,
            }
            for i in inquiries
        ],
    }


@router.get("/inquiries/pending/approval", tags=["inquiry"])
async def get_pending_approvals() -> dict:
    """Get all inquiries with responses awaiting approval decision."""
    inquiries = inquiry_service.get_pending_approvals()
    return {
        "pending_approval_count": len(inquiries),
        "inquiries": [
            {
                "inquiry_id": i.inquiry_id,
                "action_id": i.action_id,
                "user_id": i.user_id,
                "risk_level": i.risk_level,
                "action_description": i.action_description,
            }
            for i in inquiries
        ],
    }


@router.post("/inquiries/{inquiry_id}/approve", response_model=SafetyInquiryResponse, tags=["inquiry"])
async def approve_inquiry_response(
    inquiry_id: str, request: ApproveInquiryRequest
) -> SafetyInquiryResponse:
    """Approve or reject an inquiry response.

    Operator makes final decision based on user's response and understanding.
    """
    try:
        inquiry = inquiry_service.evaluate_response(
            inquiry_id, request.approved, request.reason
        )
        return SafetyInquiryResponse(
            inquiry_id=inquiry.inquiry_id,
            action_id=inquiry.action_id,
            action_description=inquiry.action_description,
            risk_level=inquiry.risk_level,
            concerns=inquiry.concerns,
            questions=inquiry.questions,
            mitigations_required=inquiry.mitigations_required,
            status=inquiry.status.value,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# Health Check Endpoint
# ============================================================================


@router.get("/health-check", tags=["assessment"])
async def get_safety_health_check(
    action_id: str, user_id: str, description: str
) -> dict:
    """Get a comprehensive safety health check for a proposed action.

    Returns assessment, concerns, inquiry questions, and mitigation steps
    in one comprehensive health check suitable for decision-making.
    """
    assessment = safety_monitor.assessed_actions.get(action_id)
    if not assessment:
        raise HTTPException(
            status_code=404, detail=f"No assessment for action {action_id}"
        )

    health_check = safety_monitor.get_health_check_for_action(
        list(safety_monitor.assessed_actions.values())[0]
    )
    return health_check
