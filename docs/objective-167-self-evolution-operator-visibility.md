# Objective 167 - Self-Evolution Operator Visibility

## Goal

Expose the bounded self-evolution packet from Objectives 164-166 in the existing MIM operator-facing UI surface so operators can inspect the current self-evolution state without leaving `/mim/ui/state` or the system reasoning panel.

## Implementation

- Reused the Objective 166 `build_self_evolution_briefing(...)` contract rather than adding a new self-evolution engine.
- Added a self-evolution snapshot into `operator_reasoning` in `core/routers/mim_ui.py`.
- Added a concise `self_evolution_summary` bridge into `conversation_context` for downstream conversational surfaces.
- Added a new system reasoning entry in the `/mim` page so the current self-evolution decision and target summary are operator-visible in the panel.
- Registered the new operator-facing capability token `self_evolution_operator_visibility` in the manifest surfaces.

## Contract

`GET /mim/ui/state` now exposes:

- `operator_reasoning.self_evolution.summary`
- `operator_reasoning.self_evolution.status`
- `operator_reasoning.self_evolution.decision_type`
- `operator_reasoning.self_evolution.priority`
- `operator_reasoning.self_evolution.target_kind`
- `operator_reasoning.self_evolution.target_id`
- `operator_reasoning.self_evolution.target_summary`
- `operator_reasoning.self_evolution.snapshot`
- `operator_reasoning.self_evolution.decision`
- `operator_reasoning.self_evolution.target`

`conversation_context` now mirrors the concise self-evolution summary in:

- `self_evolution_summary`

## Validation

- Focused integration lane:
  - `tests.integration.test_objective54_self_guided_improvement_loop`
  - `tests.integration.test_objective55_improvement_prioritization_governance`
  - `tests.integration.test_objective164_self_evolution_core`
  - `tests.integration.test_objective165_self_evolution_next_action`
  - `tests.integration.test_objective166_self_evolution_briefing`
  - `tests.integration.test_objective167_self_evolution_operator_visibility`