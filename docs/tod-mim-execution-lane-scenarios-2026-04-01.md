# TOD-MIM Execution Lane Scenarios (2026-04-01)

This repository now carries a synthetic execution-lane slice for Objective 102 so the execution contract can be hardened before MIM Arm participates as a broader live executor.

## Scope

- synthetic-only
- communication-contract only
- fast regression pass
- no live motor control

## Contract

Each request must target a named executor and use one of the bounded primitive commands:

- `move_to(x, y, z)`
- `move_relative(dx, dy, dz)`
- `move_relative_then_set_gripper(dx, dy, dz, position)`
- `pick_and_place(pick_x, pick_y, pick_z, place_x, place_y, place_z)`
- `pick_at(x, y, z)`
- `place_at(x, y, z)`
- `move_home()`
- `open_gripper()`
- `close_gripper()`
- `set_gripper(position)`
- `set_speed(level)`
- `stop()`

Each request may emit:

- exactly one `ack`
- exactly one terminal `result`

Invalid or stale requests may emit a rejected `ack` and no `result`.
Duplicate and superseded requests must not emit duplicate events.

## Scenario Catalog

### 1. Request Accepted, ACK Emitted, RESULT Emitted

- request targets the synthetic arm executor
- executor emits one accepted `ack`
- executor emits one successful `result`

### 2. Duplicate Request Handled Idempotently

- the same `request_id` is submitted twice
- the first submission emits `ack` and `result`
- the second submission emits no new events

### 3. Superseded Request Ignored

- a replacement request declares `supersedes_request_id`
- the replacement request is processed normally
- the superseded request later emits no `ack` and no `result`

### 4. Stale Or Wrong-Target Request Rejected

- stale requests are rejected with `ack_status=rejected`
- wrong-target requests are rejected with `ack_status=rejected`
- rejected requests do not emit terminal `result`

### 5. Timeout Or Failure Surfaced Cleanly

- accepted requests may end as `timed_out`
- accepted requests may end as `failed`
- the terminal `result.reason` must remain inspectable

### 6. Expanded Primitive Commands Stay Inside The Same Envelope

- `move_home`, `set_gripper`, `set_speed`, and `stop` reuse the same request envelope
- synthetic execution still emits one accepted `ack` and one terminal `result`
- the execution-target profile reports parameter schema and transport support for each command

### 7. Invalid Command Arguments Reject Before Execution

- out-of-range gripper positions are rejected before execution
- unsupported speed levels are rejected before execution
- rejected requests do not emit terminal `result`

### 8. Relative Motion Lineage Remains Governed

- `move_relative` participates in the same duplicate-request idempotency contract
- superseded relative-motion requests are ignored before execution
- clamped relative projections remain inspectable via translation metadata

### 9. Phase-Aware Pick Macro Reports Truthfully

- `pick_at(x, y, z)` remains a bounded scripted macro, not perception-driven manipulation
- the terminal result uses the shared macro-phase result contract: `phase`, `phase_history`, `completed_subactions`, `failed_subaction`, `interruption_cause`, `final_pose_summary`, and `end_effector_state`
- partial completion remains honest when interruption or transport failure occurs mid-macro

### 10. Phase-Aware Place Macro Reports Truthfully

- `place_at(x, y, z)` remains a bounded scripted macro, not adaptive placement logic
- bounded phases remain `move_above_target`, `descend_to_target`, `open_gripper`, `retract_or_lift`
- the terminal result uses the shared macro-phase result contract: `phase`, `phase_history`, `completed_subactions`, `failed_subaction`, `interruption_cause`, `final_pose_summary`, and `end_effector_state`
- partial completion remains honest when interruption or transport failure occurs mid-macro

### 11. Phase-Aware Pick-And-Place Transfer Reports Truthfully

- `pick_and_place(pick_x, pick_y, pick_z, place_x, place_y, place_z)` remains a bounded scripted transfer, not perception-driven manipulation intelligence
- the terminal result uses the shared macro-phase result contract: `phase`, `phase_history`, `completed_subactions`, `failed_subaction`, `interruption_cause`, `final_pose_summary`, and `end_effector_state` across both the pick and place halves
- partial completion remains honest when interruption or transport failure occurs mid-transfer

### 12. Interrupted Macro Replay Stays Explicit And Bounded

- interrupted macro commands may be replayed only through an explicit new request using `metadata_json.macro_replay.replay_of_request_id` and `metadata_json.macro_replay.resume_from_phase`
- replay is limited to previously interrupted `pick_at`, `place_at`, and `pick_and_place` requests with the same command name
- terminal macro results now expose a `replay` payload with `eligible`, `requested`, `replay_source_request_id`, `resume_from_phase`, `carried_forward_subactions`, `replayable_phases_remaining`, and `replay_reason`
- carried-forward phase history stays inspectable, and replay resumes from the failed phase instead of silently repeating completed subactions
- invalid replay requests fail truthfully with explicit replay reasons such as unknown source request, command mismatch, source not interrupted, or unknown resume phase

## Standing Gate

- narrow contract gate: `scripts/run_tod_mim_contract_gate.sh`
- synthetic execution harness: `scripts/run_tod_mim_execution_lane_simulation.py`
- regression coverage: `tests/tod/test_tod_mim_execution_lane_simulation.py`
- capability and current-state introspection surface: `GET /mim/arm/execution-target`

## Separate Live Reporting

- synthetic contract validation remains isolated in `scripts/run_tod_mim_contract_gate.sh`
- live hardware transport checks are isolated in `scripts/run_mim_arm_live_transport_check.py`
- end-to-end live smoke is isolated in `scripts/run_tod_mim_arm_live_smoke.py`
- consolidated operator wrapper runs all three and prints a PASS/FAIL matrix: `scripts/run_tod_mim_lane_matrix.py`

