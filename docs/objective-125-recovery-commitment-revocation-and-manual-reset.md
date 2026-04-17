# Objective 125 - Recovery Commitment Revocation and Manual Reset

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 121, Objective 122
Target Release Tag: objective-130

## Summary

Recovery-derived commitments can now be created and evaluated, but operators still lack a first-class bounded way to revoke or reset them when situational knowledge outruns the stored recovery evidence.

Objective 125 adds explicit revocation and manual reset semantics so recovery-governance state can be corrected without waiting for passive expiry.

## Delivered Slice

Delivered behavior:

- recovery-derived commitments now support an explicit operator reset path through `POST /operator/resolution-commitments/{commitment_id}/reset`
- manual reset is preserved in commitment metadata and lifecycle state instead of being inferred from the absence of an active row
- manual reset remains distinct from passive expiry pressure and from later reapplication behavior
- reset state is journaled and surfaced through the same recovery-governance rollup used by recovery tuning and commitment evaluation
- downstream governance surfaces stop treating the reset commitment as active while still preserving the lifecycle evidence for audit and UI reasoning

## Behavioral Anchor

Objective 125 is considered delivered when these statements are true:

- operators can revoke a recovery-derived commitment without mutating unrelated commitment families
- revoked commitments stop shaping downstream boundary reasoning immediately
- later monitoring and outcome evaluation treat revocation as an explicit lifecycle state, not an ambiguous absence of evidence

## Key Implementation Anchors

- `core/operator_resolution_service.py`
- `core/routers/operator.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves manual reset transitions are explicit, auditable, and remain separate from expiry-driven lifecycle changes.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: use manual reset for operator correction, not as a substitute for expiry signaling.