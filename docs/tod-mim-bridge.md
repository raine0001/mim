# TOD ↔ MIM Bridge (v1)

## Operating Rule

TOD directs work. MIM persists and contextualizes work.

## Connection

- Base URL: `http://192.168.1.120:8000`
- Recommended mode: `hybrid`

Example TOD config:

```json
{
  "mim_base_url": "http://192.168.1.120:8000",
  "mode": "hybrid",
  "timeout_seconds": 15,
  "fallback_to_local": true
}
```

## Shared Contract Objects

### Objective

- `objective_id`
- `title`
- `description`
- `priority`
- `constraints`
- `success_criteria`
- `status`
- `created_at`

### Task

- `task_id`
- `objective_id`
- `title`
- `scope`
- `dependencies`
- `acceptance_criteria`
- `status`
- `assigned_to`

### Result

- `result_id`
- `task_id`
- `summary`
- `files_changed`
- `tests_run`
- `test_results`
- `failures`
- `recommendations`
- `created_at`

### Review

- `review_id`
- `task_id`
- `decision`
- `rationale`
- `continue_allowed`
- `escalate_to_user`
- `created_at`

### JournalEntry

- `entry_id`
- `actor`
- `action`
- `target_type`
- `target_id`
- `summary`
- `timestamp`

## MIM endpoints for TOD

- `GET /health`
- `GET /status`
- `GET /manifest`
- `POST /objectives`
- `GET /objectives`
- `POST /tasks`
- `GET /tasks`
- `POST /results`
- `GET /results`
- `POST /reviews`
- `GET /reviews`
- `POST/GET /journal`

### Execution integration (Objective 22)

- `GET /gateway/capabilities/executions/{execution_id}/handoff`
- `POST /gateway/capabilities/executions/{execution_id}/feedback`
- `GET /gateway/capabilities/executions/{execution_id}/feedback`

Execution handoff payload includes:

- `execution_id`
- `goal_ref` and `action_ref`
- `capability_name`
- `arguments_json`
- `safety_mode`
- `correlation_metadata`

Execution feedback posting supports:

- direct status updates (`accepted`, `running`, `succeeded`, `failed`, `blocked`)
- runtime outcome mapping:
  - `executor_unavailable` → `failed`
  - `guardrail_blocked` → `blocked`
  - `retry_in_progress` → `running`
  - `fallback_used` → `running`
  - `recovered` → `succeeded`
  - `unrecovered_failure` → `failed`

Feedback auth/safety boundary:

- actor allow-list enforced by MIM (`execution_feedback_allowed_actors`)
- optional shared key header `X-MIM-Feedback-Key` when MIM `execution_feedback_api_key` is set

## TOD command mapping

- `ping-mim` → `GET /health`, `GET /status`
- `new-objective` → `POST /objectives`
- `list-objectives` → `GET /objectives`
- `add-task` → `POST /tasks`
- `list-tasks` → `GET /tasks`
- `add-result` → `POST /results`
- `review-task` → `POST /reviews`
- `show-journal` → `GET /journal`

## PowerShell client scaffold (TOD side)

Place these in TOD:

- `client/mim_api_client.ps1`
- `client/mim_api_helpers.ps1`

Required functions:

- `Get-MimHealth`
- `Get-MimStatus`
- `New-MimObjective`
- `Get-MimObjectives`
- `New-MimTask`
- `Get-MimTasks`
- `New-MimResult`
- `New-MimReview`
- `Get-MimJournal`

## Standard Operating Loop (Future Default)

This loop is the default operating method for all future MIM↔TOD projects.

### Phase 1 — MIM posts project/job/task

MIM publishes a task packet in shared state with objective, scope, constraints, success criteria, and due state.

Required packet fields:

- `task_id`
- `objective_id`
- `title`
- `scope`
- `constraints`
- `acceptance_criteria`
- `required_tests`
- `submission_requirements`
- `requested_by`
- `created_at`

### Phase 2 — TOD receives notice and acknowledges

TOD performs a refresh/pull, reads the latest task packet, and publishes an acknowledgement packet.

Acknowledgement outcomes:

- `confirmed` (ready to execute)
- `questions` (blocking clarifications needed)
- `rejected` (cannot execute under current constraints)

If TOD has questions, MIM answers in a clarification response packet before go-order.

### Phase 3 — MIM final go-order

After TOD confirms readiness (or clarifications are resolved), MIM publishes a final go-order packet with explicit execution authorization and any last constraints.

### Phase 4 — TOD executes and submits

