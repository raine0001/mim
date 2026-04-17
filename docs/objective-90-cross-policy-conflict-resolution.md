# Objective 90 - Cross-Policy Conflict Resolution

Date: 2026-03-26
Status: implemented
Depends On: Objective 57, Objective 58, Objective 60, Objective 80, Objective 83, Objective 84, Objective 85, Objective 86, Objective 87, Objective 88, Objective 88.2, Objective 88.3, Objective 88.4, Objective 89
Target Release Tag: objective-90

## Completed Slice

Objective 90 is implemented across four bounded seams in the current repo baseline, but the broader contradictory-reopen inquiry matrix remains a deferred follow-up rather than a closed objective.

The current baseline for Objective 90 now treats TOD execution readiness as a required policy input rather than a soft advisory signal.

That baseline adds:

- mandatory readiness evaluation before governed execution binding
- readiness policy outcomes of `allow`, `degrade`, or `block`
- readiness-aware proposal shaping and conflict inspectability
- readiness state propagation into execution trace, execution feedback, and state-bus snapshots/events

Objective 90A is `Proposal-Shaping Conflict Arbitration`.

Objective 90A adds:

- durable `WorkspacePolicyConflictProfile` and `WorkspacePolicyConflictResolutionEvent` persistence
- `core/policy_conflict_resolution_service.py` with explicit precedence for proposal shaping conflicts
- proposal-level conflict arbitration between active operator commitments, recent execution-truth governance, proposal-policy convergence, and learned preferences
- proposal-level readiness shaping so stale, degraded, or blocked execution posture can reduce proposal pressure before selection
- bounded hold-down metadata with `oscillation_count` and `cooldown_until`
- proposal API inspectability through `/workspace/proposals/policy-conflicts`
- proposal payload inspectability through `policy_conflict_resolution`
- operator-visible MIM UI inspectability through `operator_reasoning.conflict_resolution`

Current precedence in the implemented seams is explicit and bounded:

1. active operator commitment
2. execution readiness when TOD reports degraded or blocked posture for the same scope
3. recent execution-truth governance
4. commitment outcome learning where applicable
5. proposal-policy convergence or proposal-arbitration autonomy review for the specific decision family
6. learned preference

Objective 90A intentionally landed on the highest-value seam first: when a lower-precedence proposal preference would otherwise increase proposal pressure, a stronger scoped commitment or fresh governance posture can now win visibly and cap or downgrade the proposal before selection.

Objective 90B is `Stewardship and Autonomy Conflict Arbitration`.

Objective 90B adds:

- generalized decision-family handling in `core/policy_conflict_resolution_service.py`
- stewardship auto-execution arbitration between active operator commitments, execution-truth governance, learned preference pressure, and current autonomy-boundary posture
- stewardship auto-execution arbitration between active operator commitments, execution readiness, execution-truth governance, learned preference pressure, and current autonomy-boundary posture
- autonomy-boundary arbitration between active operator commitments, execution readiness, execution-truth governance, commitment outcomes, learned preferences, and proposal-arbitration autonomy review
- cooldown hold-down re-entry handling with contradictory-fresh-evidence reopen when newer higher-precedence evidence arrives
- cooldown-expiry release behavior so previously stabilized conflict outcomes can return to current policy pressure instead of remaining stuck
- persisted conflict profiles and events for `stewardship_auto_execution` and `autonomy_boundary`
- downstream inspectability on stewardship cycle payloads and autonomy-boundary adaptation reasoning

Current autonomy-resolution semantics are intentionally inspectable rather than flattened.

- the persisted conflict winner can remain `learned_preference` when the learned policy wins scope arbitration on effective strength
- an active operator commitment can still appear as a higher-authority candidate with advisory posture when it is temporarily masking the learned posture downstream
- the resolved autonomy boundary can therefore stay at the currently enforced operator-required posture even when the candidate list still shows a stronger permissive operator commitment snapshot

