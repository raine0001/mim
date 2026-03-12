import json
from pathlib import Path

from core.config import settings


DEFAULT_POLICY = {
    "thresholds": {"high": 0.85, "medium": 0.6},
    "low_confidence_behavior": "store_only",
    "require_confirmation_intents": ["execute_capability", "identify_object"],
    "blocked_capability_implications": ["arm_movement", "unsafe_motion"],
    "ambiguous_keywords": ["something", "somewhere", "around there", "kind of", "maybe"],
    "unsafe_keywords": ["override safety", "force move", "ignore guardrail"],
    "target_required_verbs": ["move", "pick", "grab", "place", "identify"],
    "max_output_chars": 240,
    "allowed_output_priorities": ["low", "normal", "high"],
    "clarification_templates": {
        "default": "Please clarify your request so I can proceed safely.",
        "missing_target": "I need a specific target to continue. What object or location should I use?",
        "ambiguous_command": "Your command sounds ambiguous. Please restate with explicit action and target.",
        "low_transcript_confidence": "I did not confidently understand that. Please repeat your command clearly.",
        "unsafe_action_request": "I cannot execute that request safely. Please provide a safer alternative.",
    },
}


def load_voice_policy() -> dict:
    policy_path = Path(settings.voice_policy_path)
    if policy_path.exists():
        try:
            loaded = json.loads(policy_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return {
                    **DEFAULT_POLICY,
                    **loaded,
                    "thresholds": {
                        **DEFAULT_POLICY.get("thresholds", {}),
                        **loaded.get("thresholds", {}),
                    },
                    "clarification_templates": {
                        **DEFAULT_POLICY.get("clarification_templates", {}),
                        **loaded.get("clarification_templates", {}),
                    },
                }
        except Exception:
            return DEFAULT_POLICY
    return DEFAULT_POLICY


def generate_clarification_prompt(escalation_reasons: list[str]) -> str:
    policy = load_voice_policy()
    templates = policy.get("clarification_templates", {})
    for reason in escalation_reasons:
        if reason in templates:
            return str(templates[reason])
    return str(templates.get("default", DEFAULT_POLICY["clarification_templates"]["default"]))


def evaluate_voice_policy(
    *,
    transcript: str,
    confidence: float,
    internal_intent: str,
    target_capability: str,
) -> dict:
    policy = load_voice_policy()
    thresholds = policy.get("thresholds", {})
    high_threshold = float(thresholds.get("high", 0.85))
    medium_threshold = float(thresholds.get("medium", 0.6))

    if confidence >= high_threshold:
        confidence_tier = "high"
    elif confidence >= medium_threshold:
        confidence_tier = "medium"
    else:
        confidence_tier = "low"

    transcript_l = transcript.lower()
    escalation_reasons: list[str] = []

    ambiguous_keywords = [str(item).lower() for item in policy.get("ambiguous_keywords", [])]
    if any(keyword in transcript_l for keyword in ambiguous_keywords):
        escalation_reasons.append("ambiguous_command")

    unsafe_keywords = [str(item).lower() for item in policy.get("unsafe_keywords", [])]
    if any(keyword in transcript_l for keyword in unsafe_keywords):
        escalation_reasons.append("unsafe_action_request")

    blocked_capability_implications = {str(item).lower() for item in policy.get("blocked_capability_implications", [])}
    if target_capability and target_capability.lower() in blocked_capability_implications:
        escalation_reasons.append("unsafe_action_request")

    target_required_verbs = [str(item).lower() for item in policy.get("target_required_verbs", [])]
    command_has_target_verb = any(verb in transcript_l for verb in target_required_verbs)
    vague_target_tokens = {"it", "that", "there", "something", "somewhere"}
    if command_has_target_verb and any(token in transcript_l.split() for token in vague_target_tokens):
        escalation_reasons.append("missing_target")

    if confidence_tier == "low":
        escalation_reasons.append("low_transcript_confidence")

    if confidence_tier == "high":
        outcome = "auto_execute"
    elif confidence_tier == "medium":
        outcome = "requires_confirmation"
    else:
        low_behavior = str(policy.get("low_confidence_behavior", "store_only"))
        outcome = low_behavior if low_behavior in {"store_only", "requires_confirmation"} else "store_only"

    if confidence_tier == "low" and outcome != "blocked":
        outcome = str(policy.get("low_confidence_behavior", "store_only"))
        if outcome not in {"store_only", "requires_confirmation"}:
            outcome = "store_only"

    if "unsafe_action_request" in escalation_reasons:
        outcome = "blocked"
    elif confidence_tier != "low" and ("ambiguous_command" in escalation_reasons or "missing_target" in escalation_reasons):
        outcome = "requires_confirmation"
    elif internal_intent in set(policy.get("require_confirmation_intents", [])) and outcome == "auto_execute":
        outcome = "requires_confirmation"

    escalation_reasons = list(dict.fromkeys(escalation_reasons))

    clarification_prompt = ""
    clarification_reasons = {
        "ambiguous_command",
        "missing_target",
        "low_transcript_confidence",
        "unsafe_action_request",
    }
    should_prompt = outcome in {"store_only", "blocked"} or any(reason in clarification_reasons for reason in escalation_reasons)
    if should_prompt:
        if "requires_clarification" not in escalation_reasons:
            escalation_reasons.append("requires_clarification")
        clarification_prompt = generate_clarification_prompt(escalation_reasons)

    return {
        "confidence_tier": confidence_tier,
        "outcome": outcome,
        "escalation_reasons": escalation_reasons,
        "clarification_prompt": clarification_prompt,
        "policy_version": "voice-policy-v1",
    }


def validate_voice_output(message: str, priority: str) -> dict:
    policy = load_voice_policy()
    max_chars = int(policy.get("max_output_chars", 240))
    allowed_priorities = {str(item) for item in policy.get("allowed_output_priorities", ["low", "normal", "high"])}

    if len(message) > max_chars:
        return {"allowed": False, "reason": "output_too_long", "max_chars": max_chars}
    if priority not in allowed_priorities:
        return {"allowed": False, "reason": "invalid_priority", "allowed_priorities": sorted(allowed_priorities)}
    return {"allowed": True, "reason": "ok", "max_chars": max_chars, "allowed_priorities": sorted(allowed_priorities)}
