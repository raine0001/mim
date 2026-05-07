# Objective 95 - Stability Guardrails and Drift Detection

Status: implemented

## Summary

Objective 95 adds a stability profile layer that scores blocked, failed, pending-review, and oscillating execution behavior and exposes the resulting mitigation posture.

## Delivered Surfaces

- `core/stability_monitor.py`
- `core/models.py`
- `core/schemas.py`
- `core/execution_policy_gate.py`
- `core/routers/execution_control.py`

## Acceptance Coverage

- Stability profiles are generated as executions enter the governed control plane.
- `/execution/stability` and `/execution/stability/evaluate` expose mitigation state and drift metrics per scope or trace.
- Active hard-stop overrides are reflected in the reported mitigation state.