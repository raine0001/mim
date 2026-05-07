# Objective 150 - Lightweight Autonomy

## Goal

Expose a compact autonomy posture that tells the operator whether MIM can continue automatically in bounded cases or whether current safeguards hold execution behind review.

## Implemented Slice

- Added a `lightweight_autonomy` snapshot in [core/routers/mim_ui.py] derived from the active autonomy profile, trust explainability, and current recommendation.
- Added `lightweight_autonomy_guidance` to the runtime feature set in [core/routers/mim_ui.py].
- Extended the system reasoning panel in [core/routers/mim_ui.py] to show a `Lightweight autonomy` card.
- Added a direct conversation answer in [core/routers/gateway.py] for `can you continue automatically` style questions.
- Added focused lifecycle coverage in [tests/test_objective_lifecycle.py] for the lightweight-autonomy reply.

## Validation

- Focused lifecycle unit lane will cover the conversation slice:
  - `/home/testpilot/mim/.venv/bin/python -m unittest tests.test_objective_lifecycle -v`

## Notes

- This bounded slice does not change autonomy policy evaluation. It surfaces the already-computed posture in a more operator-usable form.