# Objective 84 - Operator-Visible System Reasoning

Date: 2026-03-24
Status: implemented
Depends On: Objective 57, Objective 58, Objective 60, Objective 77, Objective 81, Objective 83
Target Release Tag: objective-84

## Summary

Objective 84 makes MIM's current system reasoning visible to the operator in one bounded read model.

Before this slice, the system already stored important reasoning across multiple subsystems:

- strategy-goal prioritization and reasoning summaries
- inquiry policy state and waiting decisions
- execution-truth governance decisions
- autonomy-boundary adaptation reasoning
- stewardship verification and follow-up state

Those signals existed, but they were distributed across different API surfaces and model rows. The `/mim` operator page did not expose them as one inspectable operator-facing explanation.

Objective 84 closes that gap by adding a unified `operator_reasoning` payload to `/mim/ui/state` and rendering it directly in the MIM UI.

## Implemented Surface

Objective 84 is implemented in the existing MIM operator UI surface rather than introducing a new subsystem.

Primary implementation file:

- `core/routers/mim_ui.py`

Delivered behavior:

- `/mim/ui/state` now returns a top-level `operator_reasoning` object
- `/mim/ui/state` now advertises `operator_reasoning_summary` in `runtime_features`
- `conversation_context` now includes `operator_reasoning_summary`
- `/mim` now renders a dedicated System Reasoning panel using the live state payload

The aggregated reasoning bundle includes:

- `active_goal`
- `inquiry`
- `governance`
- `autonomy`
- `stewardship`
- `summary`

## Operator Reasoning Contract

The bounded operator payload is intended to answer five practical questions:

1. What goal is currently driving behavior?
2. Is there an inquiry blocking or shaping progress?
3. Has execution-truth governance changed the system posture?
4. What autonomy level is currently active and why?
5. Is stewardship follow-up actively being generated or suppressed?

The summary field is intentionally compact so it can be shown inline in the operator UI and reused in conversational state without dumping raw internal rows.

## Why Objective 84 Matters

Autonomy is harder to trust when its reasoning is only visible through separate database-backed surfaces.

Objective 84 improves operator trust by making the current reasoning chain inspectable in one place:

- goal pressure
- inquiry pressure
- governance pressure
- autonomy pressure
- stewardship pressure

This is not a new decision engine. It is an operator-visible read model over reasoning that already exists.

## Validation

Focused Objective 84 integration lane:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective84_operator_visible_system_reasoning -v`
- result: `Ran 3 tests ... OK`

Runtime-target hardening:

- the Objective 84 integration harness now defaults to `http://127.0.0.1:18001`
- it fails fast if the target runtime does not expose the current-source Objective 84 surfaces
- stale runtimes are rejected when `/mim` lacks `systemReasoningPanel`, `/mim/ui/state` lacks the operator reasoning surface, or `/execution-truth/governance/evaluate` returns `404`

Adjacent regression lane after scope-coherence hardening:

- `tests.integration.test_objective77_mim_ui_conversation_policy_bridge`
- `tests.integration.test_objective81_execution_truth_governance_loop`
- `tests.integration.test_objective83_governed_inquiry_resolution_loop`
- `tests.integration.test_objective84_operator_visible_system_reasoning`
- result: `Ran 20 tests ... OK`

Validated behaviors:

- `/mim` contains System Reasoning panel hooks and client render logic
- `/mim/ui/state` exposes a populated `operator_reasoning` payload
- `operator_reasoning` stays scope-coherent when newer unrelated subsystem rows exist for a different managed scope
- the payload carries a compact summary plus bounded goal, inquiry, governance, autonomy, and stewardship snapshots
- `conversation_context["operator_reasoning_summary"]` mirrors the top-level summary for reuse in UI and conversation state

Live runtime probe on `http://127.0.0.1:18001/mim/ui/state` confirmed:

- `runtime_features` includes `operator_reasoning_summary`
- the payload surfaces live governance, autonomy, inquiry, and stewardship state for the exercised Objective 84 scenario

## Closure Checkpoint

Date: 2026-04-04

This runtime-hardening closure is frozen with the following exact passing live validation set against the current-source runtime target:

- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective75_interface_hardening -v`
  - result: `Ran 7 tests ... OK`
- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective23_operator_control -v`
  - result: `Ran 1 test in 2.417s ... OK`
- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective37_human_aware_interruption_and_safe_pause_handling -v`
  - result: `Ran 1 test in 3.555s ... OK`
- `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective38_predictive_workspace_change_and_replanning -v`
  - result: `Ran 1 test in 3.808s ... OK`

Validation-target rule for this hardened lane:

- `http://127.0.0.1:18001` is the current-source integration target
- target selection is now treated as deployment-topology policy, not per-suite tuning
- validation target logic should remain unchanged unless deployment topology is being changed deliberately

## Exit Criteria

Objective 84 is complete when all are true:

1. operator-facing UI state includes one unified reasoning payload
2. the `/mim` page renders that payload in a dedicated panel
3. goal, inquiry, governance, autonomy, and stewardship reasoning remain individually inspectable
4. a compact summary is available for UI reuse without exposing raw internal rows everywhere
5. focused integration coverage proves both the UI hooks and the live state bundle
