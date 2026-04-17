# Objective 97 Bridge Recovery Dispatch Record

Status: posted

## Summary

The TOD bridge-recovery project for the MIM arm follow-up was posted, but the rotating shared `latest` request artifact was later overwritten by the overnight reliability loop.

## Persistent TOD State Task

- Task id: `1774884550`
- Objective id: `97`
- Title: `Recover TOD ACK bridge and enforce dispatch readiness gate`
- Status: `queued`

Source:
- [tod/state/tasks.json](tod/state/tasks.json#L1)

## Bridge-Recovery Trigger Evidence

Latest active bridge-recovery trigger payload:
- Task id: `objective-97-task-1774884347-bridge-recovery`
- Correlation id: `obj97-task1774884347-bridge-recovery`

Source:
- [runtime/shared/MIM_TO_TOD_TRIGGER.latest.json](runtime/shared/MIM_TO_TOD_TRIGGER.latest.json#L1)

## Historical Trigger Evidence

Recorded bridge-recovery task request posts in trigger history:
- `objective-97-task-1774884218-bridge-recovery`
- `objective-97-task-1774884278-bridge-recovery`
- `objective-97-task-1774884347-bridge-recovery`

Sources:
- [runtime/shared/SHARED_TRIGGER_EVENTS.latest.jsonl](runtime/shared/SHARED_TRIGGER_EVENTS.latest.jsonl#L84255)
- [runtime/shared/SHARED_TRIGGER_EVENTS.latest.jsonl](runtime/shared/SHARED_TRIGGER_EVENTS.latest.jsonl#L84262)
- [runtime/shared/TOD_LIVENESS_EVENTS.latest.jsonl](runtime/shared/TOD_LIVENESS_EVENTS.latest.jsonl#L15385)

## Why It Was Hard To See

The shared request artifact `MIM_TOD_TASK_REQUEST.latest.json` is not immutable. It was later replaced by the overnight reliability loop request, so the arm-related bridge-recovery post does not remain visible there as the current payload.

Current overwritten latest request:
- [runtime/shared/MIM_TOD_TASK_REQUEST.latest.json](runtime/shared/MIM_TOD_TASK_REQUEST.latest.json#L1)

## Current Limitation

The task was posted, but TOD has not produced a fresh ACK for the bridge-recovery task id yet. The active ACK file still references an older task:
- [runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json](runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json#L1)

## 2026-03-30 Coordination Recovery Pass

Status: pending coordination ACK path restoration

Confirmed outcomes from the latest recovery cycle:
- The TOD-side auto-resolve guard now preserves non-regression coordination requests and no longer overwrites alias-handoff requests when regression goes green.
- Escalation state now tracks the manual coordination request id as pending instead of falsely resolved.
- TOD UI proposal ingestion now falls back to `project_question` when `title` is absent, so coordination-proof requests no longer produce empty proposal titles.

Live blocker after the above fixes:
- MIM had no active daemon watching [runtime/shared/TOD_MIM_COORDINATION_REQUEST.latest.json](runtime/shared/TOD_MIM_COORDINATION_REQUEST.latest.json#L1), so [runtime/shared/MIM_TOD_COORDINATION_ACK.latest.json](runtime/shared/MIM_TOD_COORDINATION_ACK.latest.json#L1) did not advance for the pending request.

MIM-side responder remediation introduced in this repo:
- New watcher script: [scripts/watch_mim_coordination_responder.sh](scripts/watch_mim_coordination_responder.sh)
- New user service unit: [deploy/systemd-user/mim-watch-mim-coordination-responder.service](deploy/systemd-user/mim-watch-mim-coordination-responder.service)
- Installer wiring updated: [scripts/install_objective75_user_units.sh](scripts/install_objective75_user_units.sh)

Expected ACK contract emitted by the responder:
- request_id
- objective_id
- ack_status (`pending`)
- generated_at/emitted_at
- source_host/source_service/source_instance_id
- coordination.status/phase/detail and pending_request_id context

## 2026-03-30 Collaborative Execution Across All 3 Workstreams

Status: in progress (MIM publish and guard complete, TOD consume evidence pending)

Task under collaborative dispatch:
- objective-97-task-1774894698-mim-arm-readiness

### Workstream 1: ACK/RESULT pickup monitoring

MIM side completed:
- Local request and trigger were rewritten to the collaborative task id.
- Remote shared-root publication was verified for both request and trigger packets.
- A long-window consume-evidence watcher now runs autonomously and captures first ACK/RESULT mutation timestamps for the active collaborative task when TOD consumes.

TOD side pending:
- First consume-side mutation in ACK/RESULT artifacts for the same collaborative task id.

Evidence:
- [runtime/shared/MIM_TOD_TASK_REQUEST.latest.json](runtime/shared/MIM_TOD_TASK_REQUEST.latest.json#L1)
- [runtime/shared/MIM_TO_TOD_TRIGGER.latest.json](runtime/shared/MIM_TO_TOD_TRIGGER.latest.json#L1)
- [runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json](runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json#L1)
- [runtime/shared/TOD_MIM_TASK_ACK.latest.json](runtime/shared/TOD_MIM_TASK_ACK.latest.json#L1)
- [runtime/shared/TOD_MIM_TASK_RESULT.latest.json](runtime/shared/TOD_MIM_TASK_RESULT.latest.json#L1)
- [runtime/shared/MIM_TOD_CONSUME_EVIDENCE.latest.json](runtime/shared/MIM_TOD_CONSUME_EVIDENCE.latest.json#L1)
- [deploy/systemd-user/mim-watch-tod-consume-evidence.service](deploy/systemd-user/mim-watch-tod-consume-evidence.service#L1)
- [scripts/watch_tod_consume_evidence.sh](scripts/watch_tod_consume_evidence.sh#L1)

### Workstream 2: No-stomp writer guard

MIM side completed:
- Manual dispatch lock was posted to protect the collaborative dispatch window.
- Overnight writer now pauses while the manual lock is active.

Evidence:
- [runtime/shared/MIM_TOD_MANUAL_DISPATCH_LOCK.latest.json](runtime/shared/MIM_TOD_MANUAL_DISPATCH_LOCK.latest.json#L1)
- [scripts/run_objective75_overnight_loop.sh](scripts/run_objective75_overnight_loop.sh#L1)
- [runtime/logs/objective75_overnight.log](runtime/logs/objective75_overnight.log#L1)

### Workstream 3: Joint evidence record

MIM side completed:
- Published a collaboration status artifact capturing role ownership and per-workstream status.
- Updated this dispatch record with shared evidence links and current blocker definition.

TOD side pending:
- Add consume-side ACK/RESULT mutation evidence for objective-97-task-1774894698-mim-arm-readiness.

Evidence:
- [runtime/shared/MIM_TOD_COLLAB_PROGRESS.latest.json](runtime/shared/MIM_TOD_COLLAB_PROGRESS.latest.json#L1)
- [docs/objective-97-bridge-recovery-dispatch-record.md](docs/objective-97-bridge-recovery-dispatch-record.md#L1)
