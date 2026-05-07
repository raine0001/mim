# Objective 42 — Multi-Capability Coordination

Date: 2026-03-11

## Goal

Allow safe capability chains to cooperate under bounded policy controls, with dependency-aware execution, step verification, and explainable audit behavior.

## Scope Delivered

### Capability chain model

Added Objective 42 chain model for safe capability workflows:

- chain identity and status
- policy payload and step list
- current step index
- completed/failed step tracking
- stop-on-failure and escalate-on-failure behavior
- explainable audit trail

### Chain policy rules

Policy version: `capability-chain-policy-v1`

Initial allowed safe two-step combinations:

- `workspace_scan -> observation_update`
- `workspace_scan -> target_resolution`
- `target_resolution -> speech_output`
- `rescan_zone -> proposal_resolution`

Chains outside this allowlist are rejected.

### Dependency handling

Each step can declare `depends_on` step IDs.

Policy validates that dependencies:

- exist in the chain
- reference prior steps only
- are satisfied before step execution

### Step-level verification

Each step execution returns a verification payload with capability-specific evidence (for example observation IDs, proposal IDs, target resolution ID, or speech action ID).

### Stop-on-failure / escalate behavior

On failed step execution:

- stop the chain when `stop_on_failure=true`
- mark escalation metadata when `escalate_on_failure=true`
- persist failure in chain audit and journal

### Explainable chain audit trail

Capability chains now include audit events for:

- chain creation
- step completion
- step failure
- escalation-required outcomes

## API Additions

- `GET /workspace/capability-chains`
- `POST /workspace/capability-chains`
- `GET /workspace/capability-chains/{chain_id}`
- `POST /workspace/capability-chains/{chain_id}/advance`
- `GET /workspace/capability-chains/{chain_id}/audit`

## Safety Note

Objective 42 intentionally limits chain execution to safe, bounded combinations and avoids direct physical actuation chains.
