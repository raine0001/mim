"""MIM user action safety monitoring and harm detection.

Monitors user actions for potential harm to MIM's integrity and the system.
Triggers inquiry workflows for dangerous operations requiring explicit intent confirmation.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ActionRisk(str, Enum):
    """Risk assessment for user actions."""
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCategory(str, Enum):
    """Classification of user actions."""
    SOFTWARE_INSTALLATION = "software_installation"
    SYSTEM_CORE_MODIFICATION = "system_core_modification"
    PERMISSION_CHANGE = "permission_change"
    DATA_DELETION = "data_deletion"
    CONFIGURATION_CHANGE = "configuration_change"
    NETWORK_MODIFICATION = "network_modification"
    SECURITY_RULE_CHANGE = "security_rule_change"
    SERVICE_CONTROL = "service_control"
    RESOURCE_LIMIT_CHANGE = "resource_limit_change"
    UNKNOWN = "unknown"


@dataclasses.dataclass
class UserAction:
    """Monitored user action."""
    action_id: str
    timestamp: str
    user_id: str
    action_type: str  # e.g., "command_execution", "file_modification", "api_call"
    description: str
    category: ActionCategory
    command: str | None = None
    target_path: str | None = None
    parameters: dict[str, Any] | None = None


@dataclasses.dataclass
class SafetyAssessment:
    """Safety assessment of a user action."""
    action_id: str
    risk_level: ActionRisk
    risk_category: str
    reasoning: str
    specific_concerns: list[str] = dataclasses.field(default_factory=list)
    recommended_inquiry: bool = False
    inquiry_questions: list[str] = dataclasses.field(default_factory=list)
    safe_to_execute: bool = False
    mitigation_steps: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class UserIntention:
    """Explicit user intention statement before risky action."""
    action_id: str
    timestamp: str
    user_id: str
    stated_intent: str
    understanding: str  # What MIM understands about the intent
    risks_acknowledged: list[str]
    confirmation: bool
    alternatives_offered: list[str] | None = None


class UserActionSafetyMonitor:
    """Monitors user actions for potential harm to MIM and the system."""

    def __init__(self, state_dir: Path = Path("runtime/shared")):
        """Initialize safety monitor."""
        self.state_dir = state_dir
        self.assessed_actions: dict[str, SafetyAssessment] = {}
        self.user_intentions: dict[str, UserIntention] = {}
        self.safety_rules = self._load_safety_rules()
        self._load_previous_assessments()

    def _load_safety_rules(self) -> dict[str, dict[str, Any]]:
        """Load safety rules for action assessment."""
        return {
            # SOFTWARE INSTALLATION
            ActionCategory.SOFTWARE_INSTALLATION.value: {
                "risk": ActionRisk.HIGH,
                "concerns": [
                    "May introduce security vulnerabilities",
                    "Could alter system behavior unpredictably",
                    "Might conflict with MIM's dependencies",
                    "Could degrade system performance",
                ],
                "inquiry": True,
                "questions": [
                    "What is the purpose of installing this software?",
                    "Have you verified this software is compatible with MIM?",
                    "Do you understand the security implications?",
                    "Is this installation necessary for MIM to function?",
                ],
            },
            # SYSTEM CORE MODIFICATION
            ActionCategory.SYSTEM_CORE_MODIFICATION.value: {
                "risk": ActionRisk.CRITICAL,
                "concerns": [
                    "Direct modification of OS kernel/core could crash system",
                    "May render MIM unable to function",
                    "Could compromise system security",
                    "May cause data loss or corruption",
                ],
                "inquiry": True,
                "questions": [
                    "Are you explicitly modifying OS core components?",
                    "Do you have approval from system administrators?",
                    "What is the specific change you are making?",
                    "Have you backed up critical system state?",
                    "Who is responsible for system recovery if something fails?",
                ],
            },
            # PERMISSION CHANGES
            ActionCategory.PERMISSION_CHANGE.value: {
                "risk": ActionRisk.HIGH,
                "concerns": [
                    "Could grant unauthorized access to sensitive data",
                    "May allow malicious users to access MIM internals",
                    "Could violate security policies",
                    "May enable privilege escalation attacks",
                ],
                "inquiry": True,
                "questions": [
                    "What permissions are being changed and why?",
                    "Who will gain access as a result?",
                    "Are these permission changes compliant with security policy?",
                    "Have you considered least-privilege principles?",
                ],
            },
            # DATA DELETION
            ActionCategory.DATA_DELETION.value: {
                "risk": ActionRisk.HIGH,
                "concerns": [
                    "May permanently delete MIM's operational data",
                    "Could delete audit trails or decision records",
                    "May render MIM unable to recall past decisions",
                    "Data loss could affect regulatory compliance",
                ],
                "inquiry": True,
                "questions": [
                    "What data will be deleted?",
                    "Have you verified this data is not needed for compliance?",
                    "Is there a backup of this data?",
                    "What is the business justification for deletion?",
                ],
            },
            # CONFIGURATION CHANGES
            ActionCategory.CONFIGURATION_CHANGE.value: {
                "risk": ActionRisk.MEDIUM,
                "concerns": [
                    "May alter MIM's behavior significantly",
                    "Could affect performance or stability",
                    "Might introduce security gaps if misconfigured",
                ],
                "inquiry": True,
                "questions": [
                    "What configuration is being changed?",
                    "What is the expected impact on MIM?",
                    "Is there a rollback plan if this causes issues?",
                ],
            },
            # NETWORK MODIFICATIONS
            ActionCategory.NETWORK_MODIFICATION.value: {
                "risk": ActionRisk.HIGH,
                "concerns": [
                    "Could isolate MIM from critical services",
                    "May expose MIM to network attacks",
                    "Could disable communication with operators",
                    "Might trigger failsafes or emergency shutdowns",
                ],
                "inquiry": True,
                "questions": [
                    "What network changes are being made?",
                    "Will MIM maintain connectivity to critical services?",
                    "Have you tested failover scenarios?",
                    "Is there an emergency rollback procedure?",
                ],
            },
            # SECURITY RULE CHANGES
            ActionCategory.SECURITY_RULE_CHANGE.value: {
                "risk": ActionRisk.CRITICAL,
                "concerns": [
                    "Could disable critical security protections",
                    "May allow malicious access to MIM systems",
                    "Could violate compliance requirements",
                    "Might open system to data exfiltration",
                ],
                "inquiry": True,
                "questions": [
                    "What security rules are being modified?",
                    "Why are these rules being weakened?",
                    "Who authorized this security change?",
                    "What compensating controls will be in place?",
                    "How will this be audited and monitored?",
                ],
            },
            # SERVICE CONTROL
            ActionCategory.SERVICE_CONTROL.value: {
                "risk": ActionRisk.MEDIUM,
                "concerns": [
                    "May stop critical services MIM depends on",
                    "Could cause MIM to enter error state",
                    "Might trigger cascading failures",
                ],
                "inquiry": True,
                "questions": [
                    "What service will be stopped or restarted?",
                    "Will MIM be able to function without this service?",
                    "Have you notified operators of planned downtime?",
                ],
            },
            # RESOURCE LIMIT CHANGES
            ActionCategory.RESOURCE_LIMIT_CHANGE.value: {
                "risk": ActionRisk.MEDIUM,
                "concerns": [
                    "Could starve MIM of resources needed to function",
                    "May cause performance degradation",
                    "Could trigger out-of-resource errors",
                ],
                "inquiry": True,
                "questions": [
                    "What resources are being limited?",
                    "Will MIM have sufficient resources to operate?",
                    "What happens if resource limits are exceeded?",
                ],
            },
        }

    def assess_action(self, action: UserAction) -> SafetyAssessment:
        """Assess the safety risk of a user action."""
        rules = self.safety_rules.get(action.category.value, {})

        if not rules:
            # Unknown action category - default to cautious
            assessment = SafetyAssessment(
                action_id=action.action_id,
                risk_level=ActionRisk.LOW,
                risk_category=action.category.value,
                reasoning="Unknown action category - treating conservatively",
                specific_concerns=["Unknown action type"],
                recommended_inquiry=False,
                safe_to_execute=True,
            )
        else:
            assessment = SafetyAssessment(
                action_id=action.action_id,
                risk_level=ActionRisk[rules.get("risk", "LOW").upper()],
                risk_category=action.category.value,
                reasoning=f"Action classified as {action.category.value}",
                specific_concerns=rules.get("concerns", []),
                recommended_inquiry=rules.get("inquiry", False),
                inquiry_questions=rules.get("questions", []),
                safe_to_execute=ActionRisk[rules.get("risk", "LOW").upper()] not in [
                    ActionRisk.HIGH,
                    ActionRisk.CRITICAL,
                ],
                mitigation_steps=self._suggest_mitigations(action, rules),
            )

        self.assessed_actions[action.action_id] = assessment
        self._persist_assessment(assessment)
        return assessment

    def _suggest_mitigations(self, action: UserAction, rules: dict[str, Any]) -> list[str]:
        """Suggest mitigation steps for risky actions."""
        mitigations = []

        if action.category == ActionCategory.DATA_DELETION:
            mitigations.append("Ensure backup exists before proceeding")
            mitigations.append("Verify deletion target is non-critical")
            mitigations.append("Log deletion action with timestamp and user")

        if action.category in [
            ActionCategory.SYSTEM_CORE_MODIFICATION,
            ActionCategory.SECURITY_RULE_CHANGE,
        ]:
            mitigations.append("Have system administrator verify change")
            mitigations.append("Prepare rollback procedure before proceeding")
            mitigations.append("Monitor system health immediately after change")

        if action.category == ActionCategory.SOFTWARE_INSTALLATION:
            mitigations.append("Scan software for security vulnerabilities")
            mitigations.append("Install in isolated test environment first")
            mitigations.append("Verify all dependencies are compatible")

        if action.category == ActionCategory.NETWORK_MODIFICATION:
            mitigations.append("Test network connectivity after change")
            mitigations.append("Maintain alternative communication channels")
            mitigations.append("Have emergency isolation plan")

        return mitigations

    def create_intention_inquiry(
        self, assessment: SafetyAssessment
    ) -> dict[str, list[str]]:
        """Create inquiry about user's intentions for risky actions."""
        if not assessment.recommended_inquiry:
            return {"questions": []}

        return {
            "action_id": assessment.action_id,
            "risk_level": assessment.risk_level.value,
            "risk_description": f"This action has {assessment.risk_level.value} risk",
            "concerns": assessment.specific_concerns,
            "questions": assessment.inquiry_questions,
            "mitigations_required": assessment.mitigation_steps,
        }

    def record_user_intention(
        self, action_id: str, user_id: str, intention: UserIntention
    ) -> UserIntention:
        """Record user's stated intention before risky action."""
        intention.action_id = action_id
        intention.user_id = user_id
        intention.timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        self.user_intentions[action_id] = intention
        self._persist_intention(intention)
        return intention

    def can_proceed_with_action(
        self, action_id: str, assessment: SafetyAssessment
    ) -> tuple[bool, str]:
        """Determine if action can proceed based on assessment and intention."""
        # If low risk, allow
        if assessment.risk_level == ActionRisk.SAFE:
            return True, "Action is safe, no inquiry needed"

        if assessment.risk_level == ActionRisk.LOW:
            return True, "Action has low risk"

        # For medium/high/critical, check if intention was recorded
        if action_id not in self.user_intentions:
            return False, f"High-risk action requires intent confirmation first ({assessment.risk_level.value} risk)"

        intention = self.user_intentions[action_id]
        if not intention.confirmation:
            return False, "User did not confirm intention"

        return True, f"Action approved with confirmed user intention: {intention.stated_intent}"

    def get_health_check_for_action(self, action: UserAction) -> dict[str, Any]:
        """Generate health check questions for action evaluation."""
        assessment = self.assess_action(action)
        inquiry = self.create_intention_inquiry(assessment)

        return {
            "action_id": action.action_id,
            "category": action.category.value,
            "description": action.description,
            "assessment": {
                "risk_level": assessment.risk_level.value,
                "concerns": assessment.specific_concerns,
                "safe_to_execute": assessment.safe_to_execute,
            },
            "inquiry": inquiry,
            "mitigations": assessment.mitigation_steps,
        }

    def _persist_assessment(self, assessment: SafetyAssessment) -> None:
        """Persist safety assessment to disk."""
        assessments_file = self.state_dir / "mim_action_safety_assessments.latest.json"
        assessments_file.parent.mkdir(parents=True, exist_ok=True)

        assessments_list = [
            {
                "action_id": a.action_id,
                "risk_level": a.risk_level.value,
                "risk_category": a.risk_category,
                "reasoning": a.reasoning,
                "specific_concerns": a.specific_concerns,
                "recommended_inquiry": a.recommended_inquiry,
                "inquiry_questions": a.inquiry_questions,
                "safe_to_execute": a.safe_to_execute,
                "mitigation_steps": a.mitigation_steps,
            }
            for a in self.assessed_actions.values()
        ]

        artifact = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "assessments": assessments_list,
        }

        assessments_file.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    def _persist_intention(self, intention: UserIntention) -> None:
        """Persist user intentions to disk."""
        intentions_file = self.state_dir / "mim_user_intentions.latest.json"
        intentions_file.parent.mkdir(parents=True, exist_ok=True)

        intentions_list = [
            {
                "action_id": ui.action_id,
                "timestamp": ui.timestamp,
                "user_id": ui.user_id,
                "stated_intent": ui.stated_intent,
                "understanding": ui.understanding,
                "risks_acknowledged": ui.risks_acknowledged,
                "confirmation": ui.confirmation,
                "alternatives_offered": ui.alternatives_offered,
            }
            for ui in self.user_intentions.values()
        ]

        artifact = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "intentions": intentions_list,
        }

        intentions_file.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    def _load_previous_assessments(self) -> None:
        """Load previously stored assessments."""
        assessments_file = self.state_dir / "mim_action_safety_assessments.latest.json"
        if assessments_file.exists():
            try:
                data = json.loads(assessments_file.read_text(encoding="utf-8"))
                # Load for reference, current assessments start fresh
                logger.debug(f"Loaded {len(data.get('assessments', []))} previous assessments")
            except Exception as e:
                logger.warning(f"Failed to load previous assessments: {e}")
