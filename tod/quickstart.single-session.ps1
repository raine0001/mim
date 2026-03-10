Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

param(
    [string]$ConfigPath = "./config/tod.config.json",
    [string]$Title = "Connect TOD to MIM API for workflow persistence",
    [string]$Description = "Single-session lifecycle validation",
    [ValidateSet("low", "normal", "high", "critical")]
    [string]$Priority = "high",
    [string[]]$Constraints = @("offline-first", "structured-objects-only"),
    [string]$SuccessCriteria = "Journal shows objective->task->result->review",
    [string]$TaskTitle = "Add TOD health/status client for MIM",
    [string]$TaskScope = "Implement ping-mim against /health and /status",
    [string]$AcceptanceCriteria = "Returns online status and latency",
    [string]$ResultSummary = "Health/status client wired",
    [string[]]$FilesChanged = @("tod/client/mim_api_client.ps1"),
    [string[]]$TestsRun = @("ping-mim"),
    [string]$TestResults = "pass",
    [string]$Decision = "approved",
    [string]$Rationale = "Lifecycle write path validated",
    [ValidateSet("local", "remote", "hybrid")]
    [string]$ModeOverride = "",
    [int]$Top = 20
)

Set-Location $PSScriptRoot
. ./client/mim_api_client.ps1

$cfg = $ConfigPath
  $previousModeOverride = $env:TOD_MODE_OVERRIDE
  if ($ModeOverride) {
    $env:TOD_MODE_OVERRIDE = $ModeOverride
  }

  try {

Write-Host "[1/6] ping-mim"
$ping = Ping-Mim -ConfigPath $cfg
$ping | ConvertTo-Json -Depth 10

Write-Host "[2/6] new-objective"
$objective = New-MimObjective -ConfigPath $cfg `
  -Title $Title `
  -Description $Description `
  -Priority $Priority `
  -Constraints $Constraints `
  -SuccessCriteria $SuccessCriteria
$objective | ConvertTo-Json -Depth 10

Write-Host "[3/6] add-task"
$task = New-MimTask -ConfigPath $cfg `
  -ObjectiveId $objective.objective_id `
  -Title $TaskTitle `
  -Scope $TaskScope `
  -AcceptanceCriteria $AcceptanceCriteria
$task | ConvertTo-Json -Depth 10

Write-Host "[4/6] add-result"
$result = New-MimResult -ConfigPath $cfg `
  -TaskId $task.task_id `
  -Summary $ResultSummary `
  -FilesChanged $FilesChanged `
  -TestsRun $TestsRun `
  -TestResults $TestResults
$result | ConvertTo-Json -Depth 10

Write-Host "[5/6] review-task"
$review = New-MimReview -ConfigPath $cfg `
  -TaskId $task.task_id `
  -Decision $Decision `
  -Rationale $Rationale `
  -ContinueAllowed $true
$review | ConvertTo-Json -Depth 10

Write-Host "[6/6] show-journal (top $Top)"
$journal = Get-MimJournal -ConfigPath $cfg | Select-Object -First $Top
$journal | ConvertTo-Json -Depth 10
}
finally {
  if ($null -ne $previousModeOverride -and $previousModeOverride -ne "") {
    $env:TOD_MODE_OVERRIDE = $previousModeOverride
  }
  else {
    Remove-Item Env:TOD_MODE_OVERRIDE -ErrorAction SilentlyContinue
  }
}
