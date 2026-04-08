# Objective 149 - Operator Trust Signals

## Goal

Expose the operator-facing trust cues already present in strategy planning so the UI shows what MIM did, what it plans next, why it believes the current posture is safe, and what recommendation is active.

## Implemented Slice

- Added `trust_signal_summary` generation in [core/routers/mim_ui.py] from existing strategy explainability and current recommendation state.
- Added `operator_trust_signals` to the runtime feature set in [core/routers/mim_ui.py].
- Extended the system reasoning panel renderer in [core/routers/mim_ui.py] to show a `Trust signals` card with confidence tier, safe-to-continue posture, and stop reasons when present.
- Added integration assertions in [tests/integration/test_objective84_operator_visible_system_reasoning.py] to verify trust explainability and trust-summary propagation into UI state.

## Validation

- Focused operator-reasoning integration lane will cover the bounded slice:
  - `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective84_operator_visible_system_reasoning -v`

## Notes

- This bounded slice reuses existing strategy explainability data; it does not create a second trust model.