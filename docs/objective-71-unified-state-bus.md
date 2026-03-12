# Objective 71 — Unified State Bus

Date: 2026-03-12
Status: implemented
Schema Version: 2026-03-12-64

## Summary

Objective 71 introduces a unified state bus that separates:

- real-time event flow (`workspace_state_bus_events`)
- durable state snapshots (`workspace_state_bus_snapshots`)

This creates a shared nervous-system layer for TOD runtime events and MIM cognitive layers while preserving snapshot durability as long-term memory.

## Scope Delivered

### Event Stream Layer

Added persistent event model and API surface for append/list/get:

- `POST /state-bus/events`
- `GET /state-bus/events`
- `GET /state-bus/events/{event_id}`

Supported domains:

- `tod.runtime`
- `mim.perception`
- `mim.strategy`
- `mim.improvement`
- `mim.assist` (reserved for future MIM Assist integration)

Each stream uses a per-`stream_key` `sequence_id` for ordered incremental consumption.

### Durable Snapshot Layer

Added durable snapshot model and API surface for upsert/list/get:

- `POST /state-bus/snapshots/{snapshot_scope}`
- `GET /state-bus/snapshots`
- `GET /state-bus/snapshots/{snapshot_scope}`

Snapshots track:

- versioned state (`state_version`)
- linked last event (`last_event_id`, `last_event_sequence`, domain/type)
- mutable state payload for incremental checkpointing

### MIM Strategy Auto-Publish Integration

`/orchestration/build` now emits state-bus events:

- domain: `mim.strategy`
- event type: `orchestration.built`
- stream key: `orchestration:{orchestration_id}`

This gives immediate real-time strategy synchronization without changing existing orchestration contracts.

## Bounded v1 Behavior

- Event append is explicit and schema-bounded.
- Snapshot upsert is explicit and version-incrementing.
- Snapshot and event APIs are separated to keep operational semantics clear.
- Existing Objective 63–70 behavior remains unchanged; Objective 71 is additive.

## Changed Components

- `core/models.py`
- `core/state_bus_service.py`
- `core/routers/state_bus.py`
- `core/routers/__init__.py`
- `core/routers/orchestration.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective71_unified_state_bus.py`
- `docs/objective-index.md`
