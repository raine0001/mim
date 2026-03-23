# Objective 80 - Execution Truth Convergence

Date: 2026-03-23
Status: active_planning
Depends On: Objective 75

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

### Slice 80.1 - Execution Truth Packet v1

Extend TOD feedback and canonical publication with a bounded execution-truth payload containing:

- `execution_truth_version`
- `execution_id`
- `expected_duration_ms`
- `actual_duration_ms`
- `deviation_ratio`
- `retry_count`
- `fallback_used`
- `confidence_delta`
- `environment_shift`
- `stability_status`
- `feedback_delay_ms`

### Slice 80.2 - Canonical Projection

Expose the execution-truth payload in the canonical cross-system publication path so the bridge does not rely on ad hoc executor logs or local-only TOD memory.

### Slice 80.3 - MIM Interpretation Hook

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

1. Define `execution_truth_v1` field set and semantics.
2. Separate executor-local diagnostics from canonical shared truth fields.
3. Define freshness and completeness expectations for execution-truth publication.

### Phase 2 - TOD Signal Generation

1. Publish runtime timing, retry, fallback, and drift data into feedback payloads.
2. Ensure TOD computes bounded deviation metrics consistently.
3. Surface execution truth in canonical publication rather than only local process state.

### Phase 3 - Bridge Projection

1. Add canonical bridge fields or companion artifacts for execution truth.
2. Ensure alias and canonical views stay synchronized.
3. Define recoupling checks for execution-truth freshness and completeness.

### Phase 4 - MIM Interpretation

1. Convert execution-truth fields into normalized deviation signals.
2. Feed those signals into constraint, strategy, and improvement services.
3. Preserve a clear line between observed runtime truth and inferred planning conclusions.

### Phase 5 - Validation

1. Add focused integration tests for feedback ingestion and canonical projection.
2. Add bridge-level validation for alias sync and stale-truth rejection.
3. Add at least one end-to-end test showing TOD execution reality changing a MIM-side reasoning input.

## Deliverables

- execution-truth extension for canonical TOD status publication
- shared contract additions for runtime deviation signals
- MIM interpretation rules for execution-truth feedback
- drift-detection logic comparing predicted and actual execution behavior
- integration tests covering publication, bridge transport, and MIM adaptation inputs
- a first baseline report describing the first stable `execution_truth_v1` publication path

## Acceptance Criteria

- TOD publishes canonical execution-truth signals for live executions
- bridge publication preserves those signals without alias drift
- MIM ingests execution-truth signals into reasoning and adaptation surfaces
- simulation-vs-reality drift is explicitly surfaced and queryable
- recoupling gates can fail on execution-truth instability, not just contract mismatch
- the first active slice proves one concrete loop: TOD runtime deviation changes a MIM-side structured signal

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
