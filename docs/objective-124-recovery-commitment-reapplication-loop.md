# Objective 124 - Recovery Commitment Reapplication Loop

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 122, Objective 123
Target Release Tag: objective-130

## Summary

Once recovery-derived commitments can expire, the next bounded gap is controlled reapplication when the same scope begins to regress again.

Objective 124 defines the reapplication loop so recurring recovery evidence can recommend renewing a previously expired recovery commitment without creating a second commitment path.

## Delivered Slice

Delivered behavior:

- expired recovery-derived commitments now emit an explicit `reapply_signal` when new recovery evidence indicates the scope is regressing again
- `POST /execution/recovery/policy-tuning/apply` detects reapplication from an inactive prior commitment instead of creating an unrelated new governance row
- renewed commitments record `reapplied_from_commitment_id` so first application and reapplication remain distinguishable
- reapplication remains bounded by the same actionable tuning checks and duplicate-suppression rules as first application
- operator-facing recovery-governance surfaces expose reapplication lineage and recommendation state directly

## Behavioral Anchor

Objective 124 is considered delivered when these statements are true:

- expired recovery-derived commitments can become actionable again from new recovery evidence
- reapplication remains idempotent and bounded at the commitment layer
- operators can tell the difference between first application and reapplication

## Key Implementation Anchors

- `core/execution_recovery_service.py`
- `core/operator_resolution_service.py`
- `core/operator_commitment_monitoring_service.py`
- `core/operator_commitment_outcome_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves expired commitments can become actionable again from fresh recovery evidence and that reapplications retain lineage to the prior commitment.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: keep reapplication operator-mediated and lineage-preserving.