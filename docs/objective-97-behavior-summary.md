# Objective 97 Behavior Summary

Date: 2026-03-28
Objective: 97
Title: Recovery Learning and Escalation Loop

## Behavior Summary

Objective 97 turns recovery from a trace-local loop into a scoped learning surface.

At runtime, the current slice now does four things:

1. aggregates repeated recovery outcomes by `managed_scope`, `capability_family`, and `recovery_decision`
2. turns repeated patterns into explicit escalation decisions such as `continue_bounded_recovery` or `require_operator_takeover`
3. feeds that escalation posture back into recovery arbitration before the next attempt is accepted
4. exposes the resulting learning state through execution APIs, state-bus snapshots, and `/mim/ui/state`

## Observable Runtime Effects

- repeated `failed_again` outcomes for `retry_current_step` escalate the next retry request to operator-mediated recovery
- repeated successful `resume_from_checkpoint` outcomes reinforce that bounded path instead of hiding the history
- mixed histories stay decision-specific, so success on one recovery path does not erase repeated failure on another recovery path in the same scope
- the operator surface explains the escalation through `operator_reasoning.execution_recovery_learning` and `why_recovery_escalated_before_retry`

## Inspectability Surfaces

- `GET /execution/recovery/{trace_id}`
- `GET /execution/recovery/learning/profiles`
- `GET /state-bus/snapshots?snapshot_scope=execution-recovery:{scope}:{trace_id}`
- `GET /mim/ui/state`

## Validation Snapshot

- focused Objective 97 lane: `Ran 6 tests ... OK`
- adjacent 91-97 lane: `Ran 16 tests ... OK`
- broader adjacent branch lane: `Ran 37 tests ... OK`

## Known Boundary Conditions

- no explicit learning decay or expiry yet
- no dedicated operator-success reset path yet
- no direct environment-change invalidation hook yet