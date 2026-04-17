# Objective 21: Unified Input and Perception Gateway

Objective 21 introduces a single normalized intake contract for all command/event sources and routes adapters through one canonical schema.

## Scope Implemented

- Canonical intake object persisted in `input_events`.
- Source adapters for:
  - text (`/gateway/intake/text`)
  - ui (`/gateway/intake/ui`)
  - api (`/gateway/intake/api`)
  - voice transcript (`/gateway/voice/input`)
  - vision observation (`/gateway/vision/observations`)
- Speech output queue/stub endpoint (`/gateway/voice/output`) for status updates and prompts.
- Capability registration and listing (`/gateway/capabilities`).
- Journal integration for normalization and capability lifecycle updates.

## Canonical Intake Contract

The normalized payload shape is:

- `source`: `text | ui | api | voice | vision`
- `raw_input`: raw source material
- `parsed_intent`: normalized intent string
- `confidence`: `0.0 - 1.0`
- `target_system`: target subsystem (default: `mim`)
- `requested_goal`: target goal/requested outcome
- `safety_flags`: policy and gating flags
- `metadata_json`: adapter/provider metadata

## Design Notes

- Voice is implemented as two pathways:
  - output (`/gateway/voice/output`)
  - input adapter (`/gateway/voice/input`) routed into canonical intake
- Vision events are observation-first and include a default safety gate (`requires_confirmation`) to avoid autonomous immediate action.
- Capability registration is explicit and policy-aware (`requires_confirmation`, `enabled`, `safety_policy`).

## Next Step

Connect real speech and vision providers to the adapter endpoints while keeping execution behind capability and safety policy checks.

## Objective 21.2: Capability-to-Goal Bridge

The gateway now includes an interpretation and routing bridge from normalized events to goal outcomes.

- Intent classification layer maps events into:
  - `speak_response`
  - `observe_workspace`
  - `identify_object`
  - `execute_capability`
  - `create_goal`
  - `request_clarification`
- Capability resolution maps intent to registered gateway capabilities.
- Safety gate classifies each event as:
  - `auto_execute`
  - `requires_confirmation`
  - `blocked`
- Goal bridge behavior:
  - auto-executable events create goals with `status=new`
  - confirmation-required events create goals with `status=proposed`
  - blocked events do not create goals

### Inspectability Endpoints

- `GET /gateway/events`
- `GET /gateway/events/{event_id}`
- `GET /gateway/events/{event_id}/resolution`
- `POST /gateway/events/{event_id}/promote-to-goal`

These endpoints expose event normalization, resolution decisions, capability checks, proposed action chain, and promotion outcomes for operator review.

## Objective 21.3: Vision Observation Confidence Policy

Vision observations now use a configurable policy profile for confidence-tier interpretation and escalation.

- Policy profile location:
  - `config/vision_policy.json`
  - path configurable through `vision_policy_path`
- Confidence tiers:
  - `high`
  - `medium`
  - `low`
- Vision outcomes:
  - `auto_execute`
  - `propose_goal`
  - `requires_confirmation`
  - `store_only`
  - `blocked`

### Escalation Reasons

- `low_confidence_detection`
- `ambiguous_label`
- `multiple_candidate_objects`
- `unsafe_capability_implication`
- `unknown_object`
- `conflicting_observation`
- `requires_human_confirmation`

### Current Safety Behavior

- low-confidence vision signals are stored with `store_only` and no goal creation.
- medium-confidence signals require confirmation and retain escalation context.
- high-confidence safe observations may auto-execute for configured safe intents.
- unsafe capability implications are blocked even at high confidence.

### Policy Inspection

- `GET /gateway/vision-policy`

Returns the active policy profile (thresholds, auto-propose toggle, safe intents, blocked implications, and label overrides) so operators can verify runtime safety configuration.

## Objective 21.4: Voice Interaction Policy and Output Execution

Voice input now uses a policy-driven confidence and escalation model parallel to vision.

- Policy profile location:
  - `config/voice_policy.json`
  - path configurable through `voice_policy_path`
- Voice confidence tiers:
  - `high`: normal bridge behavior
  - `medium`: requires confirmation
  - `low`: `store_only` or confirmation (policy-driven)

### Voice Escalation Reasons

- `low_transcript_confidence`
- `ambiguous_command`
- `unsafe_action_request`
- `missing_target`
- `requires_clarification`

### Clarification Behavior

When voice input is uncertain, resolution includes a structured `clarification_prompt` and avoids forcing direct execution.

### Speech Output Execution Contract

`POST /gateway/voice/output` now records structured execution actions with:

- requested text
- voice profile
- channel
- priority
- delivery status
- failure reason

Policy constraints include safe output length and allowed output priorities.

### Voice Policy Inspection

- `GET /gateway/voice-policy`

## Objective 21.5: Capability Execution Binding

Approved gateway resolutions now bind to executable capability requests with dispatch policy and feedback state.

### Execution Request Model

Execution bindings persist:

- `goal_id` reference
- `capability_name`
- `arguments_json`
- `safety_mode`
- `requested_executor`
- `dispatch_decision`
- `status`
- `reason`
- `feedback_json`

### Dispatch Policy

- auto-approved resolutions bind as `auto_dispatch` / `dispatched`
- confirmation-required resolutions bind as `requires_confirmation` / `pending_confirmation`
- blocked or store-only resolutions bind as `blocked` / `blocked`
- manual dispatch is available for approved override paths

### Execution Inspectability

- `GET /gateway/events/{event_id}/execution`
- `POST /gateway/events/{event_id}/execution/dispatch`
- `GET /capabilities/executions/{execution_id}`

## Objective 21.7: Guarded Execution Feedback Updater

Execution lifecycle feedback can now be updated with guarded status transitions.

### Feedback Endpoints

- `POST /gateway/capabilities/executions/{execution_id}/feedback`
- `GET /gateway/capabilities/executions/{execution_id}/feedback`

### Tracked Statuses

- `dispatched`
- `accepted`
- `running`
- `succeeded`
- `failed`
- `blocked`

### Guardrail

Invalid state transitions are rejected with `422`, and valid transitions append structured history into `feedback_json.history`.
