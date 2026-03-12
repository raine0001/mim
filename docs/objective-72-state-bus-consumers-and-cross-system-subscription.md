# Objective 72 — State Bus Consumers and Cross-System Subscription

Date: 2026-03-12
Status: promoted_verified
Schema Version: 2026-03-12-65

## Summary

Objective 72 turns the unified state bus into an actively consumed ecosystem by adding subscription-driven consumers with filtering, progress tracking, idempotent acknowledgment, replay controls, and inspectability.

## Scope Delivered

### Consumer/Subscription Model

Added persistent consumer state model: `WorkspaceStateBusConsumer` (`workspace_state_bus_consumers`) with:

- `consumer_key` identity
- subscription filters (`domains`, `event_types`, `sources`, `stream_keys`)
- cursor/progress (`cursor_event_id`, `cursor_occurred_at`)
- idempotency state (`processed_event_ids`)
- lag and operational metrics (`poll_count`, `ack_count`, `lag_count`)
- replay metadata (`replay_from_snapshot_scope`, timestamps)

### Event Filtering + Idempotent Consumption

Added APIs to:

- register/update consumers
- poll filtered events
- acknowledge consumed events idempotently

Ack ignores duplicate previously-processed IDs and only advances cursor for valid subscription-matching events.

### Replay Controls

Added replay endpoint supporting:

- replay from durable snapshot scope (`from_snapshot_scope`)
- replay from event history cursor (`from_event_id`)

Replay resets processed-id cache and rewinds cursor deterministically.

### Inspectability

Added inspectable consumer endpoints:

- `GET /state-bus/consumers`
- `GET /state-bus/consumers/{consumer_key}`

Returned state includes subscription filters, cursor, lag, poll/ack counters, and replay status.

### Cross-System Consumption (MIM reacting to TOD runtime)

Added built-in consumer step:

- `POST /state-bus/consumers/mim-core/step`

Behavior:

- subscribes to `tod.runtime` execution events
- consumes pending events through Objective 72 consumer mechanics
- writes `MemoryEntry` artifacts for strategy/memory adaptation context
- emits derived `mim.strategy` events (`tod.execution.ingested`)

This provides the first concrete cross-system bus consumption path from TOD runtime into MIM memory/strategy layers.

## Changed Components

- `core/models.py`
- `core/state_bus_consumer_service.py`
- `core/routers/state_bus.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective72_state_bus_consumers_and_subscription.py`
- `docs/objective-index.md`
