# Objective 35 — Autonomous Task Execution Policies

## Summary

Objective 35 introduces tightly constrained autonomous execution for safe workspace proposals while preserving operator control and auditability.

## Scope Delivered

- Policy tiers:
  - `manual_only`
  - `operator_required`
  - `auto_safe`
  - `auto_preferred`
- Rule evaluation for auto-execution:
  - confidence threshold per policy tier
  - safe-zone requirement
  - low capability risk score
  - simulation-safe condition when simulation context exists
- Operator override controls:
  - disable/enable auto-execution
  - force manual approval
  - pause monitoring loop
  - tune throttle/threshold settings
- Execution audit metadata and journal entries:
  - trigger reason
  - policy rule used
  - confidence score
  - simulation result
  - execution outcome
- Safety throttle:
  - max auto actions per minute
  - cooldown between actions
  - zone-based limits

## API Additions

- `GET /workspace/autonomy/policy`
- `POST /workspace/autonomy/override`

## Integration Behavior

- Proposal creation paths in workspace-state and monitoring flows now evaluate Objective35 policy before leaving proposals pending.
- Safe proposals can be auto-accepted and converted into queued tasks for TOD.
- Unsafe or policy-restricted proposals remain pending for operator decision.
