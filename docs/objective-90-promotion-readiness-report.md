# Objective 90 Promotion Readiness Report

Date: 2026-03-26
Objective: 90
Title: Cross-Policy Conflict Arbitration
Status: complete

## Scope Delivered

Objective 90 now covers the full bounded cross-policy conflict-resolution rollout across proposal shaping, stewardship, autonomy, governed inquiry answer-path selection, and governed inquiry decision-state suppression/cooldown/reopen behavior.

This slice introduces explicit policy conflict arbitration for workspace proposal shaping and widens the same winning-versus-losing policy reasoning into stewardship, autonomy, and governed inquiry surfaces.

## Implementation Summary

Objective 90 adds a durable, inspectable conflict-resolution layer for proposal-shaping, stewardship, autonomy, and governed inquiry disagreements between existing policy surfaces.

Delivered behavior:

- durable `WorkspacePolicyConflictProfile` and `WorkspacePolicyConflictResolutionEvent` rows record active and recent conflict outcomes
- proposal shaping now resolves disagreements between active operator commitments, recent execution-truth governance, proposal-policy convergence, and learned preferences before final proposal selection
- stewardship auto-execution now resolves disagreements between active operator commitments, fresh governance posture, learned preference pressure, and current autonomy state
- autonomy-boundary recompute now resolves disagreements between active operator commitments, execution-truth governance, commitment-outcome learning, learned preferences, and proposal-arbitration review
- governed inquiry answer-path ranking now resolves disagreements between active operator commitments and proposal-arbitration path learning for the same scope
- governed inquiry decision-state creation now resolves disagreements between active operator commitments, high-confidence autonomy suppression, recent-answer cooldown hold behavior, and fresh inquiry trigger evidence
- explicit precedence is now enforced for this seam rather than relying on hidden service-local ordering
- losing policy sources remain inspectable on both proposal payloads and conflict-profile API responses
- operator-visible reasoning now exposes the winning conflict snapshot in `/mim/ui/state`
- bounded hold-down metadata now persists `oscillation_count` and `cooldown_until`
- cooldown expiry can release a previously stabilized hold so the current policy mix is re-evaluated instead of remaining stuck

Current precedence in this slice is:

1. active operator commitment
2. execution readiness when TOD reports degraded or blocked posture for the same scope
3. recent execution-truth governance
4. commitment outcome learning where applicable
5. proposal-policy convergence or proposal-arbitration review for the specific decision family
6. learned preference

## What This Slice Includes

- proposal-level conflict detection and arbitration in workspace proposal priority refresh
- stewardship conflict detection and arbitration during stewardship cycle evaluation
- autonomy conflict detection and arbitration during autonomy-boundary recompute
- governed inquiry answer-path conflict detection and arbitration during inquiry question generation
- durable conflict profile and event persistence
- scope-local conflict handling across the currently supported decision families
- operator-visible conflict reasoning through MIM UI
- a queryable conflict API surface at `/workspace/proposals/policy-conflicts`

## Remaining Boundary

- broad branch-wide promotion validation beyond the directly affected governance lane remains outside Objective 90 acceptance

## Exact Affected Surfaces

- `core/policy_conflict_resolution_service.py`
- `core/models.py`
- `core/schemas.py`
- `core/routers/workspace.py`
- `core/routers/mim_ui.py`
- `core/autonomy_boundary_service.py`
- `core/inquiry_service.py`
- `tests/integration/test_objective90_cross_policy_conflict_resolution.py`
- `docs/objective-90-cross-policy-conflict-resolution.md`

## Behavioral Anchor

The promotion-relevant contract for Objective 90 is:

- proposal-shaping policy conflicts are resolved through explicit precedence rather than implicit last-writer-wins behavior
- stronger scoped commitments and fresh governance can visibly override weaker learned proposal pressure
- stewardship and autonomy consumers reuse the same inspectable conflict profile model instead of introducing separate hidden override rules
- governed inquiry answer-path ranking can prefer safer evidence-gathering actions over lower-precedence proposal-learning pressure for the same scope
- governed inquiry decision-state arbitration can suppress or defer inquiry creation with an inspectable winning policy source instead of hidden service-local branch ordering
- losing policy sources remain inspectable instead of disappearing behind the winner
- scope-local conflict outcomes stay bounded and do not bleed into unrelated scopes
- cooldown expiry can release a previous hold-down outcome and re-enter current arbitration cleanly
- autonomy conflict explainability can preserve a learned-preference winner while still surfacing an active operator-commitment candidate that is masking the downstream posture

## Inspectability Contract

Objective 90 is promotion-relevant because the arbitration is not hidden.

