# Objective 23B: Safe Capability Expansion

## Goal

Expand capability surface safely by introducing one new low-risk capability with full lifecycle and operator verification.

## Selected capability

- `workspace_scan`

Why this capability:

- touches environment through observation only
- non-actuating and low risk
- exercises gateway -> execution binding -> TOD feedback -> operator review loop

## Implemented scope

### Task A — capability registration

`workspace_scan` capability with execution parameters:

- `scan_mode`
- `scan_area`
- `confidence_threshold`

### Task B — execution binding

Intent mapping for workspace observation now prefers:

- `observe_workspace` -> `workspace_scan`
- fallback to `workspace_check` when `workspace_scan` is unavailable

Execution argument defaults for `workspace_scan` are generated from input metadata.

### Task C — TOD executor stub

TOD execution lifecycle includes a `workspace_scan` path that:

- emits `accepted` -> `running` -> `succeeded`
- returns structured observation dataset
- posts observations in execution feedback payload

### Task D — operator verification

Operator surface additions:

- `GET /operator/executions/{execution_id}/observations`
- `POST /operator/executions/{execution_id}/ignore`
- `POST /operator/executions/{execution_id}/request-rescan`

Existing `promote-to-goal` path remains available for observation promotion.

### Task E — gate validation target

Validation covers:

- text -> scan workspace
- voice -> scan workspace
- api -> scan workspace

And verifies:

- execution dispatch and binding
- TOD feedback states and observations
- derived observation event persistence
- operator observation review path (promote/ignore/rescan)
