from __future__ import annotations

import re
from dataclasses import dataclass


CLASSIFIER_OUTCOMES = {
    "execution_capability_request",
    "robotics_supervised_probe",
    "informational_query",
    "web_research_request",
    "unclear_requires_clarification",
}

ROBOTICS_LOCAL_GUARD_TERMS = (
    "servo",
    "gripper",
    "claw",
    "arm",
    "safe_home",
    "supervised probe",
    "motion_allowed",
    "estop_ok",
    "learned_bounds",
)

PUBLIC_INFORMATION_MARKERS = (
    "public information",
    "public sources",
    "on the web",
    "web search",
    "search the web",
    "browse the web",
    "look up",
    "find online",
    "research online",
)


@dataclass(frozen=True)
class ConsoleIntentRoute:
    classifier_outcome: str
    route_preference: str
    internal_intent: str
    capability_name: str
    web_search_allowed: bool
    routing_path: tuple[str, ...]
    reason: str


def normalize_query(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def explicitly_asks_public_information(text: str) -> bool:
    query = normalize_query(text)
    if not query:
        return False
    if any(marker in query for marker in PUBLIC_INFORMATION_MARKERS):
        return True
    return bool(re.search(r"\b(what|who|when|where|why|how)\b.*\b(public|online|web|internet)\b", query))


def contains_robotics_local_guard_term(text: str) -> bool:
    query = normalize_query(text)
    if not query:
        return False
    return any(term in query for term in ROBOTICS_LOCAL_GUARD_TERMS)


def looks_like_robotics_supervised_probe(text: str) -> bool:
    query = normalize_query(text).replace("-", " ")
    if not query:
        return False
    probe_markers = ("probe", "envelope", "learned bounds", "learned_bounds")
    robotics_markers = ("servo", "multi servo", "arm", "gripper", "claw")
    bounded_markers = ("supervised", "bounded", "prep", "safe home", "safe_home")
    return (
        any(marker in query for marker in probe_markers)
        and any(marker in query for marker in robotics_markers)
        and (any(marker in query for marker in bounded_markers) or "mim arm" in query)
    )


def looks_like_gripper_or_claw_command(text: str) -> bool:
    query = normalize_query(text)
    if not query:
        return False
    if not any(marker in query for marker in {"gripper", "claw"}):
        return False
    return any(
        re.search(pattern, query)
        for pattern in (
            r"\bopen\b",
            r"\bclose\b",
            r"\bset\b",
            r"\bmove\b",
            r"\bactuate\b",
            r"\b\d+\s*(?:degree|degrees|deg)\b",
        )
    )


def looks_like_execution_capability_request(text: str, parsed_intent: str = "") -> bool:
    query = normalize_query(text)
    intent = normalize_query(parsed_intent)
    if intent in {"execution_capability_request", "robotics_supervised_probe", "execute_capability"}:
        return True
    if looks_like_robotics_supervised_probe(text):
        return True
    if looks_like_gripper_or_claw_command(text):
        return True
    if contains_robotics_local_guard_term(text) and any(
        marker in query
        for marker in {
            "execute",
            "run",
            "dispatch",
            "invoke",
            "prepare",
            "prep",
            "propose",
            "command",
            "bounded",
            "motion",
            "open",
            "close",
            "set",
            "move",
            "actuate",
        }
    ):
        return True
    return False


def classify_console_intent(text: str, parsed_intent: str = "") -> str:
    query = normalize_query(text)
    if not query:
        return "unclear_requires_clarification"
    if looks_like_robotics_supervised_probe(text):
        return "robotics_supervised_probe"
    if looks_like_execution_capability_request(text, parsed_intent):
        return "execution_capability_request"
    if explicitly_asks_public_information(text):
        return "web_research_request"
    if query.endswith("?") or re.match(r"^(what|why|how|when|where|who|which|is|are|can|could|tell me|explain)\b", query):
        return "informational_query"
    if len(query.split()) <= 3:
        return "unclear_requires_clarification"
    return "informational_query"


def robotics_web_guard_blocks_search(text: str) -> bool:
    return contains_robotics_local_guard_term(text) and not explicitly_asks_public_information(text)


def route_console_text_input(text: str, parsed_intent: str = "") -> ConsoleIntentRoute:
    outcome = classify_console_intent(text, parsed_intent)
    routing_path = [
        "input_gateway",
        "intent_classifier",
        "capability_to_goal_bridge",
    ]
    capability_name = ""
    internal_intent = "speak_response"
    route_preference = "conversation_layer"
    reason = outcome

    if outcome == "robotics_supervised_probe":
        capability_name = "mim_arm.supervised_probe"
        internal_intent = "execute_capability"
        route_preference = "goal_system"
        routing_path.extend(("robotics_capability_registry", "execution_binding"))
    elif outcome == "execution_capability_request":
        internal_intent = "execute_capability"
        route_preference = "goal_system"
        if contains_robotics_local_guard_term(text):
            query = normalize_query(text)
            if looks_like_gripper_or_claw_command(text):
                capability_name = "mim_arm.execute_gripper"
            elif "safe_home" in query:
                capability_name = "mim_arm.execute_safe_home"
            else:
                capability_name = "mim_arm.supervised_probe"
            routing_path.extend(("robotics_capability_registry", "execution_binding"))
        else:
            routing_path.append("execution_binding")
    elif outcome == "web_research_request":
        routing_path.append("web_search_fallback")
    elif outcome == "unclear_requires_clarification":
        internal_intent = "request_clarification"

    web_search_allowed = outcome == "web_research_request" and not robotics_web_guard_blocks_search(text)
    if robotics_web_guard_blocks_search(text):
        reason = "robotics_local_guard"

    return ConsoleIntentRoute(
        classifier_outcome=outcome,
        route_preference=route_preference,
        internal_intent=internal_intent,
        capability_name=capability_name,
        web_search_allowed=web_search_allowed,
        routing_path=tuple(routing_path),
        reason=reason,
    )
