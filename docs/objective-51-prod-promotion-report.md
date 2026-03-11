# Objective 51 Production Promotion Report

Generated at: 2026-03-11 (UTC)
Environment target: production
Release tag: sha-40b28d2

## Promotion Result

- Promotion command:
  - `scripts/promote_test_to_prod.sh objective-51`
- Result: PASS

## Contract and Capability Targets

- schema version: `2026-03-11-42`
- capability includes `policy_experiment_sandbox`: `true`
- endpoints live:
  - `/improvement/experiments/run`
  - `/improvement/experiments`
  - `/improvement/experiments/{experiment_id}`

## Pre-Promotion Regression Evidence

- Focused gate:
  - `tests/integration/test_objective51_policy_experiment_sandbox.py`: PASS (`Ran 1 test ... OK`)
- Full integration regression:
  - `python -m unittest discover tests/integration -v`: PASS (`Ran 43 tests in 20.909s ... OK`)
- Production probe:
  - `MIM_TEST_BASE_URL=http://127.0.0.1:8000 /home/testpilot/mim/.venv/bin/python -m unittest tests/integration/test_objective51_policy_experiment_sandbox.py -v`: PASS (`Ran 1 test ... OK`)

## Experiment Isolation Verification

- baseline policy decision output remained unchanged before/after sandbox run:
  - before: `allowed_with_conditions`
  - after: `allowed_with_conditions`
- sandbox experiment metrics persisted separately from baseline:
  - baseline friction events: `35`
  - experimental friction events: `28`
  - baseline != experimental: `true`
- experiment record persisted independently:
  - `experiment_persisted=true`

## Post-Promotion Verification Checklist

- [x] `GET /health`: PASS
- [x] `GET /manifest`: PASS
  - [x] `release_tag=sha-40b28d2`
  - [x] `schema_version=2026-03-11-42`
  - [x] capability includes `policy_experiment_sandbox=true`
- [x] endpoint availability:
  - [x] `/improvement/experiments/run`
  - [x] `/improvement/experiments`
  - [x] `/improvement/experiments/{experiment_id}`
- [x] production probe test for Objective 51: PASS

## Verdict

PROMOTED AND VERIFIED
