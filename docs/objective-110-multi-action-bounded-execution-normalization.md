# Objective 110: Multi-Action Bounded Execution Normalization

## Status

- Implemented in the current source workspace.
- Focused bounded-action validation is pending or in progress at this checkpoint.

## Goal

Objective 110 standardizes the bounded MIM ARM execution path so every bounded action uses the same attribution contract instead of inheriting `safe_home`-specific defaults.

The immediate normalization slice locks in the shared contract already proven for:

- `safe_home`
- `scan_pose`

## Implemented Changes

- Removed bounded execution wrapper indirection in `core/routers/mim_arm.py` so action routing goes directly through `_build_bounded_pose_event_and_resolution(...)`.
- Removed `safe_home` fallback behavior from `_resolve_execution_action_name(...)`; callers must now provide the action explicitly instead of inheriting a hidden default.
- Removed the `mim_arm.execute_safe_home` fallback from publish-time dispatch telemetry recording so telemetry reflects the execution capability supplied by the live execution record.
- Added focused regression coverage proving action resolution stays explicit and does not silently collapse to `safe_home`.

## Contract Direction

Objective 110 establishes the normalization baseline for future bounded actions:

1. common dispatch telemetry schema
2. common ACK and RESULT matching rules
3. common host attribution fields
4. common proof harness expectations
5. no action-specific fallback defaults in the bounded execution path

## Acceptance Boundary

Objective 110 is complete for this slice when:

1. `safe_home` and `scan_pose` execute through the same bounded event and resolution builder
2. dispatch telemetry derives capability and identity from the actual execution record rather than a `safe_home` fallback
3. bounded action resolution requires explicit action identity instead of defaulting to `safe_home`
4. focused validation confirms the shared path still holds for both bounded actions

## Next Work

- Extend the same normalized attribution contract to the next bounded arm actions added to the live execution lane.
- Keep proof harness expectations action-agnostic as the bounded action set expands.