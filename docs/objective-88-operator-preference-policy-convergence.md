# Objective 88 - Operator Preference and Policy Convergence

Date: 2026-03-25
Status: implemented_first_slice_hardened
Depends On: Objective 40, Objective 52, Objective 53, Objective 57, Objective 58, Objective 60, Objective 83, Objective 84, Objective 85, Objective 86, Objective 87
Target Release Tag: objective-88

## Problem Statement

Objective 85 introduced durable operator commitments.

Objective 86 turned those commitments into an active enforcement and drift-monitoring loop.

Objective 87 added terminal outcomes and a bounded learning layer.

The remaining gap is convergence.

MIM can now:

- persist operator commitments
- monitor their enforcement health
- evaluate terminal outcomes
- derive local learning signals from repeated success, ineffectiveness, abandonment, harm, and supersession

But MIM still does not reliably convert repeated outcome patterns into stable behavioral defaults.

Without that convergence layer, the system can keep rediscovering the same operator intent repeatedly instead of becoming more consistent over time.

## Goal

Objective 88 turns repeated commitment outcomes and repeated operator interventions into explicit learned preferences and policy defaults that shape later behavior before the same governance question needs to be asked again.

The objective should close the loop:

- evaluation -> pattern -> preference -> policy

So the system behaves consistently rather than reactively.

## Core Outcome

After Objective 88, MIM should be able to:

- reinforce successful operator-guided behavior patterns
- down-weight repeatedly ineffective commitment patterns
- learn stable operator intent when repeated overrides imply a durable preference
- converge those learned signals into strategy, autonomy, stewardship, and inquiry policy defaults
- explain which learned preferences exist, why they exist, how strong they are, and where they are being applied

## In Scope

### 1. Preference Extraction

Objective 88 should derive bounded learned preferences from repeated lifecycle evidence rather than from one-off events.

Primary evidence families:

- repeated `satisfied` commitment outcomes for the same managed scope or decision family
- repeated `ineffective`, `harmful`, or `abandoned` outcomes for the same managed scope or decision family
- repeated operator overrides that consistently move the system in the same direction
- repeated inquiry answers that express the same practical preference for a scope or action family

Representative examples:

- operator repeatedly defers maintenance in zone A
- operator repeatedly lowers autonomy for a specific remediation class
- repeated successful evidence-gated holds reinforce a caution preference
- repeated ineffective deferrals suppress future deferral preference for the same pattern

The output should be a durable learned-preference object rather than an unstructured note in metadata.

### 2. Policy Convergence

Objective 88 should project learned preferences into downstream policy defaults.

Required convergence targets:

- strategy weight adjustments
- default autonomy-boundary posture for relevant scopes or action families
- stewardship bias and follow-up posture
- inquiry surfacing or suppression thresholds
- maintenance or remediation default shaping where appropriate

This is the point where the system should stop asking the same question every time if the evidence already supports a stable learned preference.

### 3. Conflict Resolution

Objective 88 must explicitly resolve conflicts between:

- two learned preferences for the same scope or commitment family
- operator commitments and longer-lived learned preferences
- recently observed outcomes and older learned preferences
- system-derived convergence signals and direct operator override behavior

At minimum the conflict model should answer:

- which signal wins now
- why it wins
- how long that precedence lasts
- whether the losing signal is weakened, superseded, or just temporarily masked

### 4. Inspectability

Objective 88 must remain operator-visible and auditable.

The operator should be able to inspect:

- the learned preference itself
- the evidence and pattern summary that produced it
- the preference strength or confidence
- the current policy surfaces it influences
- any active conflict and the winning resolution rule

Recommended visibility additions:

- `operator_reasoning.learned_preferences`
- `operator_reasoning.preference_conflicts`
- `conversation_context["operator_preference_summary"]`
- a dedicated operator API for listing and inspecting learned preference profiles

### 5. Preference Decay And Revalidation

Converged policies cannot become permanent stale truth.

Objective 88 should include bounded decay or revalidation behavior so that:

- old preferences weaken if evidence stops reinforcing them
- conflicting fresh evidence can reopen a previously converged preference
- preferences that have gone stale become visible as stale rather than silently controlling behavior forever

## Proposed Persistence Surface

Recommended new durable model family:

