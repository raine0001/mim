# Objective 89 - Proposal Policy Convergence

Date: 2026-03-25
Status: promoted_verified
Depends On: Objective 57, Objective 60, Objective 74, Objective 75, Objective 80, Objective 83, Objective 84, Objective 88.2, Objective 88.3, Objective 88.4
Target Release Tag: objective-89

## Problem Statement

Objective 88.2 taught MIM to remember which proposal types win or lose arbitration.

Objective 88.3 propagated that learning into strategy, commitment monitoring, stewardship follow-up preference, and inquiry answer-path weighting.

Objective 88.4 carried the same signal into autonomy review visibility.

The remaining gap is policy convergence.

MIM can now react to arbitration history after proposals have already been emitted and judged, but it still treats that learning primarily as downstream weighting.

Without a convergence layer, MIM keeps rediscovering the same proposal-shape corrections after TOD has already had to arbitrate them.

## Goal

Objective 89 should convert repeated proposal arbitration patterns into bounded, durable proposal-policy behavior that shapes proposals before TOD has to keep correcting the same pattern.

The objective should close the loop:

- proposal emitted -> arbitration result -> repeated pattern -> converged proposal policy -> pre-arbitration shaping

## Core Outcome

After Objective 89, MIM should be able to:

- remember stable proposal-shape preferences per scope or proposal family
- suppress or downgrade repeatedly losing proposal shapes before emission
- bias proposal generation defaults toward shapes that repeatedly align with TOD governance
- surface explicit convergence and divergence signals when TOD repeatedly overrides the same kind of proposal
- explain exactly why a proposal was deprioritized or suppressed before emission

## In Scope

### 1. Proposal-Shape Preference Memory

Objective 89 should introduce a durable, inspectable representation of proposal-shape preferences derived from repeated arbitration outcomes.

Representative evidence families:

- repeated losses for the same proposal type in the same scope
- repeated wins or successful merges for the same proposal family
- repeated TOD overrides that reshape proposals in the same direction
- repeated downstream acceptance or rejection tied to the same proposal shape

### 2. Scope-Aware Pre-Arbitration Shaping

Objective 89 should project converged proposal policy into proposal generation itself.

Required shaping behaviors:

- pre-arbitration suppression of repeatedly losing proposal shapes when confidence is high enough
- bounded downgrade of weak or unstable proposal families
- scope-aware preference for proposal forms that repeatedly survive arbitration in the same zone or managed scope
- proposal defaults that better match observed TOD governance posture

### 3. Convergence Confidence And Noise Guardrails

Objective 89 must not overfit to short-term noise.

The convergence model should answer:

- what minimum evidence threshold is required before suppression or downgrade is allowed?
- how is recent evidence weighted against older evidence?
- when should a losing pattern remain advisory rather than policy-active?
- how does the system reopen a previously converged policy when new evidence diverges?

Guardrails should include:

- freshness decay
- minimum sample thresholds
- bounded policy effect size
- explicit stale or weak-signal states

### 4. Divergence Signaling

Objective 89 should surface when MIM proposal behavior and TOD governance are still diverging.

Representative inspectable signals:

- repeated-loss pattern count
- active suppression or downgrade reasons
- convergence confidence
- last contradictory arbitration signal
- whether a proposal was emitted despite an active negative policy signal

### 5. Inspectability

Objective 89 must keep proposal policy shaping operator-visible.

Recommended inspectability surfaces:

- proposal payload metadata describing pre-arbitration suppression or downgrade decisions
- a queryable proposal-policy API surface
- operator reasoning summary of active proposal convergence signals
- explicit rationale fields such as `why_this_proposal_was_deprioritized_before_emission`

## Proposed Persistence Surface

Recommended durable model family:

- `WorkspaceProposalPolicyPreferenceProfile`

Expected fields:

- `managed_scope`
- `proposal_family`
- `proposal_type`
- `policy_state`
- `preference_direction`
- `convergence_confidence`
- `sample_count`
- `win_count`
- `loss_count`
- `merge_count`
- `suppression_threshold_met`
- `policy_effects_json`
- `evidence_summary_json`
- `metadata_json`
- `created_at`
- `updated_at`
- stale or decay controls

## Proposed Downstream Effects

Objective 89 should influence at least:

- proposal generation defaults before ranking
- workspace proposal refresh and ordering
- proposal suppression or downgrade metadata
- operator-visible proposal reasoning in MIM UI
- any later strategy or inquiry surfaces that consume pre-shaped proposal sets

Implemented anchor:

- `core/proposal_policy_convergence_service.py`

## Implemented Surface

Objective 89 now adds a bounded, durable proposal-policy layer derived from repeated proposal arbitration outcomes.

Delivered surface:

- durable `WorkspaceProposalPolicyPreferenceProfile` rows keyed by scope and proposal type
- policy convergence logic in `core/proposal_policy_convergence_service.py`
- pre-arbitration proposal shaping in `core/routers/workspace.py` during proposal priority refresh
- inspectable proposal payload metadata under `proposal_policy_convergence`
- queryable workspace API surface at `/workspace/proposals/policy-preferences`
- operator-visible reasoning snapshot in `/mim/ui/state` under `operator_reasoning.proposal_policy`

Bounded policy behavior:

- repeated losses can converge to `downgraded` or `suppressed`
- repeated wins can converge to `preferred`
- contradictory fresh evidence reopens a converged policy instead of locking it in
- stale evidence becomes advisory and stops applying shaping pressure

Validation:

1. `tests.integration.test_objective89_proposal_policy_convergence`
2. Adjacent arbitration/policy lane on `127.0.0.1:18001`:
	`tests.integration.test_objective88_2_proposal_arbitration_learning`
	`tests.integration.test_objective88_3_proposal_arbitration_learning_propagation`
	`tests.integration.test_objective88_4_proposal_arbitration_learning_autonomy_visibility`
	`tests.integration.test_objective89_proposal_policy_convergence`

Readiness artifact:

- `docs/objective-89-promotion-readiness-report.md`

## Out Of Scope

- removing TOD arbitration itself
- irreversible proposal suppression without inspectable evidence
- non-bounded autonomous rewriting of proposal content
- unconstrained global policy learning across unrelated scopes

## Validation Requirements

Objective 89 should not close without proving a real pre-arbitration convergence loop.

Required focused validation cases:

1. repeated losing proposal shapes converge toward bounded suppression or downgrade
2. repeated winning proposal shapes reinforce proposal defaults for the matching scope
3. weak-sample or stale evidence remains advisory rather than policy-active
4. proposal payloads expose why a proposal was deprioritized before emission
5. contradictory fresh evidence weakens or reopens an existing proposal policy preference
6. scope-aware shaping stays bounded and does not bleed into unrelated scopes

## Exit Criteria

Objective 89 is complete when all are true:

1. repeated arbitration evidence can become a durable proposal policy preference
2. proposal generation is shape-aware before TOD arbitration rather than only after it
3. active suppression or downgrade decisions remain inspectable and reversible
4. convergence confidence, decay, and divergence are explicit
5. focused and adjacent validation prove MIM is proposing in a way that better aligns with repeated TOD governance outcomes
