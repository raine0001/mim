"""Integration guide for MIM user action safety system.

Shows patterns for integrating safety monitoring into MIM's decision-making loop
and operator workflows.
"""

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SAFETY_BASE_URL = "http://127.0.0.1:18001"


async def assess_action_safety(
    user_id: str,
    action_type: str,
    description: str,
    category: str,
    command: str | None = None,
    target_path: str | None = None,
) -> dict[str, Any]:
    """Assess the safety of a proposed user action.
    
    Returns assessment including risk level and whether inquiry is needed.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SAFETY_BASE_URL}/mim/safety/assess-action",
                json={
                    "user_id": user_id,
                    "action_type": action_type,
                    "description": description,
                    "category": category,
                    "command": command,
                    "target_path": target_path,
                },
                timeout=5.0,
            )
            result = response.json()
            logger.debug(f"Action assessment: {result}")
            return result
    except Exception as e:
        logger.error(f"Failed to assess action: {e}")
        return {}


async def create_safety_inquiry(
    action_id: str, user_id: str, action_description: str
) -> dict[str, Any]:
    """Create a safety inquiry for a high-risk action.
    
    This generates questions about user intent and required mitigations.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries",
                params={
                    "action_id": action_id,
                    "user_id": user_id,
                    "action_description": action_description,
                },
                timeout=5.0,
            )
            inquiry = response.json()
            logger.info(f"Created safety inquiry: {inquiry['inquiry_id']}")
            return inquiry
    except Exception as e:
        logger.error(f"Failed to create inquiry: {e}")
        return {}


async def get_inquiry_prompt(inquiry_id: str) -> str:
    """Get human-readable prompt for displaying to user/operator."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries/{inquiry_id}/prompt",
                timeout=5.0,
            )
            return response.json().get("prompt", "")
    except Exception as e:
        logger.error(f"Failed to get inquiry prompt: {e}")
        return ""


async def submit_inquiry_response(
    inquiry_id: str, answers: dict[str, str], understanding: str = ""
) -> dict[str, Any]:
    """Submit user's response to safety inquiry."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries/{inquiry_id}/respond",
                json={"answers": answers, "understanding": understanding},
                timeout=5.0,
            )
            result = response.json()
            logger.info(f"Submitted inquiry response for {inquiry_id}")
            return result
    except Exception as e:
        logger.error(f"Failed to submit response: {e}")
        return {}


async def approve_inquiry(
    inquiry_id: str, approved: bool, reason: str = ""
) -> dict[str, Any]:
    """Operator decision to approve or reject inquiry.
    
    This finalizes the safety decision and allows/blocks the action.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries/{inquiry_id}/approve",
                json={"approved": approved, "reason": reason},
                timeout=5.0,
            )
            result = response.json()
            status = "APPROVED" if approved else "REJECTED"
            logger.info(f"Inquiry {status}: {inquiry_id}")
            return result
    except Exception as e:
        logger.error(f"Failed to approve inquiry: {e}")
        return {}


async def get_pending_inquiries_for_user(user_id: str) -> dict[str, Any]:
    """Get all pending inquiries awaiting user response."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries/pending/user/{user_id}",
                timeout=5.0,
            )
            return response.json()
    except Exception as e:
        logger.error(f"Failed to get pending inquiries: {e}")
        return {}