The matrix now carries an explicit bounded relative-motion lane in addition to the baseline no-op live transport check:

- `mim_arm_live_transport_check` validates a no-op `move_to` replay against the current pose
- `mim_arm_live_relative_transport_check` validates `move_relative(dx=5, dy=-5, dz=0)` as a small bounded live delta
- `mim_arm_live_relative_z_transport_check` validates `move_relative(dx=0, dy=0, dz=5)` to exercise the third motion axis
- `mim_arm_live_compound_transport_check` validates `move_relative_then_set_gripper(dx=5, dy=-5, dz=0, position=40)` as the first bounded compound slice
- `mim_arm_live_pick_at_transport_check` validates `pick_at(x=current_x, y=current_y, z=current_z)` as the first truthful phase-aware grasp macro slice
- `mim_arm_live_pick_and_place_transport_check` validates `pick_and_place(pick_x=current_x, pick_y=current_y, pick_z=current_z, place_x=current_x, place_y=current_y, place_z=current_z)` as the first bounded composed transfer slice
- `mim_arm_live_place_at_transport_check` validates `place_at(x=current_x, y=current_y, z=current_z)` as the first truthful phase-aware release macro slice
- `tod_mim_arm_pick_at_live_execution_smoke` validates the same bounded macro through the TOD producer and MIM HTTP surface end to end
- `tod_mim_arm_pick_and_place_live_execution_smoke` validates the same bounded transfer macro through the TOD producer and MIM HTTP surface end to end
- `tod_mim_arm_place_at_live_execution_smoke` validates the same bounded release macro through the TOD producer and MIM HTTP surface end to end
- the relative lane is intended to prove that MIM resolves deltas against the latest host pose rather than a stale cached status snapshot

These outputs must stay separate so transport flakiness does not masquerade as a synthetic contract regression.

## CI-Safe Smoke

- mocked live-host integration smoke: `tests/integration/test_tod_mim_arm_mock_live_smoke.py`
- this smoke starts a local mock arm host implementing `/arm_state` and `/move`, then drives the real TOD producer through a local MIM HTTP server
- the goal is to keep one producer-backed end-to-end lane green in CI without requiring hardware access

## Arm Integration Posture

`mim_arm` now plugs into the same request and event contract.

Live translation currently uses the host's real `/move` transport primitive plus the existing `/go_safe` and `/set_speed` routes:

- `move_to(x, y, z)` -> bounded direct projection into servo `0`, `1`, `2`
- `move_relative(dx, dy, dz)` -> bounded relative projection from the current pose into servo `0`, `1`, `2`, with explicit clamp metadata when requested angles exceed limits
- `move_relative_then_set_gripper(dx, dy, dz, position)` -> ordered relative projection into servo `0`, `1`, `2` followed by bounded gripper translation on servo `5`
- `pick_and_place(pick_x, pick_y, pick_z, place_x, place_y, place_z)` -> bounded scripted phases: pick above target, descend, close gripper, lift, move above place target, descend, open gripper, lift, with truthful phase-history reporting in the terminal result
- `pick_at(x, y, z)` -> bounded scripted phases: move above target, descend, close gripper, lift, with truthful phase-history reporting in the terminal result
- `place_at(x, y, z)` -> bounded scripted phases: move above target, descend, open gripper, retract or lift, with the shared macro-phase result contract in the terminal result
- `move_home()` -> host `/go_safe`
- `open_gripper()` -> servo `5` to the host's existing open-claw angle
- `close_gripper()` -> servo `5` to the host's existing neutral/closed angle
- `set_gripper(position)` -> servo `5` with bounded percent-to-angle translation
- `set_speed(level)` -> host `/set_speed` with bounded level-to-delay translation
- `stop()` -> host `/stop` with confirmed host acknowledgement during active motion

The live lane now treats `stop()` as transport-backed rather than contract-only. The current host truth contract distinguishes active interruption from idle acceptance:

- active-motion stop:
  - `/stop` returns `200` with `response=HOST_STOP_CONFIRMED` and `ack_source=go_safe`
  - interrupted `/go_safe` returns `409` with `status=stopped`
  - arm state exposes `serial.last_serial_event=stop_motion_honored`
- idle stop:
  - `/stop` returns `200` with `response=HOST_STOP_IDLE_NO_MOTION`, `ack_source=idle_state`, and `motion_active=false`
  - arm state exposes `serial.last_serial_event=stop_idle_no_motion`
  - idle stop does not imply interrupted motion and remains idempotent on repeated requests

This keeps the envelope unchanged while remaining honest about the host's current control model: the Pi app exposes servo-angle movement, not native Cartesian execution.

For `move_relative`, the authoritative live contract is now:

- fetch the latest host pose before translation when live transport is enabled
- compute requested servo targets by adding `dx`, `dy`, and `dz`
- clamp out-of-bounds targets to the configured servo limits
- expose both requested and applied values in translation metadata so clamp behavior is inspectable rather than silent

The first compound slice uses the same envelope rather than introducing a second protocol:

- one accepted `ack`
- one terminal `result`
- ordered dispatches preserved in the result payload
- relative motion remains bounded before the gripper step is emitted

The first bounded grasp macro keeps the same honesty rule:

- no hidden recovery or retry theater
- each phase stays inspectable in `phase_history`
- completed phases remain listed if a later phase is interrupted
- stop-driven interruption is surfaced as partial completion rather than a fake success

The bounded replay slice keeps the same operator-visible rule:

- replay is never automatic; a fresh request must opt in with `metadata_json.macro_replay`
- only interrupted macro phases may be resumed
- completed phases may be carried forward in `phase_history`, but they are marked as carried forward rather than re-executed silently
- replay result payloads remain explicit about what was resumed and why
