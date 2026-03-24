# Objective 80 - Execution Truth Convergence

Date: 2026-03-23
Status: in_progress
Depends On: Objective 75
Target Release Tag: objective-80
Target Schema Version: 2026-03-12-68

## Summary

Objective 80 makes TOD execution reality a first-class input to MIM reasoning.
MIM remains the owner of intent, contract, and adaptation policy. TOD becomes the primary source of execution truth. The bridge carries those signals in a canonical form that MIM can trust.

Objective 75 established the interface baseline. Objective 80 starts from the assumption that TOD is now ready to publish stable execution-truth signals over that recoupled path.

This objective exists to close the gap between:

- what MIM expects will happen
- what TOD actually experiences at runtime
- what the shared bridge publishes as system truth

## Ownership Model

- MIM: defines the contract, interpretation rules, and adaptation logic
- TOD: generates execution-truth signals from real runtime behavior
- Bridge: guarantees canonical publication and alignment across the two systems

## Current Baseline

Objective 80 should build on existing execution-feedback plumbing already present in MIM:

- Objective 22 established the execution handoff and feedback channel
- `POST /gateway/capabilities/executions/{execution_id}/feedback` already accepts `status`, `runtime_outcome`, `recovery_state`, `correlation_json`, and `feedback_json`
- current `runtime_outcome` mapping already covers `executor_unavailable`, `guardrail_blocked`, `retry_in_progress`, `fallback_used`, `recovered`, and `unrecovered_failure`
- execution feedback history is persisted on the execution record with actor, transition, runtime outcome, recovery state, and arbitrary feedback payloads
- MIM already has downstream deviation concepts in stewardship and inquiry flows (`deviation_signals`), but they are not yet driven by canonical TOD execution-truth publication

This means Objective 80 is not starting from zero. The missing piece is convergence: making execution-truth structured, canonical, bridge-visible, and reasoning-relevant.

## Goals

### 1. Real Execution Signal Fidelity

TOD should publish richer execution-truth signals such as:

- execution duration vs expected duration
- retries and fallback usage
- deviation from expected path
- confidence changes during execution
- environment shifts or runtime drift

Example signal shape:

```json
{
  "execution_id": "...",
  "actual_duration_ms": 1320,
  "expected_duration_ms": 900,
  "deviation_ratio": 0.46,
  "retry_count": 2,
  "fallback_used": true,
  "confidence_delta": -0.12,
  "environment_shift": true
}
```

### 2. Feedback Into MIM Reasoning

MIM should use execution truth to update:

- constraint weights
- autonomy boundaries
- strategy scoring
- improvement proposals
- future execution risk assessment

### 3. Simulation-vs-Reality Drift Detection

Objective 80 should measure where MIM planning and TOD execution diverge.

Examples:

- simulated safe path required retries in practice
- predicted fast path became latency-heavy
- expected stable environment experienced drift

These differences should become structured learning signals rather than one-off anecdotes.

### 4. Stronger Recoupling Semantics

Extend recoupling beyond contract compatibility to include execution-quality thresholds such as:

- acceptable deviation bands
- repeated runtime instability detection
- delayed feedback loop thresholds
- fallback saturation or chronic retry pressure

## First Slice

The first implementation slice should be narrow and measurable.

### Slice 80.1 - Execution Truth Contract Surface

Implemented canonical execution-truth publication on the existing Objective 22 feedback path:

- authoritative path: `POST /gateway/capabilities/executions/{execution_id}/feedback`
- authoritative readback: `GET /gateway/capabilities/executions/{execution_id}/feedback`
- canonical top-level payload: `execution_truth`
- contract marker: `execution_truth.contract = execution_truth_v1`

`execution_truth_v1` is intentionally narrow and currently includes:

- `execution_id`
- `capability_name`
- `expected_duration_ms`
- `actual_duration_ms`
- `duration_delta_ratio`
- `retry_count`
- `fallback_used`
- `runtime_outcome`
- `environment_shift_detected`
- `simulation_match_status`
- `truth_confidence`
- `published_at`

MIM now interprets this payload into structured deviation signals:

- `execution_slower_than_expected`
- `retry_instability_detected`
- `fallback_path_used`
- `simulation_reality_mismatch`
- `environment_shift_during_execution`

Those signals are persisted on execution feedback and carried into cross-domain reasoning as `workspace_state.execution_truth_summary` and `reasoning.execution_truth_influence`.

### Slice 80.2 - Canonical Projection

Implemented canonical bridge projection as a shared companion artifact:

- canonical shared file: `runtime/shared/TOD_EXECUTION_TRUTH.latest.json`
- legacy alias file: `runtime/shared/TOD_execution_truth.latest.json`
- canonical read model endpoint: `GET /gateway/capabilities/executions/truth/latest`

The bridge projection now republishes recent `execution_truth_v1` payloads, their derived deviation signals, and a compact summary so TOD and bridge tooling can consume one stable shared shape instead of scraping executor-local state.

### Slice 80.3 - MIM Interpretation Hook

Implemented a first downstream consumer beyond reasoning summary through the inquiry loop.