- `WorkspaceOperatorLearnedPreferenceProfile`

Expected persisted fields:

- `managed_scope`
- `preference_family`
- `preference_key`
- `preference_status`
- `preference_direction`
- `strength_score`
- `confidence_score`
- `evidence_count`
- `success_count`
- `failure_count`
- `override_count`
- `conflict_state`
- `superseded_by_preference_id`
- `policy_effects_json`
- `evidence_summary_json`
- `metadata_json`
- `created_at`
- `updated_at`
- `expires_at` or equivalent stale-after controls

Optional audit companion:

- `WorkspaceOperatorLearnedPreferenceEvent`

## Proposed Downstream Effects

The converged preference layer should be able to influence at least:

- `core/goal_strategy_service.py`
- `core/autonomy_boundary_service.py`
- `core/stewardship_service.py`
- `core/inquiry_service.py`
- `core/improvement_governance_service.py`
- `core/maintenance_service.py`
- `core/routers/mim_ui.py`

Objective 88 should prefer one shared preference service rather than re-encoding convergence logic independently in every consumer.

Recommended implementation anchor:

- `core/operator_preference_convergence_service.py`

## Policy Questions Objective 88 Must Answer

The implementation should make these questions explicit and inspectable:

- when has a repeated pattern earned promotion from outcome history into stable preference?
- what minimum evidence threshold is required before convergence is allowed?
- how are harmful outcomes weighted relative to successful ones?
- how much operator-direct evidence outweighs system-inferred preference?
- when should an inquiry be suppressed because the preference is now stable?
- when should the system reopen the question because the preference is stale or contradicted?

## Example Behavioral Contract

Given repeated evidence such as:

- commitment family: `defer_maintenance`
- managed scope: `zone_a`
- outcome history: `satisfied`, `satisfied`, `satisfied`
- operator overrides: repeatedly consistent

Objective 88 should be able to produce a learned preference like:

- preference direction: `prefer_deferral_for_zone_a`
- confidence: high
- downstream effects:
  - slightly lower remediation urgency for that scope
  - slightly raise inquiry suppression threshold for the same question family
  - maintain bounded autonomy conservatism until contradictory evidence appears

Likewise, repeated `ineffective` or `harmful` outcomes for the same pattern should converge toward the inverse preference.

## Out Of Scope

- unconstrained free-form persona modeling
- replacing direct operator commitments with opaque learned behavior
- broad UI redesign unrelated to inspectability
- removing safety ceilings or governance review requirements
- general-purpose preference learning outside the operator commitment and governance loop

## Validation Requirements

The first implementation slice now includes:

- learned preference convergence into durable operator preference rows
- scope-level projection into strategy, autonomy, stewardship, and operator-visible UI reasoning
- inspectable preference conflicts
- strength normalization and freshness decay
- scope arbitration so only the strongest actionable learned preference projects at a time
- explicit operator commitment precedence over learned projection while commitments remain active

The next validation bar for this objective is:

- focused conflict arbitration coverage
- stale preference demotion coverage
- adjacent Objective 85/86/87/88 regression lane coverage

Objective 88 should not close without proving a real convergence loop.

Required focused validation cases:

1. repeated successful commitment outcomes reinforce a durable preference
2. repeated ineffective outcomes weaken or invert the same preference
3. repeated operator overrides extract a stable preference even when explicit commitment text differs slightly
4. a converged preference changes downstream strategy scoring
5. a converged preference changes downstream autonomy or stewardship defaults
6. inquiry suppression thresholds reflect a stable learned preference
7. conflicting preferences are surfaced with an explicit winning rule
8. stale or contradicted preferences reopen for revalidation instead of controlling behavior forever

## Exit Criteria

Objective 88 is complete when all are true:

1. repeated commitment outcomes can become durable learned preferences
2. repeated operator overrides can become durable learned preferences
3. converged preferences shape downstream policy defaults across at least strategy, autonomy, stewardship, and inquiry
4. conflicts between learned preferences and newer evidence are explicit and inspectable
5. learned preferences expose strength, confidence, provenance, and applied policy effects
6. stale preferences weaken or reopen under contradictory evidence
7. focused and adjacent regression validation proves the system has become more behaviorally consistent rather than just more stateful
