import json
from pathlib import Path

from core.config import settings


DEFAULT_POLICY = {
    "thresholds": {
        "high": 0.85,
        "medium": 0.6,
    },
    "allow_auto_propose": True,
    "auto_execute_safe_intents": ["observe_workspace"],
    "blocked_capability_implications": ["arm_movement", "unsafe_motion"],
    "label_overrides": {
        "unknown_object": {
            "requires_confirmation": True,
            "escalation_reasons": ["unknown_object"],
        },
        "ambiguous_label": {
            "requires_confirmation": True,
            "escalation_reasons": ["ambiguous_label"],
        },
    },
}


def load_vision_policy() -> dict:
    policy_path = Path(settings.vision_policy_path)
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
                    "label_overrides": {
                        **DEFAULT_POLICY.get("label_overrides", {}),
                        **loaded.get("label_overrides", {}),
                    },
                }
        except Exception:
            return DEFAULT_POLICY
    return DEFAULT_POLICY


def evaluate_vision_policy(
    *,
    confidence: float,
    internal_intent: str,
    raw_observation: str,
    detected_labels: list[str],
    target_capability: str,
    metadata_json: dict,
) -> dict:
    policy = load_vision_policy()
    thresholds = policy.get("thresholds", {})
    high_threshold = float(thresholds.get("high", 0.85))
    medium_threshold = float(thresholds.get("medium", 0.6))

    if confidence >= high_threshold:
        confidence_tier = "high"
    elif confidence >= medium_threshold:
        confidence_tier = "medium"
    else:
        confidence_tier = "low"

    escalation_reasons: list[str] = []
    normalized_labels = {label.strip().lower() for label in detected_labels if label.strip()}
    normalized_raw = raw_observation.lower()

    if confidence_tier == "low":
        escalation_reasons.append("low_confidence_detection")
    elif confidence_tier == "medium":
        escalation_reasons.append("requires_human_confirmation")

    if "unknown_object" in normalized_labels or "unknown object" in normalized_raw:
        escalation_reasons.append("unknown_object")

    if "ambiguous" in normalized_labels or "ambiguous" in normalized_raw:
        escalation_reasons.append("ambiguous_label")

    if len(normalized_labels) > 1:
        escalation_reasons.append("multiple_candidate_objects")

    if metadata_json.get("conflicting_observation"):
        escalation_reasons.append("conflicting_observation")

    blocked_implications = {item.lower() for item in policy.get("blocked_capability_implications", [])}
    if target_capability and target_capability.lower() in blocked_implications:
        escalation_reasons.append("unsafe_capability_implication")

    for label, override in policy.get("label_overrides", {}).items():
        if label.lower() in normalized_labels:
            for reason in override.get("escalation_reasons", []):
                escalation_reasons.append(reason)
            if override.get("requires_confirmation"):
                escalation_reasons.append("requires_human_confirmation")

    escalation_reasons = list(dict.fromkeys(escalation_reasons))

    if "unsafe_capability_implication" in escalation_reasons:
        outcome = "blocked"
    elif confidence_tier == "low":
        outcome = "store_only"
    elif confidence_tier == "medium":
        outcome = "requires_confirmation"
    else:
        allow_auto_propose = bool(policy.get("allow_auto_propose", True))
        safe_intents = {item for item in policy.get("auto_execute_safe_intents", [])}
        if internal_intent in safe_intents and not any(
            reason in {"ambiguous_label", "unknown_object", "multiple_candidate_objects", "conflicting_observation"}
            for reason in escalation_reasons
        ):
            outcome = "auto_execute"
        elif allow_auto_propose:
            outcome = "propose_goal"
        else:
            outcome = "requires_confirmation"

    return {
        "confidence_tier": confidence_tier,
        "outcome": outcome,
        "escalation_reasons": escalation_reasons,
        "policy_version": "vision-policy-v1",
    }