Execution-truth deviations can now generate bounded inquiry questions with trigger type `execution_truth_runtime_mismatch`. Those questions can:

- create a bounded improvement proposal for execution-truth workflow review
- request additional observation via a rescan proposal
- keep monitoring without changing policy yet

This keeps 80.3 in the interpretation and operator-guidance lane rather than jumping directly to autonomy-boundary or strategy automation.

### Slice 80.4 - Stewardship Follow-Up Hook

Implemented a second bounded downstream consumer through environment stewardship.

Stewardship assessment now ingests scoped `execution_truth_v1` deviations alongside workspace-state drift signals. Those execution-truth deviations are surfaced in:

- stewardship assessment `execution_truth_summary`
- cycle summary and verification `execution_truth_signal_count` / `execution_truth_signal_types`
- stewardship history/read-model follow-up types
- stewardship-generated inquiry evidence when persistent degradation is rooted in runtime execution truth

This keeps the execution-truth signal on the adaptation side without directly mutating autonomy boundaries or strategy weights.

### Slice 80.5 - Strategy Scoring Hook

Implemented a bounded strategy-scoring extension using the existing execution-truth reasoning summary.

Strategy ranking now consumes execution-truth influence as an additional weighted signal derived from:

- `execution_count`
- `deviation_signal_count`
- derived execution-truth `signal_types`

This currently affects strategy weighting rather than strategy generation shape. Runtime mismatch, retry pressure, fallback dependence, and environment-shift signals can now raise or lower strategic preference in explainable ways, especially for stabilization-oriented strategies.

### Slice 80.6 - Improvement Prioritization Hook

Implemented a bounded improvement-governance extension using the same execution-truth summary path and inquiry evidence.

Execution-truth-originated improvement proposals now preserve the triggering execution metadata needed for ranking, and backlog refresh now includes a bounded execution-truth priority influence derived from:

- `execution_count`
- `deviation_signal_count`
- derived execution-truth `signal_types`
- whether a proposal was explicitly created from an execution-truth inquiry path

This affects improvement prioritization and reasoning only. It does not auto-approve changes or bypass the existing governance policy.

### Slice 80.7 - Adaptation Surface Hook

Extended the same scoped execution-truth summary path into the remaining bounded adaptation surfaces that were still only reading cross-domain context indirectly.

Fresh, scope-matched execution truth now affects or surfaces in:

- constraint evaluation decisions and explanation output
- autonomy-boundary review visibility and read-model output
- maintenance-cycle drift detection and run outcomes
- stewardship selected actions, verification, and follow-up recommendation state

This keeps the execution-truth influence explainable and bounded. Constraint decisions can now require replanning or conditioned execution when fresh runtime truth shows mismatch or instability, while autonomy remains review-oriented rather than silently self-escalating policy.

Add a first-pass MIM interpreter that converts execution truth into structured signals such as:

- `execution_deviation_high`
- `execution_retry_pressure`
- `execution_fallback_dependence`
- `execution_feedback_delay`
- `execution_environment_shift`

Those signals should be consumable by:

- autonomy-boundary review
- strategy scoring
- improvement recommendations
- stewardship/inquiry follow-up logic

## Task List

### Phase 1 - Contract Definition

1. Define `execution_truth_v1` field set and semantics. Completed for the first contract surface.
2. Separate executor-local diagnostics from canonical shared truth fields. Completed by using top-level `execution_truth` instead of burying the contract in arbitrary feedback keys.
3. Define freshness and completeness expectations for execution-truth publication. Freshness now depends on `published_at`; completeness remains limited to the initial 80.1 field set.

### Phase 2 - TOD Signal Generation

1. Publish runtime timing, retry, fallback, and drift data into feedback payloads.
2. Ensure TOD computes bounded deviation metrics consistently.
3. Surface execution truth in canonical publication rather than only local process state.

### Phase 3 - Bridge Projection

1. Add canonical bridge fields or companion artifacts for execution truth. Completed with `TOD_EXECUTION_TRUTH.latest.json`.
2. Ensure alias and canonical views stay synchronized. Completed with the lowercase alias and alias-sync validation script.
3. Define recoupling checks for execution-truth freshness and completeness. Completed for 80.2 with a focused bridge validation script.

### Phase 4 - MIM Interpretation

1. Convert execution-truth fields into normalized deviation signals. Completed for the five initial signal types.
2. Feed those signals into constraint, strategy, and improvement services. Now complete for the current bounded adaptation set through cross-domain reasoning ingestion, inquiry-loop consumption, stewardship follow-up consumption, strategy scoring influence, improvement prioritization influence, constraint evaluation, autonomy-boundary review visibility, and maintenance-cycle prioritization.
3. Preserve a clear line between observed runtime truth and inferred planning conclusions. Completed for 80.1 by keeping canonical truth and interpreted reasoning outputs separate.

### Phase 5 - Validation

1. Add focused integration tests for feedback ingestion and canonical projection. Completed with the Objective 80.1 execution-truth contract-surface test.
2. Add bridge-level validation for alias sync and stale-truth rejection. Completed for 80.2.
3. Add at least one end-to-end test showing TOD execution reality changing a MIM-side reasoning input. Completed for 80.1 through cross-domain reasoning context ingestion.

