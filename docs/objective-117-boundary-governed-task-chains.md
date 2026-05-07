# Objective 117 - Boundary-Governed Task Chains

Date: 2026-04-07
Status: promoted_verified
Depends On: Objective 36, Objective 58, Objective 116
Target Release Tag: objective-122

## Summary

Objective 117 extends the Objective 116 autonomy boundary envelope into multi-step autonomous task chains.

Before this slice, `/workspace/chains` supported approval, cooldown, failure-policy, and audit behavior, but it did not consistently carry the active autonomy explanation. A chain could be created, approved, or advanced without exposing why automation was allowed or blocked under the current boundary profile.

Objective 117 closes that gap by making chain creation, approval, and advance reuse the shared autonomy envelope so chain state, audit evidence, and journal entries answer the same governance question as planning and recovery surfaces.

## Delivered Slice

Objective 117 is now implemented as a boundary-aware extension of the existing autonomous chain surface.

Delivered behavior:

- managed-scope derivation for autonomous chains from explicit metadata first, then from an unambiguous shared proposal scope
- chain boundary-envelope evaluation for:
  - `/workspace/chains`
  - `/workspace/chains/{chain_id}/approve`
  - `/workspace/chains/{chain_id}/advance`
- chain responses that now expose:
  - `managed_scope`
  - `boundary_profile`
  - `boundary_context`
  - `decision_basis`
  - `allowed_actions`
  - `approval_required`
  - `retry_policy`
  - `risk_level`
- approval-floor enforcement when an explicit scoped or global boundary profile requires operator approval, even if the caller requested `requires_approval = false`
- create, approve, and advance audit records that persist the same boundary explanation used at runtime
- create, approve, and advance journal entries that surface the same boundary envelope and preserve caller correlation metadata such as `run_id`
- scope derivation tightened so mixed-proposal chains do not accidentally inherit a boundary from only the first proposal

## Behavioral Anchor

Objective 117 is considered delivered when these statements are true:

- an autonomous chain cannot silently bypass the active autonomy boundary during create, approve, or advance
- chain responses explain why automation was blocked or why bounded continuation remained allowed
- chain audit evidence and journal evidence carry the same boundary explanation as the live response payloads
- unrelated multi-proposal chains only inherit proposal-derived scope when the source proposals agree on one shared scope

## Key Implementation Anchors

- `core/routers/workspace.py`
- `tests/integration/test_objective117_boundary_governed_task_chains.py`
- `tests/integration/test_objective36_multi_step_autonomous_task_chaining.py`

## Validation Evidence

Focused Objective 117 proof:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective117_boundary_governed_task_chains -v`

The focused Objective 117 lane proves:

- a scoped `operator_required` boundary forces a new autonomous chain into `pending_approval`
- chain create, approve, and advance responses expose the boundary explanation directly
- audit records and journal entries preserve the same boundary envelope and explanation across chain lifecycle events

Adjacent regression slice:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective36_multi_step_autonomous_task_chaining tests.integration.test_objective116_boundaries_everywhere -v`

That slice verifies the Objective 117 work did not break:

- baseline multi-step autonomous chain approval, cooldown, and failure-policy behavior under the current autonomy floor
- Objective 116 boundary-envelope propagation across planning, execution, recovery, journaling, and UI surfaces

## Readiness Assessment

- chain boundary propagation: ready
- chain explainability and evidence persistence: ready
- chain approval-floor enforcement: ready
- adjacent regression coverage: ready

## Readiness Decision

- Objective 117 implementation status: PROMOTED_VERIFIED
- Recommendation: use Objective 117 as the boundary-governed chain substrate before extending later work into recovery taxonomies and autonomy tuning policy.