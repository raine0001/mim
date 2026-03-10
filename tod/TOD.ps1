Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot/client/mim_api_client.ps1"

param(
    [Parameter(Mandatory = $true)]
    [string]$Action,

    [string]$ConfigPath,
    [string]$Title,
    [string]$Description = "",
    [string]$Priority = "normal",
    [string[]]$Constraints = @(),
    [string]$SuccessCriteria = "",
    [string]$Status = "new",
    [int]$ObjectiveId,
    [string]$Scope = "",
    [int[]]$Dependencies = @(),
    [string]$AcceptanceCriteria = "",
    [string]$AssignedTo = "tod",
    [int]$TaskId,
    [string]$Summary = "",
    [string[]]$FilesChanged = @(),
    [string[]]$TestsRun = @(),
    [string]$TestResults = "",
    [string[]]$Failures = @(),
    [string]$Recommendations = "",
    [string]$Decision = "",
    [string]$Rationale = "",
    [bool]$ContinueAllowed = $false,
    [bool]$EscalateToUser = $false,
    [string]$ExpectedContractVersion = "tod-mim-shared-contract-v1",
    [string]$ExpectedSchemaVersion = "2026-03-09-01",
    [string[]]$ExpectedCapabilities = @("health", "status", "manifest", "objectives", "tasks", "results", "reviews", "journal"),
    [string]$PackagePath = "",
    [string]$TaskMetadataJson = "",
    [int]$Top = 20
)

switch ($Action.ToLowerInvariant()) {
    "ping-mim" {
        Ping-Mim -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "new-objective" {
        New-MimObjective -Title $Title -Description $Description -Priority $Priority -Constraints $Constraints -SuccessCriteria $SuccessCriteria -Status $Status -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "list-objectives" {
        Get-MimObjectives -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "add-task" {
        New-MimTask -ObjectiveId $ObjectiveId -Title $Title -Scope $Scope -Dependencies $Dependencies -AcceptanceCriteria $AcceptanceCriteria -Status $Status -AssignedTo $AssignedTo -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "list-tasks" {
        Get-MimTasks -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "add-result" {
        New-MimResult -TaskId $TaskId -Summary $Summary -FilesChanged $FilesChanged -TestsRun $TestsRun -TestResults $TestResults -Failures $Failures -Recommendations $Recommendations -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "review-task" {
        New-MimReview -TaskId $TaskId -Decision $Decision -Rationale $Rationale -ContinueAllowed $ContinueAllowed -EscalateToUser $EscalateToUser -ConfigPath $ConfigPath | ConvertTo-Json -Depth 10
        break
    }
    "show-journal" {
        @(Get-MimJournal -ConfigPath $ConfigPath | Select-Object -First $Top) | ConvertTo-Json -Depth 10
        break
    }
    "sync-mim" {
        Sync-Mim -ConfigPath $ConfigPath -ExpectedContractVersion $ExpectedContractVersion -ExpectedSchemaVersion $ExpectedSchemaVersion -ExpectedCapabilities $ExpectedCapabilities | ConvertTo-Json -Depth 12
        break
    }
    "invoke-engine" {
        $taskMetadata = [pscustomobject]@{
            task_id = $TaskId
            objective_id = $ObjectiveId
            title = $Title
        }

        if ($TaskMetadataJson) {
            $taskMetadata = $TaskMetadataJson | ConvertFrom-Json
        }

        Invoke-ExecutionEngine -PackagePath $PackagePath -TaskMetadata $taskMetadata -ConfigPath $ConfigPath | ConvertTo-Json -Depth 12
        break
    }
    default {
        throw "Unsupported action '$Action'. Supported: ping-mim, new-objective, list-objectives, add-task, list-tasks, add-result, review-task, show-journal, sync-mim, invoke-engine"
    }
}
