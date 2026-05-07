# MIM Execution-Bound Intent Routing Harness Report

## Root Cause

The MIM text console sent direct text input to `/gateway/intake/text` with `route_preference=conversation_layer`. The gateway honored that preference before evaluating local execution-bound robotics directives. Once in the conversation layer, the web research fallback could claim requests that contained broad research/implementation markers, producing replies such as "I tried to research that on the web..." for bounded local arm work.

The failing directive was:

`MIM-ARM-MULTI-SERVO-ENVELOPE-PROBE-PREP`

That request is not an informational query. It is an execution-bound robotics probe preparation directive and must be classified before web/search fallback.

## Routing Path

The new simulation harness maps console text through:

1. `input_gateway`
2. `intent_classifier`
3. `capability_to_goal_bridge`
4. `robotics_capability_registry`
5. `execution_binding`

For true external research requests, the path may instead continue to `web_search_fallback`.

## Classifier Outcomes

The harness now exposes explicit outcomes:

- `execution_capability_request`
- `robotics_supervised_probe`
- `informational_query`
- `web_research_request`
- `unclear_requires_clarification`

## Guardrail

Web search is blocked for local robotics terms unless the operator explicitly asks for public/web information. Guarded terms:

- `servo`
- `gripper`
- `arm`
- `safe_home`
- `supervised probe`
- `motion_allowed`
- `estop_ok`
- `learned_bounds`

## Files Changed

- `core/intent_routing_service.py`
  - Added pure console intent classifier and routing simulation harness.
- `core/routers/gateway.py`
  - Evaluates execution-bound local robotics directives before honoring conversation/web fallback.
  - Maps `robotics_supervised_probe` and `execution_capability_request` to `execute_capability`.
  - Bridges robotics probe directives to `mim_arm.supervised_probe`.
  - Blocks web research when robotics guard terms are present without explicit public-info language.
- `core/routers/mim_arm.py`
  - Adds `mim_arm.supervised_probe` to the robotics capability registry bootstrap definitions.
- `tests/test_mim_execution_bound_intent_routing.py`
  - Regression harness covering the original directive, bounded arm commands, real web research, and ambiguous commands.

## Tests Run

- `.venv/bin/python -m unittest tests.test_mim_execution_bound_intent_routing`
- `.venv/bin/python -m unittest tests.test_mim_execution_bound_intent_routing tests.test_mim_ui_chat_messages tests.test_public_chat_router tests.test_shell_router tests/tod/test_tod_mim_execution_lane_simulation.py tests/tod/test_tod_mim_arm_execution_request_producer.py`

Both runs passed.

## Remaining Risks

- Existing deployments need `/mim/arm/capabilities/bootstrap` or equivalent startup bootstrap to register the new `mim_arm.supervised_probe` capability row in persistent databases.
- The new gateway path creates/binds a governed local capability intent; actual physical probe execution still depends on the existing arm envelope authorization and operator approval surfaces.
