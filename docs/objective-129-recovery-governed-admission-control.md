# Objective 129 - Recovery-Governed Admission Control

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 96, Objective 117, Objective 118, Objective 121, Objective 122, Objective 127
Target Release Tag: objective-130

## Summary

Recovery-derived commitments currently influence reasoning and boundary recompute, but the live admission points for new execution work do not yet treat those commitments as first-class gate inputs everywhere.

Objective 129 extends recovery governance into admission control so governed recovery posture can block, defer, or require approval before new work is admitted into sensitive scopes.

## Delivered Slice

Delivered behavior:

- active recovery-derived commitments now feed live admission decisions through the existing execution policy gate
- inherited recovery posture can require operator confirmation for child scopes instead of allowing new work to bypass the recovery lesson
- admission responses preserve recovery-governance source identity, scope application, and admission posture
- admission control remains part of the existing policy gate rather than a second recovery-only blocker
- execution-control and UI rollups expose the resulting posture so blocked or deferred work remains explainable from one consistent governance source

## Behavioral Anchor

Objective 129 is considered delivered when these statements are true:

- active recovery-derived posture can prevent new work from bypassing the recovery lesson that produced the commitment
- blocked or deferred admission decisions are explainable from one consistent governance source
- recovery admission control does not create a second execution gate independent of the existing policy gate

## Key Implementation Anchors

- `core/execution_policy_gate.py`
- `core/routers/workspace.py`
- `core/routers/execution_control.py`
- `core/operator_resolution_service.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves a child execution inherits the parent recovery commitment and is held at operator confirmation instead of dispatching automatically.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: preserve recovery-governed admission inside the shared execution gate so later governance work stays additive rather than divergent.