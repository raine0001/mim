# Objective 147 - System Awareness UI

## Goal

Expose operator-visible system-awareness state more directly in the MIM UI so the current recommendation and runtime-awareness summaries are available without reading raw payloads.

## Implemented Slice

- Added `system_awareness_visibility` to the runtime feature set in [core/routers/mim_ui.py].
- Extended `conversation_context` in [core/routers/mim_ui.py] with current-recommendation summary/source fields so downstream UI and conversation surfaces can reference current system posture directly.
- Extended the system reasoning panel renderer in [core/routers/mim_ui.py] to show a `Current recommendation` card in the operator-visible reasoning list.
- Added integration assertions in [tests/integration/test_objective84_operator_visible_system_reasoning.py] to confirm the feature flag and recommendation summary are exposed.

## Validation

- Focused operator-reasoning integration lane will cover the bounded slice:
  - `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective84_operator_visible_system_reasoning -v`

## Notes

- This slice extends the existing operator reasoning surface rather than introducing a second UI state channel.