Objective 90C is `Governed Inquiry Answer-Path Arbitration`.

Objective 90C adds:

- inquiry answer-path conflict arbitration between active operator commitments and proposal-arbitration path learning for the same scope
- explicit reuse of `WorkspacePolicyConflictProfile` and `WorkspacePolicyConflictResolutionEvent` for `governed_inquiry_answer_path`
- inspectable answer-path masking and preferred-path ordering on governed inquiry payloads instead of hidden ranking overrides
- inquiry `allowed_answer_effects` that now reflect the winning policy surface while keeping losing paths visible for operator review

Objective 90D is `Governed Inquiry Decision-State Arbitration`.

Objective 90D adds:

- inquiry decision-state conflict arbitration for active operator commitment suppression, high-confidence autonomy suppression, and recent-answer cooldown hold behavior
- explicit reuse of `WorkspacePolicyConflictProfile` and `WorkspacePolicyConflictResolutionEvent` for `governed_inquiry_decision_state`
- dedupe-key scoped conflict profiles for inquiry families that do not carry an explicit managed scope, so cooldown and suppression outcomes do not bleed across unrelated inquiry runs
- inspectable `decision_policy_conflict_resolution` metadata on governed inquiry decisions and question payloads

Objective 90D closes the bounded inquiry reopen matrix used by the current governance lane.

- it covers suppression and cooldown-hold semantics for governed inquiry decision creation
- it reopens previously stabilized inquiry suppression when stronger contradictory fresh evidence arrives for low-evidence, autonomy-suppression, and cooldown-held branches
- it keeps inquiry decision arbitration scope-local so unrelated autonomy or inquiry state does not bleed into the current scope

Focused Objective 90 validation is green on the full integration lane:

- `/home/testpilot/mim/.venv/bin/python -m unittest -v tests.integration.test_objective90_cross_policy_conflict_resolution`
- result: PASS (`11/11`)

## Remaining Follow-Up Contract

Objective 90 is not fully complete yet.

The deferred remainder is the wider contradictory-reopen inquiry matrix beyond the bounded governance-lane reopen paths already covered by Objective 90D.

That remaining follow-up should stay explicit until it lands in a separate bounded slice.

Required follow-up contract:

- broaden contradictory-fresh-evidence reopen coverage beyond the current low-evidence, autonomy-suppression, and cooldown-held governance-lane branches
- validate that wider inquiry-family reopen behavior stays scope-local and inspectable when multiple contradictory signals arrive across adjacent inquiry runs
- document the focused validation lane for the broader inquiry-matrix expansion instead of silently treating the current bounded seam as full closure

Until that follow-up lands, Objective 90 should be treated as implemented and green in the current bounded slice, not fully complete.

## Problem Statement

Objective 85 introduced durable operator commitments.

Objective 80 and Objective 81 introduced execution-truth governance.

Objective 88 introduced operator preference convergence.

Objective 88.2 through 88.4 introduced proposal arbitration learning and bounded downstream propagation.

Objective 89 introduced proposal-policy convergence that shapes behavior before arbitration.

The next gap is no longer policy learning in isolation.

The next gap is policy disagreement.

MIM now has multiple policy-like influences that can legitimately point in different directions at the same time:

- operator commitments
- execution readiness
- execution-truth governance
- proposal-policy convergence
- operator preference convergence
- stewardship preference and follow-up shaping
- inquiry answer-path weighting
- autonomy-boundary adaptation

Without an explicit conflict-resolution layer, MIM risks hidden precedence rules, unstable oscillation between competing policies, and operator-visible behavior that is hard to explain.

## Goal

Objective 90 should introduce a bounded, inspectable cross-policy conflict-resolution layer so MIM can detect when policy surfaces disagree, resolve those disagreements coherently by scope, and explain why one policy signal prevailed over another.

The objective should close the next loop:

- multiple policy signals -> conflict detection -> precedence or arbitration -> bounded resolved policy outcome -> inspectable downstream behavior

