# TOD-MIM Communication Policy Authority (2026-04-01)

This file is the repo-native mirror used by MIM-side synthetic gating. The shareable policy authority remains the consolidated April 1 document already used operationally; this mirror must stay behaviorally aligned with it.

## Authoritative Inbox Rule

- `runtime/shared/dialog/MIM_TOD_DIALOG.sessions.latest.json` is the actionable inbox
- `runtime/shared/dialog/MIM_TOD_DIALOG.latest.jsonl` is append-only history only
- MIM must not inspect generic history first when an actionable session index is available

## Session Reply Rule

- find the open or actionable session addressed to `MIM`
- open the exact referenced session log under `runtime/shared/dialog`
- answer on that same session with `handoff_response`
- preserve `session_id`
- include `reply_to_turn` when the request turn is known

## Next-Step Consensus Contract

When the active request is `intent=next_step_consensus`, MIM must return:

- `summary`
- `finding_positions`

Each `finding_positions` entry must include:

- `finding_id`
- `decision`
- `reason`
- `confidence`
- `local_blockers`

## Timed-Out Session Rule

A session can still be actionable when the index shows:

- `status=timed_out`
- `open_reply.to=MIM`
- `open_reply.message_type=handoff_request`
- the latest message is a reminder or location-hint `status_request`

`timed_out` does not by itself mean the request is closed.

## Mirrored Session Path Rule

If the indexed `session_path` is mirrored from another platform and uses a Windows-style absolute path, MIM should resolve the filename into the local dialog root instead of rejecting the session.

## Synthetic Harness Constraint

The simulation harness in this repository must use synthetic roots only. It is for interoperability training and regression detection, not live coordination.

## TOD Bridge Boundary Rule

- The operative TOD↔MIM communication boundary is the MIM-owned shared root on the MIM server: `/home/testpilot/mim/runtime/shared`
- The ARM Pi at `192.168.1.90` is an executor-side system for MIM Arm transport and diagnostics, not the authoritative communication channel for TOD↔MIM contract traffic
- Contract authority, communication receipts, heartbeat authority, and request truth must stay on the MIM server unless MIM explicitly defines a different communications server surface in the contract
- ARM-specific publishers and diagnostics may still talk to the Pi for arm execution support, but that path must remain isolated from the primary TOD↔MIM communication truth model
