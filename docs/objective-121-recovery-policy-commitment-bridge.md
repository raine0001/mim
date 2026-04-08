# Objective 121 - Recovery Policy Commitment Bridge

Date: 2026-04-07
Status: promoted_verified
Depends On: Objective 85, Objective 96, Objective 97, Objective 119, Objective 120
Target Release Tag: objective-122

## Summary

Objective 121 turns the inspectable `recovery_policy_tuning` recommendation from Objective 120 into a durable, operator-governed commitment.

Before this slice, recovery evaluation could explain how future recovery autonomy should change, but operators still had to translate that recommendation manually into a downstream governance artifact.

Objective 121 closes that gap by adding a bounded bridge endpoint that reads the current recovery-policy tuning recommendation and applies it as a `WorkspaceOperatorResolutionCommitment` with autonomy-posture effects.

## Delivered Slice

Objective 121 is now implemented as an operator-governed application layer above the Objective 120 tuning contract.

Delivered behavior:

- `POST /execution/recovery/policy-tuning/apply` now evaluates the current recovery state and requires an actionable `recovery_policy_tuning` recommendation
- actionable recovery-policy tuning is converted into a durable operator resolution commitment with:
  - `decision_type = lower_autonomy_for_scope`
  - `commitment_family = autonomy_posture`
  - `downstream_effects_json.autonomy_level = recovery_policy_tuning.recommended_boundary_level`
- duplicate apply requests suppress duplicate commitments instead of creating redundant active rows
- the apply operation journals explicit `execution_recovery_policy_tuning_applied` evidence
- the created commitment remains visible through the existing `/operator/resolution-commitments` surfaces
- autonomy-boundary recompute paths consume the active commitment and expose `operator_resolution_commitment_applied`
- `/mim/ui/state` operator reasoning surfaces the active commitment and `current_recommendation` shifts from `execution_recovery_policy_tuning` to the active governance commitment

## Behavioral Anchor

Objective 121 is considered delivered when these statements are true:

- an actionable recovery-policy tuning recommendation can be applied through a bounded endpoint instead of requiring an ad hoc manual translation
- the application path creates a durable commitment rather than silently mutating workspace autonomy state
- duplicate applies are idempotent at the commitment layer
- boundary reasoning and operator-facing UI both reflect the newly active governance commitment

## Key Implementation Anchors

- `core/operator_resolution_service.py`
- `core/routers/operator.py`
- `core/routers/execution_control.py`
- `core/schemas.py`
- `tests/integration/test_objective121_recovery_policy_commitment_bridge.py`
- `tests/integration/test_objective120_recovery_policy_tuning.py`
- `tests/integration/test_objective85_operator_governed_resolution_commitments.py`
- `tests/integration/test_objective97_recovery_learning_escalation_loop.py`
- `tests/integration/test_objective84_operator_visible_system_reasoning.py`
- `tests/integration/test_objective96_execution_recovery_safe_resume.py`
- `tests/integration/test_objective119_recovery_taxonomy.py`

## Validation Evidence

Focused Objective 121 proof:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective121_recovery_policy_commitment_bridge -v`

The focused Objective 121 lane proves:

- repeated operator-mediated recovery outcomes produce actionable `recovery_policy_tuning`
- the new apply endpoint creates a durable resolution commitment for the same scope
- duplicate apply requests are suppressed cleanly
- boundary recompute reasoning shows the active operator resolution commitment being applied
- `/mim/ui/state` shifts to the governance commitment as the current recommendation
- the explicit apply journal entry is preserved

Adjacent regression slice:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective120_recovery_policy_tuning tests.integration.test_objective85_operator_governed_resolution_commitments tests.integration.test_objective97_recovery_learning_escalation_loop tests.integration.test_objective84_operator_visible_system_reasoning tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective119_recovery_taxonomy -v`

That slice verifies the Objective 121 work did not break:

- Objective 120 recovery-policy tuning inspectability
- Objective 85 operator resolution commitment semantics
- Objective 97 recovery-learning escalation behavior
- Objective 84 operator-visible system reasoning
- Objective 96 recovery attempt/outcome behavior
- Objective 119 recovery taxonomy propagation

## Readiness Assessment

- tuning-to-commitment bridge: ready
- duplicate suppression and commitment reuse: ready
- boundary reasoning integration: ready
- operator-facing governance takeover: ready
- adjacent regression coverage: ready

## Readiness Decision

- Objective 121 implementation status: PROMOTED_VERIFIED
- Recommendation: treat Objective 121 as the operator-governed bridge above Objective 120, preserving human approval over any actual autonomy-posture application that derives from recovery learning.