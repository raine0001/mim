# MIM Arm Recovery and Integration Baseline

Date: 2026-03-29
Status: drafted

## Goal

Restore the MIM arm UI application first under TOD-led bounded diagnostics, then expose arm state to MIM as a read-only input surface before any live control is considered.

## First Milestone

TOD can diagnose and restore the MIM arm app, and MIM can read the arm's live status safely.

This milestone explicitly excludes live motion control.

## Project Structure

### Task 1

Diagnose why the MIM arm UI app is not starting on `192.168.1.90`.

Constraints:

- no live motion commands
- no config changes without approval
- gather logs and root-cause candidates first
- prefer read-only inspection before remediation
- produce artifact summary and next-step recommendation

## Required Diagnostic Coverage

The startup diagnostic should include:

- host reachable/unreachable
- active processes
- service status
- startup script/command
- recent app logs
- camera device availability
- serial/controller port availability
- disk/memory basics
- Python/env dependency failures
- port binding conflicts

## Required First Artifact

Artifact name:

- `mim_arm_startup_diagnostic.latest.json`

Required sections:

- `connectivity`
- `process_service`
- `logs`
- `devices`
- `system_basics`
- `ports`
- `likely_root_cause`
- `suggested_recovery_actions`

A committed template exists at [config/mim_arm_startup_diagnostic.template.json](config/mim_arm_startup_diagnostic.template.json).

A bounded collector exists at [scripts/generate_mim_arm_startup_diagnostic.py](scripts/generate_mim_arm_startup_diagnostic.py).

## Remediation Policy

Once the diagnostic artifact is clean enough to support action, TOD may propose bounded remediation steps, but each step requires explicit operator approval.

Examples:

- restart service
- run startup script manually
- reinstall missing dependency
- correct path/env mismatch
- release occupied port
- restore config file
- verify camera permission/device mapping

## Post-Recovery Integration Phase

Only after the UI app is healthy, expose read-only arm state into MIM.

Read-only fields:

- app alive/dead
- arm status
- camera status
- current pose
- estop status
- active mode
- last error
- recent command result

## Sequencing

1. TOD restores the arm app.
2. MIM ingests read-only arm state.
3. TOD gates any live execution path.
4. MIM proposes bounded actions.

## Why This Order

Using MIM as the direct kickoff interface before the arm app and state surfaces are healthy adds risk without adding useful leverage.

The sane order is:

- TOD first for troubleshooting and bounded remediation
- MIM second for read-only awareness and reasoning
- MIM UI later as a supervisory layer after recovery is stable

## Task Artifact

A TOD-style task template for this kickoff exists at [tod/state/mim_arm_recovery_task.template.json](tod/state/mim_arm_recovery_task.template.json).

## Current Implementation Status

Implemented now:

- Stage 1 read-only awareness router at `/mim/arm/status`, `/mim/arm/pose`, `/mim/arm/camera-state`, and `/mim/arm/last-execution`
- Stable artifact-backed status surface in `core/routers/mim_arm.py`
- Status materializer script at `scripts/generate_mim_arm_status.py` which writes `runtime/shared/mim_arm_status.latest.json`
- Arm-host producer script at `scripts/generate_mim_arm_host_state.py` for the Pi side
- MIM-side sync script at `scripts/sync_mim_arm_host_state.py` to pull the host truth into `runtime/shared/mim_arm_status.latest.json`
- Stage 2 proposal-only routes for `safe_home`, `scan_pose`, and `capture_frame`
- Stage 3 first bounded live motion route: `/mim/arm/executions/safe-home`

Current live-motion constraints:

- executor remains TOD only
- first live command is safe-home only
- explicit operator approval is still required for first-phase live motion
- degraded self-health remains confirmation-gated unless explicit approval is present
- hard physical safety posture still blocks by default, with explicit approval recorded when it overrides the posture

Current validation coverage:

- read-only direct status artifact contract
- real host-state artifact contract (`mim_arm_host_state.latest.json` passthrough)
- read-only diagnostic fallback contract
- proposal-only safe-home posture contract
- idempotent capability bootstrap
- safe-home live route requires confirmation without explicit approval
- mixed physical scenario preserves both `user_action_safety_risk` and `system_health_degraded` in governance metadata

Current operational gap:

- SSH-backed host-state sync is now working from this environment, and MIM can refresh bounded arm truth from the Pi into local shared artifacts.
- Remaining promotion blockers are governance, not connectivity: TOD execution readiness still has to be current, and explicit emergency-stop support is still absent from the arm host surface.
