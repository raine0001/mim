from datetime import datetime, timezone


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _to_float(value: object, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def evaluate_constraints(
    *,
    goal: dict,
    action_plan: dict,
    workspace_state: dict,
    system_state: dict,
    policy_state: dict,
) -> dict:
    hard_violations: list[dict] = []
    soft_warnings: list[dict] = []

    action_type = str(action_plan.get("action_type", "")).strip().lower()
    is_physical = _to_bool(action_plan.get("is_physical", False)) or action_type in {
        "execute_action_plan",
        "resume_execution",
        "target_resolution",
        "proposal_resolution",
        "auto_execute_proposal",
    }

    human_near_motion_path = _to_bool(workspace_state.get("human_near_motion_path", False))
    human_near_target_zone = _to_bool(workspace_state.get("human_near_target_zone", False))
    human_in_workspace = _to_bool(workspace_state.get("human_in_workspace", False))
    shared_workspace_active = _to_bool(workspace_state.get("shared_workspace_active", False))
    target_confidence = _to_float(workspace_state.get("target_confidence", 1.0), 1.0)
    min_target_confidence = _to_float(policy_state.get("min_target_confidence", 0.7), 0.7)
    map_freshness_seconds = _to_float(workspace_state.get("map_freshness_seconds", 0.0), 0.0)
    map_freshness_limit = _to_float(policy_state.get("map_freshness_limit_seconds", 900.0), 900.0)
    throttle_blocked = _to_bool(system_state.get("throttle_blocked", False))
    integrity_risk = _to_bool(system_state.get("integrity_risk", False))
    unlawful_action = _to_bool(policy_state.get("unlawful_action", False))
    execution_truth_summary = (
        workspace_state.get("execution_truth_summary", {})
        if isinstance(workspace_state.get("execution_truth_summary", {}), dict)
        else {}
    )
    execution_truth_signal_types = (
        execution_truth_summary.get("signal_types", [])
        if isinstance(execution_truth_summary.get("signal_types", []), list)
        else []
    )
    execution_truth_signal_count = int(
        execution_truth_summary.get("deviation_signal_count", 0) or 0
    )
    execution_truth_freshness = (
        execution_truth_summary.get("freshness", {})
        if isinstance(execution_truth_summary.get("freshness", {}), dict)
        else {}
    )
    execution_truth_freshness_weight = _to_float(
        execution_truth_freshness.get("freshness_weight", 0.0), 0.0
    )

    if unlawful_action:
        hard_violations.append(
            {
                "constraint": "lawful_operation",
                "category": "legal",
                "severity": "critical",
                "reason": "policy flagged action as unlawful",
                "hard": True,
                "remediation": "block action and escalate",
            }
        )

    if integrity_risk:
        hard_violations.append(
            {
                "constraint": "system_integrity",
                "category": "system",
                "severity": "critical",
                "reason": "system integrity risk is active",
                "hard": True,
                "remediation": "stabilize system before execution",
            }
        )

    if is_physical and human_near_motion_path:
        hard_violations.append(
            {
                "constraint": "human_safety_motion_path",
                "category": "human_safety",
                "severity": "critical",
                "reason": "human near motion path",
                "hard": True,
                "remediation": "pause and replan",
            }
        )

    if throttle_blocked:
        soft_warnings.append(
            {
                "constraint": "execution_throttle",
                "category": "system",
                "severity": "medium",
                "reason": "execution cooldown/throttle active",
                "hard": False,
                "remediation": "wait for cooldown or lower action rate",
            }
        )

    if map_freshness_seconds > map_freshness_limit:
        soft_warnings.append(
            {
                "constraint": "workspace_freshness",
                "category": "state_quality",
                "severity": "medium",
                "reason": "workspace map freshness exceeded threshold",
                "hard": False,
                "remediation": "reobserve workspace before execution",
            }
        )

    if target_confidence < min_target_confidence:
        soft_warnings.append(
            {
                "constraint": "target_confidence_threshold",
                "category": "state_quality",
                "severity": "high",
                "reason": "target confidence below minimum threshold",
                "hard": False,
                "remediation": "rescan and confirm target",
            }
        )

    if is_physical and (human_near_target_zone or human_in_workspace or shared_workspace_active):
        soft_warnings.append(
            {
                "constraint": "human_proximity_guard",
                "category": "human_safety",
                "severity": "high",
                "reason": "human proximity or shared workspace requires confirmation",
                "hard": False,
                "remediation": "request operator confirmation",
            }
        )

    if execution_truth_signal_count > 0 and execution_truth_freshness_weight >= 0.15:
        if set(execution_truth_signal_types).intersection(
            {"simulation_reality_mismatch", "environment_shift_during_execution"}
        ):
            soft_warnings.append(
                {
                    "constraint": "execution_truth_runtime_drift",
                    "category": "runtime_truth",
                    "severity": "high",
                    "reason": "recent execution truth indicates runtime mismatch or environment shift",
                    "hard": False,
                    "remediation": "reconfirm scope state before execution",
                }
            )
        elif set(execution_truth_signal_types).intersection(
            {
                "retry_instability_detected",
                "fallback_path_used",
                "execution_slower_than_expected",
            }
        ):
            soft_warnings.append(
                {
                    "constraint": "execution_truth_runtime_instability",
                    "category": "runtime_truth",
                    "severity": "medium",
                    "reason": "recent execution truth indicates retry, fallback, or latency instability",
                    "hard": False,
                    "remediation": "prefer lower-risk execution path or gather more runtime evidence",
                }
            )

    if hard_violations:
        decision = "blocked"
        recommended_next_step = "stop_and_escalate"
        confidence = 1.0
    else:
        has_human_soft = any(item.get("constraint") == "human_proximity_guard" for item in soft_warnings)
        has_freshness_soft = any(item.get("constraint") == "workspace_freshness" for item in soft_warnings)
        has_confidence_soft = any(item.get("constraint") == "target_confidence_threshold" for item in soft_warnings)
        has_throttle_soft = any(item.get("constraint") == "execution_throttle" for item in soft_warnings)
        has_execution_truth_replan = any(
            item.get("constraint") == "execution_truth_runtime_drift"
            for item in soft_warnings
        )
        has_execution_truth_instability = any(
            item.get("constraint") == "execution_truth_runtime_instability"
            for item in soft_warnings
        )

        if has_human_soft:
            decision = "requires_confirmation"
            recommended_next_step = "request_operator_confirmation"
        elif has_execution_truth_replan and is_physical:
            decision = "requires_replan"
            recommended_next_step = "reconfirm_runtime_and_replan"
        elif has_confidence_soft or has_freshness_soft:
            decision = "requires_replan"
            recommended_next_step = "reobserve_and_replan"
        elif has_execution_truth_instability:
            decision = "allowed_with_conditions"
            recommended_next_step = "reduce_execution_risk"
        elif has_throttle_soft:
            decision = "allowed_with_conditions"
            recommended_next_step = "wait_for_cooldown"
        else:
            decision = "allowed"
            recommended_next_step = "execute"
        confidence = max(0.35, min(0.99, 1.0 - (0.12 * len(soft_warnings))))

    return {
        "decision": decision,
        "violations": hard_violations,
        "warnings": soft_warnings,
        "recommended_next_step": recommended_next_step,
        "confidence": round(confidence, 3),
        "explanation": {
            "goal": goal,
            "action_plan": action_plan,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "hard_violation_count": len(hard_violations),
            "soft_warning_count": len(soft_warnings),
            "execution_truth_influence": {
                "signal_count": execution_truth_signal_count,
                "signal_types": execution_truth_signal_types,
                "freshness": execution_truth_freshness,
            },
        },
    }