- workspace proposal payloads now carry `policy_conflict_resolution`
- workspace proposal priority breakdowns now carry `policy_conflict_resolution`
- governed inquiry question payloads now carry `policy_conflict_resolution`
- governed inquiry decisions and question payloads now carry `decision_policy_conflict_resolution` for decision-state arbitration
- `/workspace/proposals/policy-conflicts` exposes persisted conflict profiles directly
- `/mim/ui/state` now carries `operator_reasoning.conflict_resolution`
- precedence outcomes and losing policy sources are visible in conflict reasoning metadata

## Validation Summary

Validated on a fresh isolated server at `127.0.0.1:18001`.

Focused Objective 90 lane:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective90_cross_policy_conflict_resolution`
- Result: PASS (`11/11`)

Broader governance regression lane for the directly affected neighborhood:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective83_governed_inquiry_resolution_loop tests.integration.test_objective84_operator_visible_system_reasoning tests.integration.test_objective85_operator_governed_resolution_commitments tests.integration.test_objective88_operator_preference_policy_convergence tests.integration.test_objective88_2_proposal_arbitration_learning tests.integration.test_objective88_3_proposal_arbitration_learning_propagation tests.integration.test_objective88_4_proposal_arbitration_learning_autonomy_visibility tests.integration.test_objective89_proposal_policy_convergence tests.integration.test_objective90_cross_policy_conflict_resolution`
- Result: PASS (`36/36`)

Validated behaviors in that lane:

- active operator commitment overtakes a preferred proposal policy through an explicit precedence rule
- losing policy sources remain inspectable on proposal payloads and the conflict-profile API
- scope-local conflicts do not bleed into unrelated scopes
- stewardship cooldown can reopen when fresh contradictory governance evidence arrives
- autonomy cooldown expiry can release a previous hold and re-evaluate the current policy mix
- autonomy conflict reasoning preserves the current branch semantics where a learned preference can remain the persisted winner while an active operator commitment is surfaced as a masked advisory candidate
- governed inquiry answer-path arbitration prefers evidence-gathering paths when an active operator commitment conflicts with proposal-learning pressure for the same scope
- governed inquiry decision-state arbitration defers inquiry creation when active operator commitments require more evidence and keeps recent answered inquiries behind an inspectable cooldown winner
- generic inquiry decision-state conflict profiles are isolated to the inquiry dedupe key when no explicit managed scope exists
- `/mim/ui/state` exposes the winning conflict-resolution snapshot in `operator_reasoning.conflict_resolution`

## Fixes Required During Validation

The first green lane required several concrete fixes.

- `tests/integration/test_objective90_cross_policy_conflict_resolution.py`: cleanup was hardened so it tolerates the new conflict tables not existing before schema creation
- `tests/integration/test_objective90_cross_policy_conflict_resolution.py`: stewardship seed helpers were corrected to stop inserting the nonexistent `updated_at` column
- `tests/integration/test_objective90_cross_policy_conflict_resolution.py`: stewardship seed helpers were expanded to include required JSON fields such as `target_environment_state_json` and linked arrays
- `core/routers/mim_ui.py`: the new conflict snapshot helper was moved to module scope after a misindentation caused `/mim/ui/state` to fail with `NameError`

These fixes are part of the validated slice and are required context for future widening into additional policy consumers.

## Known Boundaries Of This Seam

- This readiness snapshot applies to the current widened Objective 90 slice: proposal shaping, stewardship, autonomy, inquiry answer-path arbitration, and bounded inquiry decision-state suppression/cooldown hold behavior.
- The current inquiry slice is intentionally bounded to the governed-inquiry branches exercised by Objective 90, and that bounded reopen matrix is now complete.
- The green validation basis is the focused Objective 90 test and the broader 83/84/85/88/88.2/88.3/88.4/89/90 governance lane, not a full branch-wide objective sweep.

## Readiness Assessment

- explicit proposal-shaping conflict arbitration: ready
- stewardship and autonomy conflict arbitration: ready
- operator-visible conflict inspectability: ready
- bounded scope-local behavior: ready
- bounded inquiry suppression/cooldown/reopen matrix: ready
- broader governance neighborhood: green
- inquiry conflict rollout: complete for the current bounded slice

## Readiness Decision

- Objective 90 feature slice: COMPLETE_AND_GREEN
- Recommendation: treat the current implementation as the stable Objective 90 checkpoint for proposal-shaping, stewardship, autonomy, inquiry answer-path arbitration, and bounded inquiry decision-state suppression/cooldown/reopen behavior, then start Objective 96 recovery-layer work on top of this now-green control plane.
