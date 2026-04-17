# Objective 61.1: Regression Recovery and Baseline Stabilization

Objective 61.1 restores a fully green integration baseline after Objective 61 by fixing lingering Objective 49/51 regression failures and re-establishing deterministic proposal-generation behavior.

## Scope Implemented

- Regression reproduction and isolation:
  - reproduced failing suites:
    - `test_objective49_self_improvement_proposal_engine.py`
    - `test_objective51_policy_experiment_sandbox.py`
- Root-cause repair:
  - improved proposal generation idempotence in `core/improvement_service.py`.
  - when an open duplicate proposal already exists, generation now returns that actionable proposal in the response instead of silently suppressing it.
- Baseline-hardening follow-up:
  - made constraint learning aggregate only recorded outcomes (`success`/`failure`) to avoid dilution by unresolved historical rows.
  - preserved `metadata_json` in horizon goal scoring so concept influence can match zone-scoped goals deterministically.
- Baseline verification:
  - targeted Objective 49/51 suites pass.
  - targeted Objective 45/52 suites pass.
  - full integration regression returns fully green.

## Why Objective 61.1 Matters

Live perception is only operationally safe when the shared regression baseline is healthy. Objective 61.1 prevents silent baseline drift and provides a clean foundation for Objective 62 inquisitive-loop work.