async def get_pending_approvals() -> dict[str, Any]:
    """Get all inquiries awaiting operator approval."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SAFETY_BASE_URL}/mim/safety/inquiries/pending/approval",
                timeout=5.0,
            )
            return response.json()
    except Exception as e:
        logger.error(f"Failed to get pending approvals: {e}")
        return {}


async def check_action_before_execution(
    user_id: str, action_type: str, description: str, category: str, command: str | None = None
) -> tuple[bool, str]:
    """Check if an action can proceed based on safety assessment.
    
    Returns (can_proceed, reason).
    If can_proceed is False, an inquiry was created and must be responded to.
    """
    # Assess the action
    assessment = await assess_action_safety(
        user_id, action_type, description, category, command
    )

    if not assessment:
        # Assessment failed, be cautious
        logger.warning("Action assessment failed, blocking for safety")
        return False, "Safety assessment unavailable - please try again"

    # Safe actions can proceed immediately
    if assessment.get("safe_to_execute"):
        return True, f"Action safe to execute ({assessment.get('risk_level')} risk)"

    # Recommended inquiry - create one
    if assessment.get("recommended_inquiry"):
        inquiry = await create_safety_inquiry(
            assessment["action_id"], user_id, description
        )
        if inquiry:
            return False, f"Safety inquiry created: {inquiry['inquiry_id']} - user must respond"
        else:
            return False, "Failed to create safety inquiry"

    # Default: allow if no issues detected
    return True, "No safety concerns detected"


# ============================================================================
# Integration Pattern: Decision Loop
# ============================================================================


async def check_action_in_decision_loop(
    decision_context: dict[str, Any],
) -> dict[str, Any]:
    """Integration point in MIM's decision-making loop.
    
    Before executing any decision that involves user actions, check safety.
    
    Example:
    ```python
    decision = await orchestration.get_next_decision()
    if decision.involves_user_action:
        safety_check = await check_action_in_decision_loop(decision.context)
        if not safety_check["can_proceed"]:
            decision.status = "blocked_for_safety"
            decision.reason = safety_check["reason"]
            await orchestration.record_decision(decision)
            return
    ```
    """
    action_info = decision_context.get("action", {})

    if not action_info:
        return {"can_proceed": True, "reason": "No action context"}

    can_proceed, reason = await check_action_before_execution(
        user_id=decision_context.get("user_id", "unknown"),
        action_type=action_info.get("type", "unknown"),
        description=action_info.get("description", ""),
        category=action_info.get("category", "unknown"),
        command=action_info.get("command"),
    )

    return {
        "can_proceed": can_proceed,
        "reason": reason,
        "inquiry_id": None,  # Would be set if inquiry created
    }


# ============================================================================
# Integration Pattern: Operator Dashboard
# ============================================================================


async def get_operator_safety_status() -> dict[str, Any]:
    """Get comprehensive safety status for operator dashboard.
    
    Shows what needs immediate attention related to safety.
    """
    pending_approvals = await get_pending_approvals()
    user_pending = {}

    # Group by user for dashboard display
    try:
        async with httpx.AsyncClient() as client:
            # In production, would query all known users
            for user_id in ["admin", "operator", "service_account"]:
                pending = await get_pending_inquiries_for_user(user_id)
                if pending.get("pending_count", 0) > 0:
                    user_pending[user_id] = pending
    except Exception as e:
        logger.error(f"Failed to get user pending: {e}")

    return {
        "pending_approvals": pending_approvals,
        "pending_by_user": user_pending,
        "dashboard_summary": {
            "total_awaiting_approval": pending_approvals.get("pending_approval_count", 0),
            "total_awaiting_user_response": sum(
                u.get("pending_count", 0) for u in user_pending.values()
            ),
        },
    }


# ============================================================================
# Integration Pattern: Audit Trail
# ============================================================================


async def log_safety_decision(
    action_id: str,
    user_id: str,
    assessment: dict[str, Any],
    decision: str,
    reason: str = "",
) -> None:
    """Log safety decision to audit trail.
    
    Called when action is final blocked/approved.
    """
    audit_entry = {
        "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "action_id": action_id,
        "user_id": user_id,
        "risk_level": assessment.get("risk_level"),
        "decision": decision,  # "approved"/"rejected"
        "reason": reason,
    }

    logger.info(f"Safety audit: {json.dumps(audit_entry)}")

    # In production, write to audit system/database
    # await audit_service.record_safety_decision(audit_entry)


# ============================================================================
# Integration Examples
# ============================================================================


async def example_dangerous_action_workflow():
    """Example: User tries to install untrusted software."""
    print("=== Example: Software Installation Workflow ===\n")

    user_id = "admin@example.com"
    action_description = "Installing python-requests package"

    # Step 1: Assess action
    print("1. Assessing action safety...")
    assessment = await assess_action_safety(
        user_id=user_id,
        action_type="command_execution",
        description=action_description,
        category="software_installation",
        command="pip install requests",
    )
    print(f"   → Risk Level: {assessment.get('risk_level')}")
    print(f"   → Concerns: {assessment.get('specific_concerns', [])[:2]}...")

    # Step 2: Create inquiry
    if assessment.get("recommended_inquiry"):
        print("\n2. Creating safety inquiry...")
        inquiry = await create_safety_inquiry(
            assessment["action_id"], user_id, action_description
        )
        inquiry_id = inquiry.get("inquiry_id")
        print(f"   → Inquiry ID: {inquiry_id}")

        # Step 3: Get prompt
        print("\n3. Displaying inquiry prompt to user...")
        prompt = await get_inquiry_prompt(inquiry_id)
        print(prompt[:500] + "...\n")

        # Step 4: User responds
        print("4. User submitting response...")
        await submit_inquiry_response(
            inquiry_id,
            answers={
                "1": "Updating for security patch",
                "2": "Yes, verified compatibility",
                "3": "Yes, understand the risks",
                "4": "Yes, needed for MIM functionality",
            },
            understanding="I understand this may affect dependencies but it's needed for security.",
        )
        print("   → Response submitted")

        # Step 5: Operator approves
        print("\n5. Operator reviewing and approving...")
        await approve_inquiry(
            inquiry_id,
            approved=True,
            reason="Verified safe version, security patch necessary",
        )
        print("   ✓ APPROVED - Action can now proceed")


async def example_dangerous_action_rejection():
    """Example: User tries to wipe all data without justification."""
    print("\n=== Example: Unsafe Data Deletion (Rejected) ===\n")

    user_id = "junior_admin"

    print("1. Assessing deletion action...")
    assessment = await assess_action_safety(
        user_id=user_id,
        action_type="command_execution",
        description="Deleting all MIM operational data",
        category="data_deletion",
        command="rm -rf /var/lib/mim/*",
    )
    print(f"   → Risk Level: {assessment.get('risk_level')}")

    if assessment.get("recommended_inquiry"):
        print("\n2. Creating safety inquiry...")
        inquiry = await create_safety_inquiry(
            assessment["action_id"], user_id, "Data deletion"
        )
        inquiry_id = inquiry.get("inquiry_id")

        print("\n3. User responding inadequately...")
        await submit_inquiry_response(
            inquiry_id,
            answers={
                "1": "Just experimenting",
                "2": "Not sure",
                "3": "No backup",
                "4": "No particular reason",
            },
            understanding="I'm not sure what I'm doing",
        )

        print("\n4. Operator reviewing response...")
        await approve_inquiry(
            inquiry_id,
            approved=False,
            reason="Insufficient understanding of risks, no backup, no business justification",
        )
        print("   ✗ REJECTED - Action blocked for safety")


if __name__ == "__main__":
    # Run examples
    asyncio.run(example_dangerous_action_workflow())
    asyncio.run(example_dangerous_action_rejection())
