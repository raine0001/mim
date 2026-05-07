# Objective 23: Operator-facing control and exception handling

## Goal

Provide an operator control surface so execution lifecycle intervention is possible without digging through raw logs/endpoints.

## Scope

### Task A — Execution inbox

Added inbox and execution-query endpoints for:

- pending confirmations
- blocked executions
- failed executions
- active/running executions
- recently succeeded executions

### Task B — Operator actions

Added structured action endpoints:

- approve
- reject
- retry
- resume
- cancel
- promote proposal to goal

### Task C — Exception reasons

Normalized exception categories exposed per execution:

- blocked_by_policy
- missing_capability
- low_voice_confidence
- low_vision_confidence
- runtime_failure
- dependency_blocked
- auth_rejected

### Task D — Summary/status surface

Endpoints:

- `GET /operator/inbox`
- `GET /operator/executions`
- `GET /operator/executions/{execution_id}`
- `POST /operator/executions/{execution_id}/approve`
- `POST /operator/executions/{execution_id}/reject`
- `POST /operator/executions/{execution_id}/retry`
- `POST /operator/executions/{execution_id}/resume`
- `POST /operator/executions/{execution_id}/cancel`
- `POST /operator/executions/{execution_id}/promote-to-goal`

### Task E — Audit trail

Every operator action is journaled with:

- execution_id
- goal_id
- prior status
- new status
- reason
- operator actor and metadata

## Validation

Integration test:

- `tests/integration/test_objective23_operator_control.py`

Covers inbox population, action effects, exception normalization visibility, and journal audit linkage.