TOD performs work and publishes a result packet with:

- implementation summary
- changed artifacts
- tests executed
- test outcomes
- known limitations/issues
- recommendation (`ready_for_review` / `needs_iteration`)

### Phase 5 — MIM review and decision

MIM reviews submission and returns one of:

- `accepted`
- `repeat_with_changes`
- `closed_no_action`

MIM includes closeout notes and, when accepted, captures final evidence links.

### Phase 6 — TOD journals and loop state

TOD appends journal entries and either:

- starts next queued task (`loop_state=continue`), or
- marks end-of-cycle (`loop_state=end`).

## Shared Packet Conventions

Store packets under `runtime/shared` with deterministic latest filenames:

- `MIM_TOD_TASK_REQUEST.latest.json`
- `TOD_MIM_TASK_ACK.latest.json`
- `MIM_TOD_GO_ORDER.latest.json`
- `TOD_MIM_TASK_RESULT.latest.json`
- `MIM_TOD_REVIEW_DECISION.latest.json`
- `TOD_LOOP_JOURNAL.latest.json`

Realtime trigger packets:

- `MIM_TO_TOD_TRIGGER.latest.json`
- `TOD_TO_MIM_TRIGGER.latest.json`
- `MIM_TO_TOD_TRIGGER_ACK.latest.json`
- `TOD_TO_MIM_TRIGGER_ACK.latest.json`

All packets should include:

- `generated_at` (UTC ISO8601)
- `packet_type`
- `handshake_version`
- `task_id`
- `objective_id`
- `correlation_id`

## Start-Now Rule for First Task

Yes—start immediately with the first task under this loop.

First task recommendation:

- objective: Objective 75 interface hardening gate validation
- task: TOD refresh + publish status showing compatibility/alignment
- done when: `compatible=true` and `objective_alignment.status=aligned`

## Realtime Trigger Layer (Recommended)

To reduce polling latency and keep both sides responsive, run a trigger watcher on each side that observes `runtime/shared` packet updates.

### Trigger Behavior

- When MIM updates a `MIM_*.latest.json` artifact, emit/update `MIM_TO_TOD_TRIGGER.latest.json`.
- When TOD updates a `TOD_*.latest.json` artifact, emit/update `TOD_TO_MIM_TRIGGER.latest.json`.
- Receiver performs immediate pull/reload and writes ACK packet:
  - `TOD_TO_MIM_TRIGGER_ACK.latest.json` (when TOD processed MIM trigger)
  - `MIM_TO_TOD_TRIGGER_ACK.latest.json` (when MIM processed TOD trigger)

### Trigger SLA

- Target detection latency: <= 2 seconds (watcher poll interval).
- Target processing latency: <= 10 seconds from trigger creation to ACK write.
- Missing ACK beyond SLA should be treated as degraded link state and logged.

### Script Support

MIM provides watcher helper:

- `scripts/watch_shared_triggers.sh`

MIM provides TOD liveness watchdog:

- `scripts/watch_tod_liveness.sh`

Defaults:

- `SHARED_DIR=runtime/shared`
- `POLL_SECONDS=2`

Run:

```bash
./scripts/watch_shared_triggers.sh
```

## TOD Freeze Watchdog (MIM Side)

MIM should continuously evaluate TOD shared artifacts to detect freeze conditions and send a lightweight liveness ping before escalating.

### Freeze Detection Inputs

Watch these TOD artifacts for freshness:

- `TOD_MIM_TASK_ACK.latest.json`
- `TOD_MIM_TASK_RESULT.latest.json`
- `TOD_LOOP_JOURNAL.latest.json`
- `TOD_INTEGRATION_STATUS.latest.json`

### Freeze Heuristic

- If the oldest watched artifact exceeds staleness threshold, mark `freeze_suspected`.
- Emit `MIM_TO_TOD_PING.latest.json` and trigger packet update.
- Apply cooldown to avoid ping storms.

### Expected Ping Flow

1. MIM writes `MIM_TO_TOD_PING.latest.json` with reason and stale age.
2. MIM updates `MIM_TO_TOD_TRIGGER.latest.json` (`trigger=liveness_ping`).
3. TOD responds with `TOD_TO_MIM_PING.latest.json` and normal trigger ACK.
4. MIM records alive response and clears incident condition on fresh artifact updates.

### Script Defaults

- `POLL_SECONDS=3`
- `STALE_SECONDS=45`
- `COOLDOWN_SECONDS=30`

Run:

```bash
./scripts/watch_tod_liveness.sh
```
