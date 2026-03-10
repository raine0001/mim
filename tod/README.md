# TOD ↔ MIM Operational Guide

## Overview

TOD plans work and interacts with MIM through its API. MIM persists objectives, tasks, results, reviews, and journal entries as the durable workflow record.

## Configuration

Use [config/tod.config.json](config/tod.config.json):

```json
{
  "mode": "hybrid",
  "mim_base_url": "http://192.168.1.120:8000",
  "timeout_seconds": 15,
  "fallback_to_local": true,
  "default_actor": "tod"
}
```

### Modes

| Mode | Behavior |
|---|---|
| local | TOD uses only local state |
| remote | TOD uses only MIM API |
| hybrid | TOD writes to MIM and maintains local fallback/cache |

Hybrid is the default mode.

## Source of Truth Rule

MIM is the durable record of workflow state. TOD may cache locally but must treat MIM as the authoritative system of record once connected.

## Command Examples

Run from [tod](.) with PowerShell.

### Test connection

```powershell
pwsh -File TOD.ps1 -Action ping-mim
```

Expected output shape:

```json
{
  "message": "MIM reachable",
  "status": "running",
  "latency_ms": 12.4,
  "available": true
}
```

### Validate manifest compatibility

```powershell
pwsh -File TOD.ps1 -Action sync-mim `
  -ExpectedContractVersion "tod-mim-shared-contract-v1" `
  -ExpectedSchemaVersion "2026-03-09-01"
```

Decision semantics:

- `ok`: contract/schema/capabilities aligned
- `warn`: schema/capabilities drift (re-index or cache refresh recommended)
- `escalate`: contract version incompatible

### Invoke execution engine (Task 30)

```powershell
pwsh -File TOD.ps1 -Action invoke-engine `
  -ConfigPath ./config/tod.config.json `
  -PackagePath ./packages/task-24.md `
  -TaskId 24 `
  -ObjectiveId 12 `
  -Title "Execute packaged task"
```

Behavior:

- selects `execution.active_engine`
- falls back to `execution.fallback_engine` on failure
- returns normalized `ExecutionResultEnvelope`

Envelope fields:

- `execution_engine`, `success`, `summary`, `raw_output`
- `files_changed`, `tests_run`, `test_results`, `failures`, `recommendations`
- `needs_escalation`, `execution_metadata`, `completed_at`
- `review_precheck.ready_for_review`, `review_precheck.blocking_issues`

Task 32 behavior:

- malformed/partial engine outputs are normalized safely
- required result fields are always present in the envelope
- pre-review checks run automatically before returning the result

### Create objective

```powershell
pwsh -File TOD.ps1 -Action new-objective `
  -Title "Build MIM module" `
  -Description "Implement MIM runtime features" `
  -Priority high `
  -Constraints "Local-first" , "modular design" `
  -SuccessCriteria "API running, DB connected"
```

### List objectives

```powershell
pwsh -File TOD.ps1 -Action list-objectives
```

### Add task

Use `objective_id` from `list-objectives`.

```powershell
pwsh -File TOD.ps1 -Action add-task `
  -ObjectiveId 4 `
  -Title "Create API endpoint" `
  -Scope "Add new router" `
  -AcceptanceCriteria "Endpoint returns JSON"
```

### List tasks

```powershell
pwsh -File TOD.ps1 -Action list-tasks
```

### Add result

Use `task_id` from `list-tasks`.

```powershell
pwsh -File TOD.ps1 -Action add-result `
  -TaskId 2 `
  -Summary "Endpoint implemented" `
  -FilesChanged "api/router.py" `
  -TestsRun "router_test" `
  -TestResults pass
```

### Review task

```powershell
pwsh -File TOD.ps1 -Action review-task `
  -TaskId 2 `
  -Decision approved `
  -Rationale "All criteria satisfied" `
  -ContinueAllowed $true
```

### Inspect journal

```powershell
pwsh -File TOD.ps1 -Action show-journal -Top 20
```

## First Real Integration Workflow

1. `ping-mim`
2. `new-objective`
3. `add-task`
4. `add-result`
5. `review-task`
6. `show-journal`

If `show-journal` contains the create objective/task/result/review lifecycle entries, the integration is working.

## Quickstart (One Block)

Run this from [tod](.) on your TOD machine (PowerShell):

```powershell
$cfg = "./config/tod.config.json"

