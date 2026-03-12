# Objective 24 — Workspace Observation Memory

Date: 2026-03-10

## Goal

Introduce persistent environment memory so `workspace_scan` observations accumulate over time and can influence later reasoning.

## Implemented Scope

### Task A — Observation Store

Added `workspace_observations` persistence with:

- `observation_id` (`id`)
- `timestamp` (`observed_at` / `last_seen_at`)
- `zone`
- `detected_object` (`label`)
- `confidence`
- `source`
- `related_execution_id` (`execution_id`)
- `lifecycle_status` (`active`, `outdated`, `superseded`)

Additional memory fields:

- `first_seen_at`
- `observation_count`
- `metadata_json`

### Task B — Observation Deduplication

During `workspace_scan` feedback ingestion:

- dedupe key = `label + zone`
- dedupe window = 300 seconds
- when duplicate found in-window:
  - update timestamps
  - keep max confidence
  - increment `observation_count`
  - attach latest execution context

### Task C — Observation Aging

Freshness is computed from `last_seen_at`:

- recent: <= 10m
- aging: <= 60m
- stale: > 60m

Confidence weighting for decision use:

- recent: `1.0x`
- aging: `0.75x`
- stale: `0.4x`

Lifecycle update behavior:

- recent -> `active`
- aging/stale -> `outdated`
- `superseded` is preserved and excluded by default from list queries

### Task D — Observation Query API

Added endpoints:

- `GET /workspace/observations`
- `GET /workspace/observations/{observation_id}`
- `GET /workspace/observations?zone=table`

Response includes:

- raw `confidence`
- `effective_confidence`
- `freshness_state`
- `lifecycle_status`
- related execution and metadata

### Task E — Scan Integration

`workspace_scan` execution feedback now:

- continues creating scan `InputEvent` (`observation_event_id`)
- upserts persistent `workspace_observations`
- records `workspace_observation_ids` in execution feedback payload

## Validation Plan

Gate tests must prove:

- `workspace_scan` writes persistent observations
- duplicates merge into existing observation memory records
- freshness and lifecycle updates are applied
- workspace query endpoints return expected records
- stale observations receive reduced effective confidence
