# Objective 126 - Recovery Commitment Conflict Arbitration

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 90, Objective 121, Objective 122, Objective 123, Objective 125
Target Release Tag: objective-130

## Summary

Recovery-derived commitments now occupy the same governance space as other operator commitments and learned policy surfaces, but their conflict semantics are still implicit.

Objective 126 adds bounded conflict arbitration for recovery-derived commitments so operators can inspect which governance source wins when recovery posture disagrees with other active guidance.

## Delivered Slice

Delivered behavior:

- recovery-derived commitments now participate in conflict reasoning under the explicit policy source `execution_recovery_commitment`
- execution policy and recovery surfaces preserve the winning and losing governance sources instead of relying on ad hoc precedence checks
- recovery-governance rollups surface conflict family, winning source, losing sources, and admission posture for the governed scope
- conflict handling stays inside the existing governance-resolution path rather than introducing a second arbitration mechanism for recovery posture
- downstream execution and UI surfaces consume one resolved winner while keeping the underlying conflict evidence inspectable

## Behavioral Anchor

Objective 126 is considered delivered when these statements are true:

- a recovery-derived commitment can be in explicit conflict with another active policy source without producing ambiguous UI reasoning
- conflict outcomes are inspectable and replayable from stored evidence
- downstream execution and planning surfaces consume one resolved winner for the governed scope

## Key Implementation Anchors

- `core/policy_conflict_resolution_service.py`
- `core/operator_resolution_service.py`
- `core/execution_recovery_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves recovery-derived commitments appear as explicit conflict sources and remain visible in both execution-control rollups and `/mim/ui/state`.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: keep recovery posture inside the shared conflict-resolution stack so operator reasoning stays coherent.