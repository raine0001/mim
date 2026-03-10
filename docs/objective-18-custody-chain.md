# Objective 18: Goal / Action / State Custody Chain

## Scope

Objective 18 introduces execution accountability on top of Objective 17 routing intelligence.

Phase 1 proves five capabilities:

1. A goal can be declared and persisted.
2. An action references that goal and defines expected state delta.
3. Pre and post state snapshots are captured consistently.
4. Validation compares expected delta against observed state change.
5. Custody chain can be retrieved later by goal or task.

## Data Model

### Goal
- `goal_id`
- `objective_id`
- `task_id`
- `goal_type`
- `goal_description`
- `requested_by`
- `priority`
- `status`
- `created_at`

### Action
- `action_id`
- `goal_id`
- `engine`
- `action_type`
- `input_ref`
- `expected_state_delta`
- `validation_method`
- `started_at`
- `completed_at`
- `status`

### StateSnapshot
- `snapshot_id`
- `goal_id`
- `action_id`
- `snapshot_phase` (`pre` | `post`)
- `state_type`
- `state_payload`
- `captured_at`

### ValidationResult
- `validation_id`
- `goal_id`
- `action_id`
- `validation_method`
- `validation_status` (`achieved` | `partial` | `failed` | `blocked` | `unknown`)
- `validation_details`
- `validated_at`

## Execution Contract

`POST /actions` requires:
- `goal_id`
- `expected_state_delta`
- `validation_method`
- `pre_state`
- `post_state`

This removes anonymous execution records and guarantees intent + expected outcome are present.

## Validation Logic (Phase 1)

- Observed state delta is derived from `pre_state` vs `post_state`.
- Validation compares each expected key/value against observed delta.
- Classification:
  - `achieved`: all expected deltas match
  - `partial`: some match
  - `failed`: none match
  - `blocked`: action status is blocked
  - `unknown`: no expected delta and non-success action status

## APIs

### Write
- `POST /goals`
- `POST /actions`

### Read / Inspectability
- `GET /goals`
- `GET /goals/{goal_id}`
- `GET /actions/{action_id}`
- `GET /goals/{goal_id}/custody`
- `GET /tasks/{task_id}/custody`

## Deployment Policy

- Development machine first
- Validate on MIM Server test stack
- Prod remains untouched until Objective 18 gate passes
