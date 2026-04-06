# Objective 97 - Recovery Learning and Escalation Loop

Date: 2026-03-28
Status: implemented
Depends On: Objective 90, Objective 91, Objective 92, Objective 93, Objective 94, Objective 95, Objective 96
Target Release Tag: objective-97

## Problem Statement

Objective 96 gives MIM a bounded recovery loop, but the current slice is still trace-local.

MIM can now:

- evaluate whether a failed or blocked execution is recoverable
- record accepted and blocked recovery attempts
- publish recovery posture to the state bus
- record simple recovery outcomes such as `recovered`, `failed_again`, and `operator_required`

The remaining gap is escalation and reuse across executions.

Without Objective 97, recovery learning stays attached to one trace and does not yet change how the wider system escalates, reprioritizes, or avoids repeating known-bad recovery patterns across a scope or capability family.

## Goal

Objective 97 should turn recovery from a bounded local control loop into a cross-execution learning and escalation surface.

The objective should close the next loop:

- execution failure -> bounded recovery -> recovery outcome -> repeated recovery pattern -> scoped escalation or learning bias -> improved future recovery posture

## Core Outcome

After Objective 97, MIM should be able to:

- aggregate repeated recovery outcomes by scope, capability family, and recovery decision
- detect when recovery is repeatedly ineffective or repeatedly operator-mediated
- escalate sooner when a scope shows repeated failed recovery patterns
- project scoped recovery learning into execution policy, stability posture, and operator-visible recommendations
- explain why the system chose escalation rather than another retry or resume path

## In Scope

### 1. Recovery Outcome Aggregation

Objective 97 should aggregate repeated recovery outcomes across recent executions for the same scope or capability family.

Representative patterns:

- repeated `failed_again` after `retry_current_step`
- repeated `operator_required` after `resume_from_checkpoint`
- repeated successful resume paths that justify stronger confidence for a scope
- repeated rollback-and-replan paths that indicate the current orchestration checkpoint is too weak

### 2. Escalation Decisions

Objective 97 should add a bounded escalation decision layer above Objective 96 outcomes.

Representative escalation decisions:

- `continue_bounded_recovery`
- `require_operator_takeover`
- `pause_scope_for_review`
- `replan_capability_family`
- `lower_scope_autonomy_for_recovery`

### 3. Downstream Propagation

The new recovery-learning surface should influence at least:

- execution-policy gating
- stability mitigation posture
- autonomy-boundary reasoning
- operator-visible reasoning in `/mim/ui/state`
- state-bus publication for recovery escalation snapshots

### 4. Inspectability

Objective 97 must remain visible and auditable.

Recommended inspectability surfaces:

- recovery-learning profiles by scope or capability
- recovery escalation snapshots in the state bus
- operator reasoning fields such as `operator_reasoning.execution_recovery_learning`
- explicit rationale fields such as `why_recovery_escalated_before_retry`

### 5. Validation Requirements

Objective 97 should not close without proving:

1. repeated failed recovery outcomes escalate the next recovery recommendation
2. repeated successful recovery outcomes remain bounded and inspectable rather than hidden
3. escalation remains scope-local and does not bleed into unrelated scopes
4. operator-visible reasoning explains why escalation won over another local retry

## Exit Criteria

Objective 97 is complete when all are true:

1. recovery outcomes can be aggregated into a scoped learning signal
2. escalation decisions are explicit and inspectable
3. the execution-control plane reacts differently when recent recovery patterns are repeatedly failing
4. state-bus and operator surfaces expose the resulting recovery-learning posture

## Implemented Slice

The current Objective 97 slice is implemented as a bounded cross-execution recovery-learning layer on top of Objective 96.

Delivered behavior:

- repeated `failed_again` outcomes for the same scoped recovery decision now aggregate into a durable `ExecutionRecoveryLearningProfile`
- repeated `recovered` outcomes stay bounded and inspectable as reinforced recovery paths rather than being silently collapsed away
- mixed histories remain decision-specific inside a scope, so earlier successful resume paths do not hide later failed retry patterns for a different recovery decision
- recovery learning is keyed by `managed_scope`, `capability_family`, and `recovery_decision`, so escalation does not bleed into unrelated scopes
- explicit escalation decisions such as `require_operator_takeover` and `continue_bounded_recovery` are persisted and surfaced through recovery evaluation payloads
- recovery learning now participates in recovery conflict arbitration, so repeated failed recovery patterns can override another local retry before the next attempt is accepted
- the recovery state bus snapshot now carries the learning payload alongside the trace-local recovery snapshot
- `/mim/ui/state` now exposes `operator_reasoning.execution_recovery_learning` plus the explanatory `why_recovery_escalated_before_retry` field on the recovery surface

## Implementation Anchors

- `core/models.py`
	- `ExecutionRecoveryLearningProfile`
- `core/execution_recovery_service.py`
	- scoped aggregation of recent recovery outcomes
	- persisted learning profiles and policy effects
	- escalation-aware recovery conflict arbitration
	- propagation into execution feedback, trace metadata, orchestration metadata, stability metadata, and state-bus recovery snapshots
- `core/routers/execution_control.py`
	- `GET /execution/recovery/learning/profiles`
- `core/routers/mim_ui.py`
	- `operator_reasoning.execution_recovery_learning`
- `tests/integration/test_objective97_recovery_learning_escalation_loop.py`
	- focused Objective 97 validation lane

## Validation

Focused Objective 97 validation passed on the current-source server at `:18001`:

- `python -m unittest tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 6 tests ... OK`

Adjacent execution-control validation across Objectives 91-97 also passed on `:18001`:

- `python -m unittest tests.integration.test_objective91_95_execution_control_plane tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 16 tests ... OK`

Broader adjacent branch-neighborhood validation also passed on `:18001`:

- `python -m unittest tests.integration.test_objective72_state_bus_consumers_and_subscription tests.integration.test_objective83_governed_inquiry_resolution_loop tests.integration.test_objective84_operator_visible_system_reasoning tests.integration.test_objective86_commitment_enforcement_drift_monitoring_loop tests.integration.test_objective90_cross_policy_conflict_resolution tests.integration.test_objective91_95_execution_control_plane tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective97_recovery_learning_escalation_loop -v`
- `Ran 37 tests ... OK`

## Bounded Notes

This slice keeps recovery-learning influence inside the execution-control plane and operator/state-bus inspectability surfaces. It does not yet project into dedicated autonomy-boundary profile rows or a separate readiness-style downgrade surface outside the execution-control metadata path.

## Known Boundary Conditions

- recovery learning currently persists indefinitely until newer scoped evidence outweighs it; explicit decay or timed expiry is not yet implemented
- operator-assisted success does not yet perform a dedicated reset or reweight operation beyond contributing a new outcome to the profile
- environmental shifts are only observed indirectly through new execution outcomes and readiness/stability posture, not through a standalone recovery-learning reset trigger
- the broader validation pass surfaced that shared readiness artifacts can leak stale state between integration tests unless fixtures rewrite them completely; the Objective 90, 96, and 97 readiness fixtures are now hardened accordingly