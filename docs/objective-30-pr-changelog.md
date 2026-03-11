# Objective 30 PR Changelog

## Summary

Adds safe directed action planning on top of directed workspace targeting, with policy-derived planning outcomes, operator approval/rejection controls, and queue handoff stubs under non-actuating safety constraints.

## What Changed

- Added persistent action-plan model:
  - `WorkspaceActionPlan` in `core/models.py`
- Added request contracts:
  - `WorkspaceActionPlanCreateRequest`
  - `WorkspaceActionPlanDecisionRequest`
  - `WorkspaceActionPlanHandoffRequest`
  - safe action type set: `observe`, `rescan`, `speak`, `prepare_reach_plan`, `request_confirmation`
  - in `core/schemas.py`
- Added Objective 30 workspace APIs in `core/routers/workspace.py`:
  - `POST /workspace/action-plans`
  - `GET /workspace/action-plans/{plan_id}`
  - `POST /workspace/action-plans/{plan_id}/approve`
  - `POST /workspace/action-plans/{plan_id}/reject`
  - `POST /workspace/action-plans/{plan_id}/queue`
- Implemented planning policy outcomes from target-resolution context:
  - `plan_ready_for_approval`
  - `plan_requires_review`
  - `plan_blocked`
  - operator transitions to `plan_approved`, `plan_rejected`, `plan_queued`
- Added queue handoff stub behavior:
  - queue creates a linked queued task
  - response includes handoff metadata and requested executor
- Updated manifest metadata in `core/manifest.py`:
  - `schema_version` -> `2026-03-10-20`
  - capability -> `safe_directed_action_planning`
  - endpoint/object catalog updates
- Added docs:
  - `docs/objective-30-safe-directed-action-planning.md`
  - `docs/objective-30-promotion-readiness-report.md`
  - `docs/objective-30-prod-promotion-report.md`

## Validation

### Test gate (test stack `:8001`)

Passed:

- `tests.integration.test_objective30_safe_directed_action_planning`
- `tests.integration.test_objective29_directed_targeting`
- `tests.integration.test_objective28_autonomous_task_proposals`
- `tests.integration.test_objective27_workspace_map_relational_context`
- `tests.integration.test_objective26_object_identity_persistence`
- `tests.integration.test_objective25_memory_informed_routing`
- `tests.integration.test_objective24_workspace_observation_memory`
- `tests.integration.test_objective23b_workspace_scan`

### Production

- Promotion script: `scripts/promote_test_to_prod.sh objective-30` -> PASS
- `GET /health` -> PASS
- `GET /manifest` -> PASS (`schema_version=2026-03-10-20`, `release_tag=objective-30`)
- `tests.integration.test_objective30_safe_directed_action_planning` against `:8000` -> PASS
- `tests.integration.test_objective29_directed_targeting` against `:8000` -> PASS

## Safety Notes

- No direct actuation endpoints introduced.
- Planning remains operator-mediated before queueing.
- Queue action is a safe handoff stub that records task linkage and metadata only.
