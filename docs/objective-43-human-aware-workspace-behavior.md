# Objective 43 — Human-Aware Workspace Behavior

## Goal

Add explicit human-presence signals and policy-driven behavior controls so autonomous loops and capability chains behave safely in shared workspaces.

## Scope Delivered

### Task A: Human-presence signal model

Added explicit inspectable signals persisted in workspace monitoring metadata:

- `human_in_workspace`
- `human_near_target_zone`
- `human_near_motion_path`
- `shared_workspace_active`
- `operator_present`
- `occupied_zones`
- `high_proximity_zones`

API:

- `GET /workspace/human-aware/state`
- `POST /workspace/human-aware/signals`

### Task B: Behavior policy

Added human-aware policy outcomes:

- `continue`
- `slow_suppress`
- `pause`
- `require_operator_confirmation`
- `stop_replan`

Policy is applied to:

- autonomous proposal execution decisions (`_maybe_auto_execute_workspace_proposal`)
- capability-chain advancement (`/workspace/capability-chains/{chain_id}/advance`)

### Task C: Shared-workspace rules

Implemented rules including:

- no autonomous movement in occupied zones
- no auto-exec physical actions under high human proximity
- pause when workspace is actively shared
- operator-aware speech suppression for etiquette (`speech_output` step can be suppressed)

### Task D: Inspectability

Exposed inspectable state with reasons:

- current signals
- policy outcome set
- last policy decision (`outcome`, `reason`, `at`)
- last signal update actor/reason/timestamp

Inspectability included in:

- `GET /workspace/human-aware/state`
- `GET /workspace/autonomy/policy`
- `GET /workspace/monitoring`

### Task E: Tests

Added focused Objective 43 integration test:

- `tests/integration/test_objective43_human_aware_workspace_behavior.py`

Scenarios covered:

- human enters during autonomous chain -> pause
- human near target zone -> require confirmation
- human leaves and workspace safe -> resume allowed
- non-physical safe behavior continues with operator-present speech suppression

## Files Updated

- `core/routers/workspace.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective43_human_aware_workspace_behavior.py`

## Manifest Updates

- schema version: `2026-03-11-34`
- capability added: `human_aware_workspace_behavior`
- endpoints added:
  - `/workspace/human-aware/state`
  - `/workspace/human-aware/signals`
