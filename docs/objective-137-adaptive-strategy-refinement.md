# Objective 137 - Adaptive Strategy Refinement

Objective 137 adds a refinement snapshot so the strategy layer can explain when the current plan should be adapted instead of blindly continued.

## Delivered

- Strategy plans now publish a `refinement_state` snapshot with:
  - whether refinement is needed
  - why refinement was triggered
  - the active adaptation count
  - the preferred alternative plan candidate
  - the step most recently superseded or retried
- Refinement is triggered by failed or blocked steps and by low confidence states.
- The refinement snapshot persists across plan advancement so resumption decisions stay coherent.

## Key Files

- `core/execution_strategy_service.py`
- `tests/integration/test_objective131_135_strategy_intent_explainability.py`
