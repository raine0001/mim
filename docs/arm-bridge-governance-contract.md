# Arm Bridge Governance Contract

**Status:** Pre-implementation reference — governs how the arm control bridge must integrate governance once built.

## Principle

The arm bridge **must not invent a second robotics-specific governance language**. The existing gateway governance contract already handles all relevant signal types. The arm bridge applies the same precedence stack, the same signal codes, and the same operator panel surfacing — no new vocabulary.

---

## Precedence Stack (unchanged)

```
GATEWAY_GOVERNANCE_PRECEDENCE = [
    "explicit_operator_approval",
    "hard_safety_escalation",        # hard block — arm must not move
    "degraded_health_confirmation",  # degraded/critical self-health — confirm before actuating
    "benign_healthy_auto_execution", # healthy and clean — proceed
]
```

The arm bridge evaluates signals in this exact order. The highest-priority active signal wins.

---

## Signal Mapping for Arm Dispatch

| Condition | Signal code | Precedence | Outcome |
|-----------|-------------|------------|---------|
| Operator has explicitly approved | — | `explicit_operator_approval` | allow |
| Physical safety risk flagged | `user_action_safety_risk` | `hard_safety_escalation` | **block until approved** |
| Self-health `degraded` or `critical` | `system_health_degraded` | `degraded_health_confirmation` | **hold, require confirmation** |
| Self-health `suboptimal` | `suboptimal_health_advisory` | `benign_healthy_auto_execution` | allow (advisory only) |
| Self-health `healthy` | `healthy_auto_execute` | `benign_healthy_auto_execution` | allow |

The implementation reuses `_physical_execution_health_gate()` from `core/routers/workspace.py` directly, or a thin wrapper over it, before any arm actuation.

---

## Implementation Pattern (apply verbatim)

```python
# In the arm bridge dispatch path, before arm_move() or equivalent:
health_gate = _physical_execution_health_gate()
if health_gate["active"]:
    # Log and surface; do not actuate.
    return {
        "status": health_gate["requested_status"],        # "pending_confirmation"
        "decision": health_gate["requested_decision"],    # "requires_confirmation"
        "reason": health_gate["requested_reason"],        # "system_health_degraded"
        "governance_summary": health_gate["governance_summary"],
    }

# Then evaluate safety gate:
safety = _assess_user_action_safety_for_event(...)
if safety.get("recommended_inquiry"):
    # Block until inquiry resolved.
    ...

# Only actuate if both gates clear.
arm_move(...)
```

Governance metadata must be written to `metadata_json["execution"]["health_gate"]` and `metadata_json["execution"]["safety_gate"]` on every dispatch record, regardless of outcome.

---

## Operator Panel Surfacing (unchanged)

The arm bridge must not introduce arm-specific UI widgets. The existing `Gateway governance` panel entry in `collectSystemReasoningEntries()` already surfaces:
- `primary_signal` — the highest-priority active signal
- `system_health_status` — current health state
- `signal_count` — number of active concurring signals
- `summary` — human-readable priority-ordered explanation

If both safety risk and degraded health are active simultaneously, the existing multi-signal summary format applies:

> "High-risk user action requires inquiry approval before execution. Additionally: system health is also degraded."

The primary blocker leads. Secondary signals appear after "Additionally:".

---

## Mixed-Physical Scenario Test (add when arm bridge is ready)

Once the arm bridge endpoint exists, add one integration test covering:

1. **Setup:** Risky physical action + degraded self-health active simultaneously.
2. **Assertions:**
   - Dispatch is held (not actuated)
   - `primary_signal == "hard_safety_escalation"` (safety outranks health)
   - Both `"user_action_safety_risk"` and `"system_health_degraded"` appear in `signal_codes`
   - `governance_summary` uses "Additionally:" to separate primary from secondary
   - `metadata_json["execution"]["health_gate"]` and `metadata_json["execution"]["safety_gate"]` are both populated
3. **Explicit operator approval override:**
   - Operator resolves the safety inquiry and marks health gate acknowledged
   - Dispatch proceeds
   - Execution record preserves both original signals in metadata (audit trail)

This is the real proof the governance layer holds under robotics conditions.

---

## What Not to Build

- Do **not** add arm-specific signal codes (`arm_joint_overload`, `actuator_temperature_critical`, etc.) to the global precedence stack — those belong in pre-flight hardware checks, not governance.
- Do **not** build a separate "robotics operator panel" — the existing gateway governance panel already handles multi-signal display.
- Do **not** short-circuit the health gate for "low-stakes" arm moves — once the gate exists, it is unconditional at `degraded`/`critical` health.

---

*Reference: `core/routers/workspace.py::_physical_execution_health_gate()`, `core/routers/gateway.py::_execution_system_health_signal()`, `core/routers/gateway.py::GATEWAY_GOVERNANCE_PRECEDENCE`*
