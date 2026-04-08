# Objective 136 - Confidence Scoring

Objective 136 extends the execution strategy layer with an explicit confidence assessment that is persisted on every strategy plan.

## Delivered

- Strategy plans now publish a `confidence_assessment` snapshot with:
  - score
  - tier
  - source (`planned` or `observed`)
  - factor breakdown across intent, environment, autonomy boundary, continuation state, and domain complexity
- Confidence is recalculated when a plan is created and when it advances.
- The top-level plan `confidence` value now reflects the structured assessment instead of only the original blueprint score.

## Key Files

- `core/execution_strategy_service.py`
- `core/schemas.py`
- `tests/integration/test_objective131_135_strategy_intent_explainability.py`
