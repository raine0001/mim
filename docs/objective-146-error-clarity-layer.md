# Objective 146 - Error Clarity Layer

## Goal

Make deterministic conversation failures easier to interpret by returning clear boundary messages for unsafe requests, private-runtime disclosure requests, unsupported external-action assumptions, and ambiguous external-action instructions.

## Implemented Slice

- Added explicit boundary detection in [core/routers/gateway.py] for unsafe or risky requests, private runtime disclosure requests, ambiguous external-action requests, and unsupported claims that an external action already happened.
- Tightened resume-control matching in [core/routers/gateway.py] so `go ahead and do ...` is no longer misclassified as a pure resume command.
- Strengthened [conversation_eval_runner.py] with an `offer_safe_alternative` expectation so unsafe-refusal regressions fail when the response refuses but does not redirect constructively.
- Added focused lifecycle coverage in [tests/test_objective_lifecycle.py] for the new error-clarity branches and safe-alternative scoring.

## Validation

- Focused lifecycle unit lane will cover the bounded slice:
  - `/home/testpilot/mim/.venv/bin/python -m unittest tests.test_objective_lifecycle -v`

## Notes

- This slice is limited to the deterministic conversation layer. It does not change deeper execution-policy or transport-level error handling.