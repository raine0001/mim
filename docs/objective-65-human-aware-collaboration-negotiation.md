# Objective 65: Human-Aware Collaboration Negotiation

Objective 65 adds a persistent collaboration-negotiation layer so MIM can request structured human decisions when human-aware collaboration policy identifies meaningful trade-offs between safe options.

## Scope Implemented

### Task A: Negotiation Model

- Added persistent model: `workspace_collaboration_negotiations`.
- Core fields include:
  - `negotiation_id` (`id`)
  - originating references (`origin_orchestration_id`, `origin_context_id`, `origin_goal_id`, `origin_horizon_plan_id`)
  - `human_context_state_json`
  - `requested_decision`
  - `options_presented_json`
  - `default_safe_path`
  - resolution lifecycle (`status`, `resolution_status`, selected option, resolver, resolved timestamp)

### Task B: Negotiation Triggers

Negotiation generation is integrated into orchestration build when one or more of the following are detected:

- collaboration mode resolves to `deferential`
- urgent communication conflicts with autonomous physical action
- shared workspace suppresses a preferred action
- multiple safe paths exist and human preference matters
- operator presence raises decision significance

### Task C: Option Shaping

Structured options are shaped from context and may include:

- `continue_now`
- `defer_action`
- `rescan_first`
- `speak_summary_only`
- `request_confirmation_later`

### Task D: Negotiation Endpoints

Added collaboration negotiation endpoints:

- `GET /collaboration/negotiations`
- `GET /collaboration/negotiations/{id}`
- `POST /collaboration/negotiations/{id}/respond`

Endpoints also support safe fallback application for unanswered open negotiations via query controls (`apply_fallback`, `fallback_after_seconds`).

### Task E: Explainability

Each negotiation stores explainability with:

- trigger/conflict summary (`trigger_summary`)
- why human input is needed (`why_human_input_needed`)
- safe fallback if unanswered (`safe_fallback_if_unanswered`)
- human-aware state grounding (`human_context_state.signals`)

## Validation Intent

Objective 65 verifies that MIM escalates from pure autonomy to cooperative decision-making when human-aware context indicates non-trivial trade-offs, while preserving safe defaults when decisions are unanswered.
