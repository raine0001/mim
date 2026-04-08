# Objective 128 - Recovery Commitment Rollout Preview

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 121, Objective 122, Objective 126, Objective 127
Target Release Tag: objective-130

## Summary

Operators can apply recovery-derived commitments today, but they cannot preview the concrete downstream effects before committing the governance change.

Objective 128 adds a rollout-preview layer so operators can inspect what scopes, plans, chains, and execution lanes would be affected by applying, reapplying, or revoking a recovery-derived commitment.

## Delivered Slice

Delivered behavior:

- recovery governance now supports `POST /execution/recovery/policy-tuning/commitment/preview` for bounded lifecycle transitions
- preview responses expose expected transition, scope application, active execution counts, chain counts, and recommended next action
- preview reasoning stays descriptive and bounded rather than attempting full execution simulation
- preview output is aligned with the same rollup structures used by apply, evaluate, and UI reasoning surfaces
- operators can inspect expiry and reapplication impact before committing the lifecycle transition

## Behavioral Anchor

Objective 128 is considered delivered when these statements are true:

- operators can preview recovery-governance impact before changing active commitment state
- preview results stay bounded and descriptive rather than attempting full execution simulation
- preview evidence is visible in UI and auditable after the decision is made

## Key Implementation Anchors

- `core/operator_resolution_service.py`
- `core/execution_recovery_service.py`
- `core/routers/execution_control.py`
- `core/routers/operator.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves preview surfaces return inherited-scope impact and bounded transition summaries before the lifecycle change is applied.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: keep preview results descriptive and auditable, not predictive beyond the current governance envelope.