# Objective 123 - Recovery Commitment Expiry Signals

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 122
Target Release Tag: objective-130

## Summary

Objective 122 can evaluate a recovery-derived commitment, but it does not yet produce a stable contract for when that commitment should expire.

Objective 123 defines the bounded expiry-signal layer for recovery-derived commitments so operators can see when the evidence has shifted enough to retire a previously useful autonomy-posture constraint.

## Delivered Slice

Delivered behavior:

- recovery-derived commitment monitoring now emits explicit `expiry_signal` state instead of leaving expiry as an implicit operator judgment
- expiry signaling is derived from recovery-native monitoring and outcome evidence rather than generic commitment age alone
- lifecycle state now distinguishes passive expiry pressure from manual reset and other terminal actions
- execution-control and UI governance surfaces project expiry reasoning without collapsing it into revocation semantics
- expiry evidence is preserved with the commitment monitoring and rollup records used by later lifecycle transitions

## Behavioral Anchor

Objective 123 is considered delivered when these statements are true:

- recovery-derived commitments can accumulate explicit expiry pressure instead of remaining active indefinitely by default
- operators can inspect why a commitment is approaching expiry without reading raw recovery rows
- expiry guidance remains separate from revocation and reapplication decisions

## Key Implementation Anchors

- `core/operator_commitment_monitoring_service.py`
- `core/operator_commitment_outcome_service.py`
- `core/operator_resolution_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves expiry pressure becomes visible after stable recovered outcomes and remains inspectable through the recovery-governance surfaces.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: treat Objective 123 as the bounded passive-expiry contract for the later recovery-governance lifecycle.