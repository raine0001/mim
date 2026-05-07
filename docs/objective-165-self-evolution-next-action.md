# Objective 165 - Self-Evolution Next Action

Date: 2026-04-08
Status: implemented
Depends On: Objective 54, Objective 55, Objective 164
Target Release Tag: objective-165

## Goal

Objective 165 adds a bounded decision surface on top of the self-evolution core so MIM can recommend the single most relevant next improvement action without requiring operators to infer it manually from snapshot counts and ranked backlog state.

## Implemented Slice

- Added bounded next-action selection in `core/self_evolution_service.py`.
- Added `GET /improvement/self-evolution/next-action` in `core/routers/improvement.py`.
- Reused Objective 164 snapshot state plus the existing Objective 54 and 55 routes to recommend one next step at a time.
- Returned explicit action metadata including HTTP method, route path, payload, target kind, target id, priority, rationale, and snapshot context.
- Kept the slice non-destructive by recommending existing review, inspection, generation, or refresh routes instead of mutating improvement state directly.

## Validation

- Focused runtime-backed lane passed on the current-source server:
  - `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective54_self_guided_improvement_loop tests.integration.test_objective55_improvement_prioritization_governance tests.integration.test_objective164_self_evolution_core tests.integration.test_objective165_self_evolution_next_action -v`
  - Result: `Ran 4 tests ... OK`

## Notes

- This slice is a control recommendation layer, not a new approval workflow.
- The returned action metadata is intended to make the self-evolution loop inspectable and operable from one bounded decision contract.