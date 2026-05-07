# Objective 83 - Governed Inquiry Resolution Loop

Date: 2026-03-24
Status: implemented
Depends On: Objective 60, Objective 62, Objective 80, Objective 81, Objective 82
Target Release Tag: objective-83

## Summary

Objective 83 turns inquiry from an available side effect into a governed system behavior.

MIM can already:

- detect persistent degradation
- generate inquiry candidates
- accept inquiry answers
- create bounded downstream actions

Objective 83 closes the control gap by formalizing when inquiry should be required, when it should remain optional, when it should be suppressed, what answer paths are allowed to change, and how repeat inquiry should be bounded.

## Implemented Surface

The governed inquiry loop is now implemented in the shared inquiry service and API surface.

Delivered behavior:

- inquiry is decision-governed rather than event-triggered, so trigger signals are evaluated through explicit policy before any row is created
- inquiry generation evaluates an explicit decision state before creating a row
- duplicate open inquiries are suppressed before new rows are created
- recent answered inquiries can be reused during cooldown windows instead of creating duplicates
- low-evidence inquiry candidates are suppressed before persistence
- moderate-evidence candidates can be suppressed when autonomy is already confidently bounded
- generated question payloads expose decision state, evidence score, cooldown status, and allowed answer effects
- answer responses expose decision state, allowed downstream effect classes, and a state-delta summary
- generate responses include inspectable suppressed and reused decisions even when no question row is created

## Why Objective 83 Matters

Without a governed inquiry policy, inquiry can become noisy, inconsistent, and hard to trust.

The system is now at the point where self-generated questions are no longer just observability artifacts. They influence stewardship, workspace follow-up, autonomy posture, and improvement flow. That means inquiry needs an explicit policy layer with inspectable rules and anti-thrashing controls.

## Objective 83 Scope

### 1. Inquiry Decision Policy

Inquiry should become a first-class decision with explicit policy states.

Target decision states:

- `required_for_progress`
- `optional_for_refinement`
- `suppressed_low_evidence`
- `suppressed_high_confidence_autonomy`
- `deferred_due_to_cooldown`

The decision policy should combine:

- evidence quality
- managed-scope degradation severity
- recent inquiry history for the same scope or trigger
- autonomy boundary state
- recent valid answers that can still be reused safely

### 2. Answer Impact Contract

Inquiry answers should map to explicit downstream effect classes rather than ad hoc local behavior.

Target answer impact outcomes:

- `rescan`
- `tighten_tracking`
- `propose_improvement`
- `lower_autonomy`
- `escalate_to_operator`
- `suppress_repeat_inquiry_temporarily`
- `no_action`

Each answered inquiry should record:

- selected path
- allowed downstream effect
- applied downstream effect
- why that effect was allowed
- whether the effect changed system state materially

### 3. Anti-Thrashing Controls

Objective 83 should prevent inquiry spam and repeated low-value loops.

Required controls:

- per-scope inquiry cooldown
- duplicate inquiry suppression
- minimum evidence threshold before inquiry is surfaced
- answer reuse when a recent answer is still valid
- bounded suppression window after repeated unchanged conditions

These controls should be applied before generating new open inquiry rows so the system suppresses repeated noise instead of creating rows and cleaning them up later.

### 4. Inspectability

Inquiry decisions should be inspectable even when no question is surfaced.

Inspectability should answer:

- why a question was asked
- why a question was suppressed
- what evidence crossed or failed the threshold
- what answer paths were offered
- what changed after the answer
- whether prior answers or cooldowns were reused

Target inspectability additions:

- inquiry decision payload includes policy state and suppression reason
- history/read-model output includes cooldown and reuse metadata
- answer output includes applied downstream effect and state delta summary

### 5. Regression Proof

Objective 83 should add integration coverage for the governed inquiry contract.

Required regression cases:

1. persistent degradation with enough evidence leads to `required_for_progress`
2. weak evidence leads to suppression instead of open inquiry creation
3. repeated unchanged condition respects cooldown and avoids spam
4. recent valid answer is reused instead of generating duplicate inquiry
5. answer path changes downstream behavior predictably
6. suppression and reuse remain inspectable in read models and API responses

## Proposed Policy Inputs

The governed inquiry policy should evaluate at least these inputs:

- evidence score
- degradation severity
- trigger type
- managed scope
- recent inquiry count for the same dedupe domain
- recent answer recency
- recent answer validity
- autonomy level
- execution-truth governance state
- stewardship follow-up state

## Proposed Output Contract

The inquiry policy should return a compact decision object such as:

```json
{
  "decision_state": "required_for_progress",
  "reason": "persistent_degradation_with_actionable_uncertainty",
  "evidence_score": 0.84,
  "cooldown_active": false,
  "duplicate_suppressed": false,
  "recent_answer_reused": false,
  "allowed_answer_effects": [
    "rescan",
    "tighten_tracking",
    "propose_improvement",
    "no_action"
  ]
}
```

## Expected System Outcome

After Objective 83, inquiry should no longer feel like scattered follow-up logic spread across stewardship and inquiry code paths.

Instead, it should behave like a governed loop:

- detect uncertainty
- decide whether inquiry is warranted
- expose bounded answer paths
- apply explicit downstream effects
- suppress redundant repeats
- explain what happened afterward

## 2026-03-24 Validation

Focused Objective 83 lane:

- `tests.integration.test_objective83_governed_inquiry_resolution_loop` -> `Ran 4 tests ... OK`

Focused cases now prove:

- required inquiry decisions expose policy state and bounded answer-effect contracts
- cooldown reuse defers duplicate inquiry creation when a recent valid answer still applies
- low-value inquiry candidates are suppressed before persistence
- partial improvement after a previously required inquiry does not retrigger a fresh inquiry when the remaining degradation falls below the surfacing threshold

Adjacent inquiry regression lane:

- `tests.integration.test_objective60_stewardship_inquiry_followup`
- `tests.integration.test_objective62_inquisitive_question_loop`
- `tests.integration.test_objective80_execution_truth_inquiry_hook`
- combined result -> `Ran 5 tests ... OK`

Cross-surface sanity chain:

- persistent degradation -> required inquiry -> bounded answer effect -> inspectable workspace/stewardship/governance follow-through -> no duplicate inquiry on the next governed pass
- recorded in `docs/objective-83-promotion-readiness-report.md`

## Exit Criteria

Objective 83 is complete when all are true:

1. inquiry generation uses an explicit decision-state policy
2. repeat inquiry is bounded by cooldown and duplicate suppression
3. recent valid answers can suppress or reuse inquiry safely
4. answer paths apply only allowed downstream effect classes
5. inspectability surfaces both asked and suppressed inquiry decisions
6. regression coverage proves required, optional, suppressed, deferred, and reused paths
