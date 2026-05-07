# Maintenance Report — Test Stack Reconciliation

Date: 2026-03-12
Scope: Shared test environment (`:8001`) reliability recovery

## Summary

The shared test stack had drifted behind current objective surface and schema, causing widespread `404` failures in full integration regression. This maintenance pass reconciled the stack to current code/schema, added a repeatable refresh script, and restored a green objective regression baseline.

## Root Cause

- Test stack was running stale build/runtime metadata (`release_tag=test-current`, `schema_version=2026-03-11-55`).
- Current objective surfaces (Objective 71+ state-bus and Objective 74 interface routes) were absent from the test manifest.
- No dedicated script existed to stamp test build metadata and enforce post-rebuild manifest parity checks.

## Changes Applied

- Added `scripts/refresh_test_stack.sh`.
  - Stamps `env/.env.test` with `BUILD_GIT_SHA`, `BUILD_TIMESTAMP`, `RELEASE_TAG`.
  - Rebuilds/restarts test compose stack.
  - Waits for `/health` and `/status` readiness.
  - Validates test manifest schema matches `core/manifest.py` `SCHEMA_VERSION`.
  - Validates required state-bus/interface endpoints are present.
  - Refreshes shared context export via `scripts/export_mim_context.py`.
- Updated Objective 54 integration assertion in:
  - `tests/integration/test_objective54_self_guided_improvement_loop.py`
  - Assertion now keys off `metadata_json.triggered_from_development_pattern` and verifies proposal trigger consistency against metadata, matching current recommendation semantics.

## Verification Evidence

### Test Stack Refresh

- Command:
  - `scripts/refresh_test_stack.sh objective-74-test-sync`
- Result:
  - `test_release_tag=objective-74-test-sync`
  - `test_schema_version=2026-03-12-67`
  - `manifest_validation=pass`

### Targeted Objective 54 Check

- Command:
  - `MIM_TEST_BASE_URL=http://127.0.0.1:8001 /home/testpilot/Desktop/MIM/.venv/bin/python -m unittest tests.integration.test_objective54_self_guided_improvement_loop`
- Result: PASS (`1/1`)

### Full Objective Regression (Shared Test)

- Command:
  - `MIM_TEST_BASE_URL=http://127.0.0.1:8001 /home/testpilot/Desktop/MIM/.venv/bin/python -m unittest discover -s tests/integration -p 'test_objective*.py'`
- Result: PASS (`66/66`)

## Outcome

- Shared test environment is reconciled and aligned with current schema/objective surface.
- Full objective regression gate is restored to a trustworthy green baseline.
- No production runtime code path changes were introduced in this maintenance pass.
