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
