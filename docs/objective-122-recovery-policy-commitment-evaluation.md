# Objective 122 - Recovery Policy Commitment Evaluation

Date: 2026-04-07
Status: promoted_verified
Depends On: Objective 86, Objective 87, Objective 120, Objective 121
Target Release Tag: objective-122

## Summary

Objective 122 closes the next bounded gap after Objective 121.

Objective 121 could create a durable recovery-derived operator commitment, but the commitment lifecycle still evaluated that commitment only through generic stewardship, maintenance, inquiry, and execution signals. It did not read recovery attempts or recovery outcomes directly.

Objective 122 makes those recovery-derived commitments evaluable in recovery-native terms while still using the existing generic commitment governance path.

## Delivered Slice

Delivered behavior:

- recovery-derived `lower_autonomy_for_scope` commitments now collect recovery-attempt and recovery-outcome evidence during commitment monitoring
- recovery-derived commitments now incorporate recovery outcomes into commitment outcome evaluation
- `POST /execution/recovery/policy-tuning/commitment/evaluate` provides a thin recovery-native wrapper around the generic commitment monitoring and outcome evaluators
- `/mim/ui/state` now exposes:
  - `execution_recovery_policy_commitment`
  - `execution_recovery_policy_commitment_monitoring`
  - `execution_recovery_policy_commitment_outcome`
- operator reasoning recommendation precedence now preserves existing Objective 86 monitoring guidance while still allowing Objective 121 governance takeover when a recovery-derived commitment is the active authority

## Behavioral Anchor

Objective 122 is considered delivered when these statements are true:

- recovery-derived commitments are evaluated against future recovery behavior for the governed scope, not just the original recovery trace that created the commitment
- monitoring and outcome rows preserve recovery-specific evidence counts and reasoning
- operators can evaluate the current recovery-derived commitment directly from the execution-control surface
- `/mim/ui/state` shows the recovery-derived commitment plus its latest monitoring and outcome snapshots without creating a second commitment system

## Key Implementation Anchors

- `core/operator_resolution_service.py`
- `core/operator_commitment_monitoring_service.py`
- `core/operator_commitment_outcome_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`
- `tests/integration/test_objective122_recovery_policy_commitment_evaluation.py`

## Validation Evidence

Focused Objective 122 lane:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective122_recovery_policy_commitment_evaluation`

Adjacent regression slice on a fresh runtime:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18002 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective121_recovery_policy_commitment_bridge tests.integration.test_objective122_recovery_policy_commitment_evaluation tests.integration.test_objective86_commitment_enforcement_drift_monitoring_loop tests.integration.test_objective87_commitment_outcome_learning_loop`

That validation proves:

- Objective 121 commitment creation still holds
- Objective 122 recovery-aware evaluation works end-to-end
- Objective 86 monitoring recommendation behavior still holds
- Objective 87 outcome learning behavior still holds

## Readiness Assessment

- recovery-derived monitoring evidence: ready
- recovery-derived outcome evidence: ready
- recovery-native evaluation wrapper: ready
- operator UI visibility: ready
- adjacent regression coverage: ready

## Readiness Decision

- Objective 122 implementation status: PROMOTED_VERIFIED
- Recommendation: use Objective 122 as the lifecycle/evaluation companion to Objective 121 before considering any later slice that would automate expiry, revocation, or reapplication of recovery-derived commitments.