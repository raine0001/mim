# Objective 26 — Object Identity Persistence

Date: 2026-03-10

## Goal

Extend workspace memory from zone-level observations to object-level identity so MIM can reason about the same object over time, movement, and uncertainty.

## Implemented Scope

### Task A — Object Memory Model

Added `workspace_object_memories` with:

- `object_memory_id` (`id`)
- `canonical_name`
- `candidate_labels` (aliases)
- `confidence`
- `zone` (last seen location)
- `first_seen_at`
- `last_seen_at`
- `status` (`active`, `uncertain`, `stale`, `missing`)
- `last_execution_id`
- `location_history`
- `metadata_json`

### Task B — Identity Matching

On each `workspace_scan` observation:

- match against existing object memory candidates using:
  - similar label
  - same/preferred zone
  - recent time window
  - score threshold
- update matched object memory when likely same object
- create new object memory when no match is found

### Task C — Moved/Missing Logic

- if matched object appears in a new zone:
  - update location history
  - mark status as `uncertain`
- if expected object in scanned zone is not re-observed:
  - mark as `missing`
  - degrade confidence
- if object ages beyond stale window:
  - mark as `stale`

### Task D — Query Endpoints

Added:

- `GET /workspace/objects`
- `GET /workspace/objects/{object_memory_id}`
- `GET /workspace/objects?label=...`

Output includes identity confidence, effective confidence, status, location history, and execution linkage.

### Task E — Routing Integration

Memory-informed routing now includes object identity signals:

- `object_recent_strong_count`
- `object_uncertain_count`
- `object_stale_missing_count`
- `dominant_object`
- `strongest_object_confidence`

Decision behavior:

- uncertain object identity can force reconfirmation
- strong recent object identity can support auto execution
- stale/missing identity contributes to reconfirmation pressure
