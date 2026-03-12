# Objective 73 — Bus-Driven Cross-System Reactions

Date: 2026-03-12
Status: promoted_verified
Schema Version: 2026-03-12-66

## Summary

Objective 73 turns bus consumption into reaction behavior by adding a dedicated cross-system reaction step that consumes TOD runtime and MIM perception events and emits derived reaction events for downstream strategy/assist flows.

## Scope Delivered

### Reaction Consumer Step

Added endpoint:

- `POST /state-bus/reactions/mim-tod/step`

Behavior:

- upserts and uses consumer key `mim-tod-reaction-core`
- subscribes to `tod.runtime` and `mim.perception` event domains
- reacts to `execution.completed`, `execution.failed`, and `camera.detected`
- produces derived events on the state bus under stream `reaction:mim-tod-reaction-core`

### Derived Reaction Events

Each consumed source event can produce a reaction event:

- `tod.execution.completed_observed` (`mim.strategy`)
- `tod.execution.failure_attention_required` (`mim.assist`)
- `perception.observation_received` (`mim.assist`)

### Replay-Safe Idempotency

Reaction processing stores durable `reacted_event_ids` in consumer metadata and skips already-reacted source events even after cursor replay, preventing duplicated reaction outputs.

### Inspectable Reaction Artifacts

For each reaction, the system writes:

- `MemoryEntry` with class `cross_system_reaction`
- derived state bus event with source linkage (`source_event_id`, domain/type, derived memory id)

## Changed Components

- `core/state_bus_consumer_service.py`
- `core/routers/state_bus.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective73_bus_driven_cross_system_reactions.py`
- `docs/objective-index.md`
