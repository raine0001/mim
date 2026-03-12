from collections import defaultdict


def _to_float(value: object, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _constraint_keys(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    keys: list[str] = []
    for item in items:
        if isinstance(item, dict):
            key = str(item.get("constraint", "")).strip()
            if key:
                keys.append(key)
    return keys


def aggregate_constraint_outcomes(evaluations: list[dict], *, max_constraints: int = 50) -> list[dict]:
    stats: dict[str, dict] = defaultdict(
        lambda: {
            "constraint_key": "",
            "observations": 0,
            "successes": 0,
            "failures": 0,
            "avg_outcome_quality": 0.0,
            "warning_observations": 0,
            "warning_successes": 0,
            "blocked_observations": 0,
            "example_current_threshold": None,
            "successful_warning_target_confidences": [],
        }
    )

    for row in evaluations:
        decision = str(row.get("decision", "")).strip()
        outcome_result = str(row.get("outcome_result", "unknown")).strip().lower()
        outcome_quality = _to_float(row.get("outcome_quality", 0.0), 0.0)
        warnings = _constraint_keys(row.get("warnings_json", []))
        violations = _constraint_keys(row.get("violations_json", []))
        all_keys = sorted(set(warnings + violations))

        policy_state = row.get("policy_state_json", {}) if isinstance(row.get("policy_state_json", {}), dict) else {}
        workspace_state = row.get("workspace_state_json", {}) if isinstance(row.get("workspace_state_json", {}), dict) else {}

        is_success = outcome_result in {"success", "pass", "passed", "completed", "ok"}
        is_failure = outcome_result in {"failure", "failed", "error", "blocked"}
        if not is_success and not is_failure:
            continue

        for key in all_keys:
            entry = stats[key]
            entry["constraint_key"] = key
            entry["observations"] += 1
            entry["avg_outcome_quality"] += outcome_quality

            if is_success:
                entry["successes"] += 1
            elif is_failure:
                entry["failures"] += 1

            if key in warnings:
                entry["warning_observations"] += 1
                if is_success:
                    entry["warning_successes"] += 1

                if key == "target_confidence_threshold":
                    entry["example_current_threshold"] = _to_float(policy_state.get("min_target_confidence", 0.0), 0.0)
                    target_confidence = workspace_state.get("target_confidence")
                    if is_success and target_confidence is not None:
                        entry["successful_warning_target_confidences"].append(_to_float(target_confidence, 0.0))

            if decision == "blocked":
                entry["blocked_observations"] += 1

    rows = []
    for value in stats.values():
        observations = max(1, int(value["observations"]))
        value["success_rate"] = round(float(value["successes"]) / observations, 3)
        value["avg_outcome_quality"] = round(float(value["avg_outcome_quality"]) / observations, 3)
        rows.append(value)

    rows.sort(key=lambda item: (item["success_rate"], item["observations"]), reverse=True)
    return rows[: max(1, min(max_constraints, 500))]


def build_adjustment_proposals(
    stats_rows: list[dict],
    *,
    min_samples: int,
    success_rate_threshold: float,
    max_proposals: int,
) -> list[dict]:
    proposals: list[dict] = []

    for item in stats_rows:
        observations = int(item.get("observations", 0) or 0)
        success_rate = _to_float(item.get("success_rate", 0.0), 0.0)
        warning_observations = int(item.get("warning_observations", 0) or 0)
        warning_successes = int(item.get("warning_successes", 0) or 0)

        if observations < min_samples:
            continue
        if success_rate < success_rate_threshold:
            continue
        if warning_observations <= 0:
            continue
        if warning_successes <= 0:
            continue

        constraint_key = str(item.get("constraint_key", "")).strip()
        if not constraint_key:
            continue

        proposal = {
            "constraint_key": constraint_key,
            "proposal_type": "soft_weight_adjustment",
            "current_value": None,
            "proposed_value": None,
            "sample_size": observations,
            "success_rate": round(success_rate, 3),
            "hard_constraint": False,
            "rationale": "",
            "metadata_json": {
                "warning_observations": warning_observations,
                "warning_successes": warning_successes,
                "avg_outcome_quality": _to_float(item.get("avg_outcome_quality", 0.0), 0.0),
            },
        }

        if constraint_key == "target_confidence_threshold":
            current_value = _to_float(item.get("example_current_threshold", 0.0), 0.0)
            successful_confidences = item.get("successful_warning_target_confidences", [])
            if isinstance(successful_confidences, list) and successful_confidences:
                avg_success_confidence = sum(_to_float(x, 0.0) for x in successful_confidences) / len(successful_confidences)
                proposed_value = round(max(0.5, min(0.99, avg_success_confidence)), 2)
                proposal["current_value"] = current_value
                proposal["proposed_value"] = proposed_value
                proposal["rationale"] = (
                    "Repeated successful outcomes occurred below the current soft confidence threshold; "
                    "propose threshold validation update via gated promotion."
                )
            else:
                proposal["rationale"] = (
                    "Constraint warning has repeated successful outcomes; propose soft-weight review through gated promotion."
                )
        else:
            proposal["rationale"] = (
                "Repeated successful outcomes under this soft constraint suggest threshold/weight review via gated promotion."
            )

        proposals.append(proposal)
        if len(proposals) >= max(1, min(max_proposals, 50)):
            break

    return proposals
