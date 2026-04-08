# Objective 127 - Recovery Commitment Scope Propagation

Date: 2026-04-08
Status: promoted_verified
Depends On: Objective 117, Objective 118, Objective 121, Objective 122, Objective 126
Target Release Tag: objective-130

## Summary

Recovery-derived commitments are currently readable at the governed scope, but the exact propagation rules into descendant task, capability, and execution scopes are still implicit.

Objective 127 makes those scope-propagation rules explicit so recovery-derived posture can follow the same envelope logic already established for chains and execution planning.

## Delivered Slice

Delivered behavior:

- recovery-derived commitments now resolve through deterministic scope hierarchy lookup instead of exact-scope-only matching
- inherited application is explicit through `scope_hierarchy`, `scope_match_kind`, and `scope_match_distance`
- execution and recovery surfaces preserve whether a commitment applied directly or by inherited scope
- child execution admission and governance rollups now show the inherited recovery posture without silently widening to unrelated scopes
- propagation reasoning is summarized in the same scope-application structure consumed by preview, admission, and UI rollup surfaces

## Behavioral Anchor

Objective 127 is considered delivered when these statements are true:

- planners and chain executors can explain whether a recovery-derived commitment applied directly, indirectly, or not at all
- scope inheritance for recovery posture is deterministic and inspectable
- recovery commitment propagation does not silently widen into unrelated global scopes

## Key Implementation Anchors

- `core/autonomy_boundary_service.py`
- `core/routers/workspace.py`
- `core/execution_policy_gate.py`
- `core/execution_recovery_service.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`

## Validation Evidence

Focused lifecycle proof is included in:

- `tests.integration.test_objective123_130_recovery_governance_lifecycle`

That lane proves inherited parent commitments shape child-scope admission and remain explainable as inherited rather than exact matches.

## Exit Criteria

- implementation status: READY_FOR_PROMOTION_REVIEW
- recommendation: treat inherited scope matching as part of the core governance contract, not a UI-only explanation layer.