## Core Outcome

After Objective 90, MIM should be able to:

- detect when multiple policy surfaces are shaping the same scope or decision in incompatible ways
- apply explicit precedence and tie-break rules instead of implicit last-writer-wins behavior
- arbitrate conflicts differently by scope when the same policies should not dominate everywhere
- surface operator-visible reasoning for why a winning policy prevailed
- reduce oscillation between competing policy surfaces with bounded suppression and cooldown behavior

## In Scope

### 1. Policy Conflict Detection

Objective 90 should introduce a durable and inspectable representation of cross-policy conflict events.

Representative conflict families:

- operator commitment says defer or require evidence while proposal-policy convergence says prefer or suppress differently
- execution-truth governance says monitor-only while stewardship or strategy shaping says act now
- execution readiness says degrade or block while execution-truth or proposal policy still prefers progress
- operator preference convergence says reinforce a direction that conflicts with recent execution-truth or proposal-policy evidence
- inquiry answer-path weighting favors a path that contradicts a currently active operator commitment or execution-truth posture

Required conflict signals:

- conflicting policy sources
- affected scope and decision family
- severity and confidence of the disagreement
- whether the conflict is active, advisory, stale, or resolved

### 2. Precedence And Tie-Break Rules

Objective 90 should make policy precedence explicit.

Required questions the system must answer:

- when does an operator commitment outrank learned policy?
- when does recent execution truth override stale learned preference?
- when does execution readiness block or degrade execution even when another policy surface is permissive?
- when should proposal-policy convergence be advisory because a stronger governance surface is active?
- how are ties resolved when multiple learned policies have similar confidence but opposite directional pressure?

The outcome should be a bounded arbitration layer rather than one-off hard-coded overrides spread across services.

### 3. Scope-Aware Policy Arbitration

Objective 90 should resolve policy conflicts by affected scope, not by one global precedence ladder.

Required scope-aware behaviors:

- apply conflict resolution at managed-scope, related-zone, proposal-family, or decision-family granularity
- prevent one noisy scope from suppressing unrelated scopes
- preserve bounded inheritance only where policy families are intentionally shared

### 4. Conflict Explainability

Objective 90 must keep conflict resolution operator-visible.

Representative inspectable surfaces:

- winning and losing policy sources for a decision
- applied precedence rule or tie-break reason
- confidence and freshness of each policy signal involved in the conflict
- whether the losing policy was suppressed, deferred, or left advisory
- explicit rationale fields such as `why_policy_a_overrode_policy_b`

### 5. Oscillation Suppression

Objective 90 should reduce flip-flopping when competing policies alternate rapidly.

Required guardrails:

- bounded cooldown or hold-down intervals after high-confidence resolutions
- reopen behavior when contradictory fresh evidence appears
- explicit oscillation counters or instability markers
- prevention of irreversible lock-in from short-term conflict bursts

## Proposed Persistence Surface

Recommended durable model family:

- `WorkspacePolicyConflictProfile`
- `WorkspacePolicyConflictResolutionEvent`

Expected fields:

- `managed_scope`
- `decision_family`
- `policy_sources`
- `winning_policy_source`
- `losing_policy_sources`
- `precedence_rule`
- `conflict_state`
- `conflict_confidence`
- `oscillation_count`
- `cooldown_until`
- `resolution_reason_json`
- `evidence_summary_json`
- `metadata_json`
- `created_at`
- `updated_at`

## Proposed Downstream Effects

Objective 90 should influence at least:

- workspace proposal shaping when proposal-policy convergence conflicts with stronger governance surfaces
- governed execution binding when readiness blocks or degrades execution for the current scope
- autonomy-boundary resolution when multiple policy surfaces disagree on action permissiveness
- stewardship recommendation resolution when execution-truth governance or operator commitments disagree
- governed inquiry answer-path selection when policy surfaces conflict about what is currently allowed or preferred
- operator-visible reasoning in MIM UI