# 1) ping-mim
pwsh -File ./TOD.ps1 -Action ping-mim -ConfigPath $cfg

# 2) new-objective
$obj = pwsh -File ./TOD.ps1 -Action new-objective -ConfigPath $cfg `
  -Title "Connect TOD to MIM API for workflow persistence" `
  -Description "First real lifecycle validation" `
  -Priority high `
  -Constraints "offline-first", "structured-objects-only" `
  -SuccessCriteria "Journal shows objective->task->result->review"
$objJson = $obj | ConvertFrom-Json

# 3) add-task
$task = pwsh -File ./TOD.ps1 -Action add-task -ConfigPath $cfg `
  -ObjectiveId $objJson.objective_id `
  -Title "Add TOD health/status client for MIM" `
  -Scope "Implement ping-mim against /health and /status" `
  -AcceptanceCriteria "Returns online status and latency"
$taskJson = $task | ConvertFrom-Json

# 4) add-result
pwsh -File ./TOD.ps1 -Action add-result -ConfigPath $cfg `
  -TaskId $taskJson.task_id `
  -Summary "Health/status client wired" `
  -FilesChanged "tod/client/mim_api_client.ps1" `
  -TestsRun "ping-mim" `
  -TestResults pass

# 5) review-task
pwsh -File ./TOD.ps1 -Action review-task -ConfigPath $cfg `
  -TaskId $taskJson.task_id `
  -Decision approved `
  -Rationale "Lifecycle write path validated" `
  -ContinueAllowed $true

# 6) show-journal (top 20)
pwsh -File ./TOD.ps1 -Action show-journal -ConfigPath $cfg -Top 20
```

## Quickstart (Single PowerShell Session)

This variant avoids nested `pwsh` calls by dot-sourcing the client once.

```powershell
Set-Location ./tod
. ./client/mim_api_client.ps1

$cfg = "./config/tod.config.json"

# 1) ping-mim
$ping = Ping-Mim -ConfigPath $cfg
$ping | ConvertTo-Json -Depth 10

# 2) new-objective
$objective = New-MimObjective -ConfigPath $cfg `
  -Title "Connect TOD to MIM API for workflow persistence" `
  -Description "Single-session lifecycle validation" `
  -Priority high `
  -Constraints "offline-first", "structured-objects-only" `
  -SuccessCriteria "Journal shows objective->task->result->review"

# 3) add-task
$task = New-MimTask -ConfigPath $cfg `
  -ObjectiveId $objective.objective_id `
  -Title "Add TOD health/status client for MIM" `
  -Scope "Implement ping-mim against /health and /status" `
  -AcceptanceCriteria "Returns online status and latency"

# 4) add-result
$result = New-MimResult -ConfigPath $cfg `
  -TaskId $task.task_id `
  -Summary "Health/status client wired" `
  -FilesChanged "tod/client/mim_api_client.ps1" `
  -TestsRun "ping-mim" `
  -TestResults pass

# 5) review-task
$review = New-MimReview -ConfigPath $cfg `
  -TaskId $task.task_id `
  -Decision approved `
  -Rationale "Lifecycle write path validated" `
  -ContinueAllowed $true

# 6) show-journal (top 20)
$journal = Get-MimJournal -ConfigPath $cfg | Select-Object -First 20
$journal | ConvertTo-Json -Depth 10
```

You can also run the parameterized script directly:

```powershell
pwsh -File ./quickstart.single-session.ps1 `
  -ConfigPath ./config/tod.config.json `
  -Title "Connect TOD to MIM API for workflow persistence" `
  -Priority high `
  -ModeOverride hybrid `
  -Top 20
```

Most useful parameters:

- `-Title`, `-Description`, `-Priority`
- `-TaskTitle`, `-TaskScope`, `-AcceptanceCriteria`
- `-ResultSummary`, `-FilesChanged`, `-TestsRun`, `-TestResults`
- `-Decision`, `-Rationale`
- `-ModeOverride` (`local`, `remote`, `hybrid`)
- `-Top` (journal entry count)
