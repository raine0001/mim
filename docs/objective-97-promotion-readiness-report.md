# Objective 97 Promotion Readiness Report

Date: 2026-03-28
Status: ready_for_promotion_review
Objective: 97
Title: Recovery Learning and Escalation Loop

## Summary

Objective 97 is implemented as a bounded cross-execution recovery-learning layer on top of Objective 96.

The delivered slice aggregates repeated recovery outcomes by scope, capability family, and recovery decision, then turns those repeated patterns into explicit escalation decisions that affect the next recovery recommendation.

## Delivered Behavior

- repeated failed recovery outcomes escalate the next recovery recommendation before another bounded retry is accepted
- repeated successful recovery outcomes remain bounded and inspectable as reinforced recovery paths
- mixed recovery histories remain inspectable and decision-specific inside the same scope
- recovery escalation remains scope-local and does not bleed into unrelated scopes
- operator-visible reasoning explains why escalation won over another local retry
- recovery-learning profiles are inspectable through execution-control APIs and `/mim/ui/state`

## Implementation Anchors

- `core/models.py`
  - `ExecutionRecoveryLearningProfile`
- `core/execution_recovery_service.py`
  - recovery learning aggregation
  - learning profile persistence
  - escalation-aware recovery conflict arbitration
  - recovery-learning propagation into state-bus and execution metadata
- `core/routers/execution_control.py`
  - recovery learning profile listing endpoint
- `core/routers/mim_ui.py`
  - `operator_reasoning.execution_recovery_learning`
- `tests/integration/test_objective97_recovery_learning_escalation_loop.py`

## Validation Evidence

Focused Objective 97 lane:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 python -m unittest tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 6 tests in 4.318s ... OK`

Adjacent execution-control lane:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 python -m unittest tests.integration.test_objective91_95_execution_control_plane tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 16 tests in 7.510s ... OK`

Broader adjacent branch-neighborhood lane:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 python -m unittest tests.integration.test_objective72_state_bus_consumers_and_subscription tests.integration.test_objective83_governed_inquiry_resolution_loop tests.integration.test_objective84_operator_visible_system_reasoning tests.integration.test_objective86_commitment_enforcement_drift_monitoring_loop tests.integration.test_objective90_cross_policy_conflict_resolution tests.integration.test_objective91_95_execution_control_plane tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 37 tests in 195.424s ... OK`

## Remaining Caveat

This readiness report covers the bounded Objective 97 slice implemented inside the execution-control plane. Wider downstream projections into dedicated autonomy-boundary or separate stability-profile policy rows remain follow-on work rather than blockers for this slice.

Explicit follow-on items:

- add recovery-learning decay or reset semantics so old patterns stop dominating automatically
- decide whether operator-assisted recovery success should explicitly soften or reset a previously negative recovery-learning profile
- decide whether environmental change should trigger proactive recovery-learning downgrade rather than waiting for the next execution outcome