Recommended implementation anchor:

- `core/policy_conflict_resolution_service.py`

## Out Of Scope

- replacing all existing policy surfaces with a single monolithic policy engine
- irreversible suppression of losing policies without inspectable evidence
- unconstrained global precedence that ignores scope
- fully autonomous policy rewriting without operator-visible rationale

## Validation Requirements

Objective 90 should not close without proving real cross-policy arbitration behavior.

Required focused validation cases:

1. conflicting operator commitment and learned policy signals resolve through an explicit precedence rule
2. recent execution-truth governance can override stale learned policy influence in a bounded way
3. scope-local conflicts do not bleed into unrelated scopes
4. oscillating policy disagreements are dampened rather than flip-flopping indefinitely
5. losing policy signals remain inspectable even when they do not win
6. contradictory fresh evidence can reopen a previously stabilized policy-conflict resolution
7. MIM UI and/or policy APIs explain why a winning policy prevailed over a losing one
8. readiness state changes publish to the state bus and persist as the latest scoped readiness snapshot

## Validation Summary

Validated on a fresh isolated server at `127.0.0.1:18099`.

Focused Objective 90 lane:

- `tests.integration.test_objective90_cross_policy_conflict_resolution`
- result: `8/8` passing

Broader governance regression lane:

- `tests.integration.test_objective83_governed_inquiry_resolution_loop`
- `tests.integration.test_objective84_operator_visible_system_reasoning`
- `tests.integration.test_objective85_operator_governed_resolution_commitments`
- `tests.integration.test_objective88_operator_preference_policy_convergence`
- `tests.integration.test_objective88_2_proposal_arbitration_learning`
- `tests.integration.test_objective88_3_proposal_arbitration_learning_propagation`
- `tests.integration.test_objective88_4_proposal_arbitration_learning_autonomy_visibility`
- `tests.integration.test_objective89_proposal_policy_convergence`
- `tests.integration.test_objective90_cross_policy_conflict_resolution`
- result: `36/36` passing

Validated behaviors in the implemented seams:

- active operator commitment overtakes a preferred proposal policy through an explicit precedence rule
- losing policy sources remain inspectable on proposal payloads and the policy-conflict API
- scope-local conflicts do not bleed into unrelated scopes
- stewardship auto-execution arbitration prefers the active operator commitment over lower-precedence learned preference pressure
- autonomy-boundary arbitration prefers the active operator commitment over lower-precedence learned preference pressure
- fresh contradictory governance evidence can reopen a stewardship cooldown instead of preserving a stale hold-down winner
- autonomy cooldown expiry releases the previous hold and returns the scope to the currently effective policy mix
- autonomy conflict inspectability preserves both the persisted learned-preference winner and the masked active operator-commitment candidate when both matter to the outcome explanation
- governed inquiry answer-path arbitration now prefers evidence-gathering paths under an active operator commitment while keeping lower-precedence proposal-learning paths visible but masked
- governed inquiry decision-state arbitration now defers inquiry creation when active operator commitments require more evidence and holds recent answered inquiries behind an inspectable cooldown winner
- generic inquiry decision-state conflicts are now scoped to the inquiry dedupe key instead of a shared global cooldown bucket
- MIM UI exposes the winning conflict-resolution snapshot in `operator_reasoning.conflict_resolution`

Readiness artifact:

- `docs/objective-90-promotion-readiness-report.md`

## Exit Criteria

Objective 90 is complete when all are true:

1. multiple policy surfaces can be evaluated together for conflicts
2. precedence and tie-break behavior is explicit rather than implicit
3. scope-aware conflict resolution is bounded and inspectable
4. oscillation suppression exists without creating stubborn lock-in
5. focused and adjacent validation prove MIM can resolve competing policy signals coherently
6. governed inquiry suppression, cooldown hold, and contradictory-reopen branches are all covered rather than only the currently bounded answer-path and decision-state slices
