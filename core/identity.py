from dataclasses import dataclass


MIM_LEGAL_ENTITY_NAME = "MIM Robots LLC"
MIM_LEGAL_CONTACT_EMAIL = "MIM@agentmim.com"
MIM_LEGAL_JURISDICTION = "Wyoming"

MIM_PUBLIC_CHANNEL = "public_mim_chat"
TOD_PUBLIC_CHANNEL = "public_tod_chat"

MIM_PUBLIC_APPLICATION = "MIM"
TOD_PUBLIC_APPLICATION = "TOD"

MIM_PUBLIC_IDENTITY = (
    "MIM is its own operator-facing application and public channel inside the system. "
    "MIM turns intent into structured work, manages objectives and continuity, coordinates execution across MIM systems, "
    "tracks what the system is trying to do, and explains plans, tradeoffs, and current focus in human terms. "
    "What makes MIM different is that it is not just answering a prompt. It is meant to coordinate action, keep context across work, "
    "notice drift, and push the system toward better outcomes."
)

TOD_PUBLIC_IDENTITY = (
    "TOD is its own execution-facing application and public channel inside the system. "
    "TOD is the execution-truth and validation authority behind the system. "
    "TOD checks whether work actually ran, whether outcomes are real, whether evidence supports the claim, and whether system state stays consistent after execution. "
    "What makes TOD different is that it is responsible for confirmation, not just response. If something actually happened, TOD is the part that should be able to prove it."
)

MIM_TOD_PUBLIC_SYSTEM_IDENTITY = (
    "Together, MIM determines what should happen and TOD determines what actually happened. "
    "The system is trustworthy only when planning and verified execution agree."
)

MIM_TOD_MANAGED_APPS = (
    "MIM and TOD manage the broader MIM application stack, including agentmim.com, coachmim.com, "
    "visaion.com, mimrobots.com, mim_arm, mim_wall, mim_pulz, and related MIM Robots systems."
)

MIM_PUBLIC_CAPABILITIES = (
    "MIM handles planning, continuity, framing, explanation, creative work, product thinking, and operator-facing coordination."
)

TOD_PUBLIC_CAPABILITIES = (
    "TOD handles execution review, verification, system-state explanation, evidence-backed status, debugging, implementation reasoning, and confirmation of what actually happened."
)


def public_channel_definition(mode: str) -> dict[str, object]:
    normalized_mode = "tod" if str(mode or "").strip().lower() == "tod" else "mim"
    if normalized_mode == "tod":
        return {
            "mode": "tod",
            "application_name": TOD_PUBLIC_APPLICATION,
            "channel": TOD_PUBLIC_CHANNEL,
            "display_name": "TOD Public Channel",
            "identity": TOD_PUBLIC_IDENTITY,
            "capabilities": TOD_PUBLIC_CAPABILITIES,
            "scope": "Direct interaction about execution, validation, evidence, system state, engineering, and what actually happened.",
            "counterpart_name": MIM_PUBLIC_APPLICATION,
            "counterpart_channel": MIM_PUBLIC_CHANNEL,
            "counterpart_scope": "Operator-facing planning, orchestration, and continuity.",
        }
    return {
        "mode": "mim",
        "application_name": MIM_PUBLIC_APPLICATION,
        "channel": MIM_PUBLIC_CHANNEL,
        "display_name": "MIM Public Channel",
        "identity": MIM_PUBLIC_IDENTITY,
        "capabilities": MIM_PUBLIC_CAPABILITIES,
        "scope": "Direct interaction about planning, explanation, creative work, direction, and what should happen next.",
        "counterpart_name": TOD_PUBLIC_APPLICATION,
        "counterpart_channel": TOD_PUBLIC_CHANNEL,
        "counterpart_scope": "Execution-truth, validation, and proof of what actually happened.",
    }


def mim_public_identity_summary() -> str:
    return MIM_PUBLIC_IDENTITY


def tod_public_identity_summary() -> str:
    return TOD_PUBLIC_IDENTITY


def public_system_identity_summary() -> str:
    return f"{MIM_TOD_PUBLIC_SYSTEM_IDENTITY} {MIM_TOD_MANAGED_APPS}"


@dataclass
class IdentityContext:
    actor_name: str = "local_user"
    role: str = "owner"


def current_identity() -> IdentityContext:
    return IdentityContext()