## Deliverables

- execution-truth extension for canonical TOD status publication
- shared contract additions for runtime deviation signals
- MIM interpretation rules for execution-truth feedback
- drift-detection logic comparing predicted and actual execution behavior
- integration tests covering publication, bridge transport, and MIM adaptation inputs
- a first baseline report describing the first stable `execution_truth_v1` publication path

## Acceptance Criteria

- TOD publishes canonical execution-truth signals for live executions. Implemented for the first contract surface.
- bridge publication preserves those signals without alias drift.
- bridge publication preserves those signals without alias drift. Implemented for the canonical and legacy execution-truth artifacts.
- MIM ingests execution-truth signals into reasoning and bounded adaptation surfaces. Implemented for reasoning, inquiry, stewardship, strategy scoring, improvement prioritization, constraint evaluation, autonomy review visibility, and maintenance prioritization.
- simulation-vs-reality drift is explicitly surfaced and queryable. Implemented through `simulation_reality_mismatch`.
- recoupling gates can fail on execution-truth instability, not just contract mismatch.
- the first active slice proves one concrete loop: TOD runtime deviation changes a MIM-side structured signal. Implemented in the focused Objective 80.1 integration test.

## 80.1 Evidence

- contract surface defined in MIM schema/model space
- canonical publication path remains the gateway feedback endpoint; no parallel execution-truth endpoint was introduced
- MIM transforms canonical execution truth into structured deviation signals during feedback ingestion
- cross-domain reasoning now carries execution-truth influence explicitly
- focused integration proof: `tests/integration/test_objective80_execution_truth_contract_surface.py`

## 80.2 Evidence

- canonical execution-truth bridge endpoint: `GET /gateway/capabilities/executions/truth/latest`
- canonical shared artifact: `runtime/shared/TOD_EXECUTION_TRUTH.latest.json`
- legacy alias artifact: `runtime/shared/TOD_execution_truth.latest.json`
- alias-sync validator: `scripts/check_tod_execution_truth_alias_sync.sh`
- freshness/completeness validator: `scripts/validate_tod_execution_truth_bridge.sh`
- focused bridge projection proof: `tests/integration/test_objective80_execution_truth_bridge_projection.py`

## 80.3 Evidence

- downstream consumer: inquiry generation via `POST /inquiry/questions/generate`
- new trigger type: `execution_truth_runtime_mismatch`
- bounded answer effect: create execution-truth workflow improvement proposal
- focused inquiry proof: `tests/integration/test_objective80_execution_truth_inquiry_hook.py`

## 80.4 Evidence

- downstream consumer: stewardship assessment via `POST /stewardship/cycle`
- scoped execution-truth deviations now appear in stewardship assessment, summary, verification, and history surfaces
- stewardship-generated inquiry evidence now exposes execution-truth signal count and types when runtime truth is the degradation source
- focused stewardship proof: `tests/integration/test_objective80_execution_truth_stewardship_hook.py`

## 80.5 Evidence

- downstream consumer: strategy scoring via `POST /strategy/goals/build`
- strategy ranking now includes execution-truth signal count, signal types, and a bounded execution-truth strategy weight
- strategy reasoning now exposes execution-truth influence and rationale without mutating autonomy-boundary policy directly
- focused strategy proof: `tests/integration/test_objective80_execution_truth_strategy_scoring.py`

## 80.6 Evidence

- downstream consumer: improvement backlog prioritization via `POST /improvement/backlog/refresh`
- execution-truth inquiry-created proposals now preserve execution id and signal types for bounded review
- improvement backlog reasoning now exposes execution-truth influence, signal types, and a bounded priority weight
- focused prioritization proof: `tests/integration/test_objective80_execution_truth_improvement_prioritization.py`

## 80.7 Evidence

- downstream consumers: `POST /constraints/evaluate`, `POST /autonomy/boundaries/recompute`, `POST /maintenance/cycle`, and `POST /stewardship/cycle`
- constraint decisions now consume scoped execution-truth freshness and signal type evidence
- autonomy boundary profiles now expose `execution_truth_influence` as review-oriented evidence
- maintenance runs now record execution-truth drift signals and outcome counts
- stewardship cycles now emit `execution_truth_review_recommended` actions plus `execution_truth_followup_recommended` summary and verification fields
- focused regression coverage:
  - `tests/integration/test_objective80_execution_truth_constraint_influence.py`
  - `tests/integration/test_objective80_execution_truth_adaptation_surfaces.py`

## Immediate Start Condition

Objective 80 can start now because Objective 75 already established:

- stable shared truth export
- trustworthy canonical status publication
- proven recoupling logic
- catch-up status that no longer fakes success

The first practical move is to define and publish `execution_truth_v1` on top of the existing Objective 22 feedback channel.

## Notes

- Objective 80 is cross-system by design.
- It is TOD-heavy for signal generation, MIM-heavy for interpretation, and bridge-critical for truth alignment.
- It should not be implemented purely in TOD or purely in MIM, because either approach would recreate the split Objective 75 just resolved.
