# Objective 29 PR Changelog

## Summary

Adds directed workspace target resolution with confidence-driven policy outcomes, operator confirmation flow, and proposal integration under non-actuating safety constraints.

## What Changed

- Added persistent target resolution model:
  - `WorkspaceTargetResolution` in `core/models.py`
- Added request contracts:
  - `WorkspaceTargetResolveRequest`
  - `WorkspaceTargetConfirmRequest`
  - in `core/schemas.py`
- Added Objective 29 workspace APIs in `core/routers/workspace.py`:
  - `POST /workspace/targets/resolve`
  - `GET /workspace/targets/{target_resolution_id}`
  - `POST /workspace/targets/{target_resolution_id}/confirm`
- Implemented label/zone-aware matching and confidence policy outcomes:
  - `target_confirmed`
  - `target_requires_confirmation`
  - `target_not_found`
  - `target_stale_reobserve`
  - `target_blocked_unsafe_zone`
- Added optional proposal generation for target outcomes and confirmation actions.
- Updated manifest metadata in `core/manifest.py`:
  - `schema_version` -> `2026-03-10-19`
  - capability -> `directed_workspace_targeting`
  - endpoint/object catalog updates
- Added docs:
  - `docs/objective-29-directed-targeting.md`
  - `docs/objective-29-promotion-readiness-report.md`
  - `docs/objective-29-prod-promotion-report.md`

## Validation

### Test gate (test stack `:8001`)

Passed:

- `tests.integration.test_objective29_directed_targeting`
- `tests.integration.test_objective28_autonomous_task_proposals`
- `tests.integration.test_objective27_workspace_map_relational_context`
- `tests.integration.test_objective26_object_identity_persistence`
- `tests.integration.test_objective25_memory_informed_routing`
- `tests.integration.test_objective24_workspace_observation_memory`
- `tests.integration.test_objective23b_workspace_scan`

### Production

- Promotion script: `./scripts/promote_test_to_prod.sh objective-29` -> PASS
- `GET /health` -> PASS
- `GET /manifest` -> PASS (`schema_version=2026-03-10-19`, `release_tag=objective-29`)
- `tests.integration.test_objective29_directed_targeting` against `:8000` -> PASS

## Safety Notes

- No direct actuation introduced.
- Target resolution remains recommendation/queue oriented with operator confirmation where required.
