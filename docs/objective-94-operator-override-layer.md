# Objective 94 - Operator Override Layer

Status: implemented

## Summary

Objective 94 adds an explicit override plane for hard-stop, pause, and redirect controls that applies both to active executions and future executions in the same managed scope.

## Delivered Surfaces

- `core/models.py`
- `core/schemas.py`
- `core/execution_policy_gate.py`
- `core/routers/execution_control.py`
- `core/routers/operator.py`

## Acceptance Coverage

- `/execution/overrides` creates active overrides for a scope or an execution.
- Hard-stop overrides immediately block targeted executions and are enforced by the pre-execution policy gate for future executions.
- Override actions are journaled and exposed through the execution control endpoints.