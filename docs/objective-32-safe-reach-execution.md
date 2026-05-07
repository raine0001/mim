# Objective 32 — Safe Reach Execution

## Summary

Objective 32 links simulated action plans to guarded physical execution through an explicit execution capability handoff (`reach_target` or `arm_move_safe`) with strict preconditions and abort controls.

## Added API

- `POST /workspace/action-plans/{plan_id}/execute`
  - Queues execution task.
  - Creates TOD-facing capability execution handoff.
  - Returns execution reference and feedback endpoint.

- `POST /workspace/action-plans/{plan_id}/abort`
  - Operator/system safety stop for in-flight execution.
  - Marks execution blocked and plan aborted.

## Execution Capability Payload

Execution handoff carries:

- `plan_id`
- `target_pose`
- `approach_vector`
- `clearance`
- `safety_score`

## Preconditions

Execution is allowed only if:

- plan is operator approved (`status=approved`)
- simulation has passed (`simulation_outcome=plan_safe`, gate passed)
- `collision_risk < threshold`
- `target_confidence >= policy_minimum`

## Safety Abort Conditions

Abort is intended for:

- new obstacle detected
- target confidence dropped
- zone became unsafe

## Safety Posture

- Actuation remains operator guarded.
- Simulation-first policy is enforced.
- Abort path is explicit and journaled.
