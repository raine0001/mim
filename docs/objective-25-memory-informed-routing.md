# Objective 25 — Memory-Informed Routing

Date: 2026-03-10

## Goal

Use accumulated workspace observation memory to influence gateway routing decisions so execution confidence reflects environment memory freshness.

## Implemented Scope

### Memory Signal in Resolution

For `observe_workspace` intent, gateway now computes a `memory_signal` from `workspace_observations` by zone:

- `zone`
- `recent_count`
- `stale_count`
- `best_effective_confidence`
- `dominant_label`

The signal is attached to `InputEventResolution.metadata_json.memory_signal`.

### Decision Influence Rules

For `observe_workspace` outcomes (when not blocked/store-only):

- If memory has recent observations with effective confidence >= 0.75:
  - bias to `auto_execute`
  - reason set to `memory_confident_recent_observation`
- If memory is stale-only (no recent observations):
  - downgrade to `requires_confirmation`
  - reason set to `memory_stale_requires_reconfirm`
  - add escalation reason `stale_observation_needs_reconfirm`

Existing safety boundaries are preserved:

- capability availability checks
- safety flags (e.g. requires confirmation)
- capability confirmation policies

## Validation Target

Objective 25 test verifies:

- stale memory causes confirmation downgrade
- fresh memory restores confident auto-execution
- resolution metadata includes memory signal for inspectability
