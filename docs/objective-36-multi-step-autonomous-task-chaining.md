# Objective 36 — Multi-Step Autonomous Task Chaining

## Overview
Objective 36 introduces policy-controlled multi-step autonomous task chaining. A chain represents a bounded sequence of related proposal executions managed as a single lifecycle object.

This objective provides:
- persistent chain records,
- chain lifecycle endpoints,
- journaled chain creation and advancement events,
- deterministic step progression semantics,
- chain-level approval and cooldown controls,
- stop-on-failure and step-policy enforcement,
- chain audit trail visibility.

## Data Model
`WorkspaceAutonomousChain` stores chain state:
- `chain_id` (stable external identifier)
- `chain_type` (scenario/type label)
- `status` (`pending_approval`, `active`, `completed`, `failed`, `canceled`)
- `source` (origin of chain request)
- `trigger_reason` (human-readable trigger)
- `proposal_ids` (ordered proposal sequence)
- `step_policy_json` (`terminal_statuses`, `failure_statuses`)
- `stop_on_failure`
- `cooldown_seconds`
- `requires_approval`, `approved_by`, `approved_at`
- `last_advanced_at`
- `current_step_index` (active step pointer)
- `completed_step_ids` / `failed_step_ids`
- `audit_trail_json`
- `metadata_json`

## API Surface
- `GET /workspace/chains`
  - Lists chains (newest first).
- `POST /workspace/chains`
  - Creates a chain in `pending` state.
- `GET /workspace/chains/{chain_id}`
  - Returns one chain.
- `POST /workspace/chains/{chain_id}/approve`
  - Approves chain execution when approval is required.
- `GET /workspace/chains/{chain_id}/audit`
  - Returns chain-level audit events.
- `POST /workspace/chains/{chain_id}/advance`
  - Advances lifecycle by evaluating current step proposal state against policy.
  - Enforces:
    - approval requirement,
    - cooldown windows,
    - terminal/failure status policy,
    - optional force override.

## Journaling
Chain operations emit journal events for:
- `workspace_chain_created`
- `workspace_chain_advanced`
- `workspace_chain_approved`

Each event includes `chain_id` and lifecycle deltas. The chain record additionally persists an in-object `audit_trail` timeline with actor/reason/status transitions.

## Safety and Control
- Chain advancement can require explicit operator approval.
- Chain cooldown can rate-limit sequence progression.
- Stop-on-failure behavior prevents unsafe step continuation by default.
- Objective 35 autonomy controls remain authoritative for proposal-level execution.

## Validation Scope
Objective 36 validation covers:
- create/list/get chain APIs,
- approval-gated progression,
- cooldown enforcement,
- stop-on-failure policy behavior,
- chain audit trail visibility,
- not-found behavior for unknown chain IDs.
