# Objective 31 — Safe Reach/Approach Simulation

## Summary

Objective 31 extends safe directed action planning with non-actuating reach/approach simulation before queue handoff. The simulation evaluates path feasibility and collision risk from workspace map context and object memory, then gates queue eligibility with inspectable outcomes.

## Added API

- `POST /workspace/action-plans/{plan_id}/simulate`
  - Request:
    - `collision_risk_threshold` (optional)
    - `metadata_json` (optional)
  - Returns simulation result and updated plan gate state.

- `GET /workspace/action-plans/{plan_id}/simulation`
  - Returns the latest persisted simulation payload and policy outcome for the plan.

## Action-Plan Model Extensions

`WorkspaceActionPlan` now includes:

- `motion_plan`
  - `approach_vector`
  - `target_pose`
  - `clearance_zone`
  - `estimated_path`
  - `collision_risk`
- `simulation_outcome`
- `simulation_status`
- `simulation`
- `simulation_gate_passed`

## Simulation Policy

Simulation outcome is one of:

- `plan_safe`
  - Reachable path with acceptable collision risk.
- `plan_requires_adjustment`
  - Stale/uncertain identity or confidence condition requiring re-confirmation.
- `plan_blocked`
  - Unsafe/unknown zone or collision policy violation.

Simulation response includes operator-visualization fields:

- `reachable`
- `path_length`
- `collision_candidates`
- `confidence`
- `zone`
- `approach_direction`
- `clearance`
- `obstacle_warnings`

## Queue Gate Behavior

- Queue remains non-actuating and operator-mediated.
- If simulation has been executed, queue requires a pass gate (`simulation_outcome=plan_safe` and `simulation_gate_passed=true`).
- Backward-compatible behavior is preserved for plans that have not been simulated.

## Safety Constraints

- No direct robot/motion actuation endpoints are introduced.
- Collision and zone safety decisions are policy-derived from map/object memory.
- Stale/uncertain identity paths escalate to adjustment/reconfirmation rather than silent queueing.
