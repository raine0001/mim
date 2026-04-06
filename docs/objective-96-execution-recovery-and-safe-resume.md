# Objective 96 - Execution Recovery and Safe Resume

Date: 2026-03-28
Status: complete
Depends On: Objective 90, Objective 91, Objective 92, Objective 93, Objective 94, Objective 95
Target Release Tag: objective-96

## Summary

Objective 96 extends the new execution control plane from visibility and containment into bounded recovery.

Objectives 91 through 95 now give MIM durable traces, persisted intent lineage, orchestration checkpoints, operator overrides, and stability scoring. The remaining gap is that failed, blocked, or operator-paused executions still rely on ad hoc follow-up instead of one governed recovery contract.

Objective 96 adds a recovery and safe-resume loop that inspects the latest trace and orchestration state, decides whether recovery is allowed, and creates a bounded restart, resume, retry, or rollback recommendation without losing causal history.

## Delivered Slice

Objective 96 is now implemented on top of the 91–95 execution control plane.

Delivered behavior:

- durable `ExecutionRecoveryAttempt` persistence linked to `trace_id`, `execution_id`, and `managed_scope`
- durable `ExecutionRecoveryOutcome` persistence that records whether a recovery path actually recovered, failed again, or remained operator-blocked
- recovery evaluation that inspects execution status, orchestration checkpoint state, active overrides, stability posture, and retry pressure
- conflict-aware recovery arbitration that routes recovery decisions through the Objective 90 policy-conflict layer instead of relying on local branch ordering alone
- bounded recovery decisions covering `resume_from_checkpoint`, `retry_current_step`, `restart_execution`, `rollback_and_replan`, `require_operator_resume`, `hard_stop_persisted`, and `no_recovery_available`
- `POST /execution/recovery/evaluate` for inspectable recovery eligibility and recommended next action
- `POST /execution/recovery/attempt` for durable recovery-attempt recording with policy-blocked and accepted outcomes
- `POST /execution/recovery/outcomes/evaluate` and `GET /execution/recovery/outcomes/{trace_id}` for inspectable recovery-outcome learning
- `GET /execution/recovery/{trace_id}` for latest recovery state and attempt history
- trace-integrated recovery events through `recovery_attempted` and `recovery_blocked`
- orchestration and trace metadata updates so accepted recovery attempts remain visible in the control plane
- automatic recovery-state sync from execution feedback updates for failed, blocked, pending-confirmation, and succeeded executions
- state-bus publication of recovery posture under `tod.runtime` so recovery state becomes queryable outside the execution-control endpoint family
- operator-visible recovery snapshot in `/mim/ui/state` through `operator_reasoning.execution_recovery`

Focused validation is green:

- `/home/testpilot/mim/.venv/bin/python -m unittest -v tests.integration.test_objective96_execution_recovery_safe_resume`
- result: PASS (expanded focused lane)

Control-plane regression coverage is also green:

- `/home/testpilot/mim/.venv/bin/python -m unittest -v tests.integration.test_objective91_95_execution_control_plane tests.integration.test_objective96_execution_recovery_safe_resume`
- result: PASS (91–96 control-plane lane)

## Problem Statement

The current control plane can explain what happened to an execution and whether the scope is stable, but it does not yet formalize how to recover when an execution stalls, fails, or is intentionally paused.

Without Objective 96, the system can accumulate durable execution state without turning that state into a consistent next action.

The result is a gap between:

- knowing the trace and orchestration checkpoint
- understanding whether the scope is degraded or blocked
- deciding what bounded recovery action is safe and inspectable

## Goal

Objective 96 should add a first-class recovery loop for governed executions.

The new loop should answer:

- can this execution be safely resumed?
- should it be retried from the current checkpoint or restarted from the beginning?
- does the current stability or override state require operator intervention instead of autonomous recovery?
- if recovery is attempted, how is that attempt recorded so the trace remains causal and auditable?

## In Scope

### 1. Recovery Decision Contract

Objective 96 should define a bounded recovery decision for a trace or execution.

Representative recovery decisions:

- `resume_from_checkpoint`
- `retry_current_step`
- `restart_execution`
- `rollback_and_replan`
- `require_operator_resume`
- `hard_stop_persisted`
- `no_recovery_available`

The decision should be shaped by:

- latest orchestration step and checkpoint state
- execution status and dispatch decision
- active operator overrides
- stability mitigation state
- recent retry pressure and prior recovery attempts

### 2. Durable Recovery State

Objective 96 should persist recovery attempts instead of treating them as transient retries.

Recommended persistence family:

- `ExecutionRecoveryAttempt`

Representative fields:

- `trace_id`
- `execution_id`
- `managed_scope`
- `recovery_decision`
- `recovery_reason`
- `attempt_number`
- `resume_step_key`
- `source`
- `actor`
- `status`
- `result_json`
- `metadata_json`

### 3. Trace-Integrated Recovery Events

Every recovery attempt should extend the execution trace instead of creating a separate hidden lifecycle.

Required trace visibility:

- recovery decision recorded as a causality event
- checkpoint used for the recovery attempt
- whether the recovery was autonomous or operator-mediated
- whether the recovery succeeded, failed, or was blocked by policy

### 4. Safe Resume Rules

Objective 96 should not allow recovery to bypass the control surfaces added in Objectives 94 and 95.

Minimum rules:

- active hard-stop overrides must block autonomous resume
- active pause overrides must require explicit operator resume
- degraded stability can cap recovery to bounded retry or operator review
- retry count and oscillation pressure must remain inspectable and bounded

### 5. Inspectability

Objective 96 should expose a small recovery API surface.

Recommended endpoints:

- `POST /execution/recovery/evaluate`
- `POST /execution/recovery/attempt`
- `GET /execution/recovery/{trace_id}`

Those responses should show:

- latest recovery decision
- latest recovery attempt state
- recovery eligibility reason
- whether operator action is required
- linkage back to the execution trace and orchestration checkpoint

## Out Of Scope

- unconstrained autonomous replay of arbitrary execution history
- bypassing operator overrides or stability guardrails
- general workflow redesign outside the governed execution plane
- silent retry loops without durable recovery state

## Acceptance Criteria

Objective 96 is complete when all are true:

1. failed, blocked, paused, or degraded executions can be evaluated through one explicit recovery contract
2. recovery attempts are persisted durably and linked to `trace_id`
3. recovery decisions append inspectable trace events instead of bypassing the causality graph
4. active overrides and stability mitigation posture shape whether resume is autonomous, bounded, or operator-required
5. recovery state is queryable through dedicated execution control endpoints
6. focused integration coverage proves resume, bounded retry, operator-required resume, and blocked recovery paths