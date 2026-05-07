param(
    [string]$SharedStateDir = "shared_state",
    [string]$TodScriptPath = "scripts/TOD.ps1",
    [string]$TodConfigPath = "tod/config/tod-config.json",
    [string]$StatePath = "tod/data/state.json",
    [string]$TestSummaryPath = "tod/out/training/test-summary.json",
    [string]$SmokeSummaryPath = "tod/out/training/smoke-summary.json",
    [string]$QualityGatePath = "tod/out/training/quality-gate-summary.json",
    [string]$TrainingStatusPath = "shared_state/tod_training_status.latest.json",
    [string]$ApprovalReductionPath = "shared_state/approval_reduction_summary.json",
    [string]$ManifestPath = "tod/data/sample-manifest.json",
    [string]$MimContextExportPath = "tod/out/context-sync/MIM_CONTEXT_EXPORT.latest.json",
    [string]$MimContextExportYamlPath = "tod/out/context-sync/MIM_CONTEXT_EXPORT.latest.yaml",
    [string]$MimManifestPath = "tod/out/context-sync/MIM_MANIFEST.latest.json",
    [string]$MimSharedContextExportPath = "",
    [string]$MimSharedContextExportYamlPath = "",
    [string]$MimSharedManifestPath = "",
    [string]$MimSharedExportRoot = "",
    [switch]$RefreshMimContextFromShared,
    [switch]$RefreshMimContextFromSsh,
    [string]$MimSshHost = "mim",
    [string]$MimSshUser = "",
    [int]$MimSshPort = 0,
    [string]$MimSshPassword = "",
    [string]$MimSshSharedRoot = "/home/testpilot/mim/runtime/shared",
    [string]$MimSshStagingRoot = "tod/out/context-sync/ssh-shared",
    [switch]$PublishTodStatusToMimArm,
    [string]$MimArmSshHost = "",
    [string]$MimArmSshUser = "",
    [int]$MimArmSshPort = 0,
    [string]$MimArmSshPassword = "",
    [string]$MimArmSshRemoteRoot = "/home/testpilot/mim_arm/runtime/shared",
    [string]$MimArmSshToolsRoot = "/home/testpilot/mim_arm/runtime/tools",
    [string]$MimArmConsumerTemplatePath = "scripts/mim_arm/tod_authority_consumer.py",
    [switch]$AllowInteractiveSshPrompt,
    [string]$DotEnvPath = ".env",
    [string]$ScpCommand = "scp",
    [string]$ContextSyncInboxPath = "tod/inbox/context-sync/updates",
    [string]$ListenerRequestPath = "tod/out/context-sync/listener/MIM_TOD_TASK_REQUEST.latest.json",
    [string]$ListenerDecisionPath = "tod/out/context-sync/listener/TOD_MIM_EXECUTION_DECISION.latest.json",
    [double]$MimStatusStaleAfterHours = 24,
    [string]$ReleaseTagOverride,
    [string]$NextProposedObjective = "TOD-17",
    [switch]$RefreshAgentMimReadiness
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-LocalPath {
    param([Parameter(Mandatory = $true)][string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) { return $PathValue }
    return (Join-Path $repoRoot $PathValue)
}

function New-DirectoryIfMissing {
    param([Parameter(Mandatory = $true)][string]$PathValue)
    if (-not (Test-Path -Path $PathValue)) {
        New-Item -ItemType Directory -Path $PathValue -Force | Out-Null
    }
}

function Get-JsonFileContent {
    param([Parameter(Mandatory = $true)][string]$PathValue)
    $resolved = Get-LocalPath -PathValue $PathValue
    if (-not (Test-Path -Path $resolved)) { throw "File not found: $resolved" }
    return (Get-Content -Path $resolved -Raw | ConvertFrom-Json)
}

function Get-JsonFileIfExists {
    param([Parameter(Mandatory = $true)][string]$PathValue)
    $resolved = Get-LocalPath -PathValue $PathValue
    if (-not (Test-Path -Path $resolved)) { return $null }
    try {
        return (Get-Content -Path $resolved -Raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Normalize-ObjectiveIdText {
    param([string]$Value)

    $text = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return ""
    }

    $match = [regex]::Match($text, '(?i)(?:^objective-(?<objective>\d+)$|^(?<objective>\d+)$)')
    if ($match.Success) {
        return [string]$match.Groups['objective'].Value
    }

    return $text
}

function Get-ObjectiveIdFromTaskReference {
    param([string]$Value)

    $text = ([string]$Value).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return ""
    }

    $match = [regex]::Match($text, '(?i)^objective-(?<objective>\d+)-task-[^\s]+$')
    if ($match.Success) {
        return [string]$match.Groups['objective'].Value
    }

    return ""
}

function Get-TaskProgressWeight {
    param([string]$Status)

    $normalized = ([string]$Status).Trim().ToLowerInvariant()
    switch ($normalized) {
        "pass" { return 1.0 }
        "reviewed_pass" { return 1.0 }
        "done" { return 1.0 }
        "completed" { return 1.0 }
        "implemented" { return 0.75 }
        "in_progress" { return 0.5 }
        "active" { return 0.5 }
        "revise" { return 0.35 }
        "planned" { return 0.15 }
        "open" { return 0.1 }
        default { return 0.0 }
    }
}

function Get-ListenerObjectiveProgressMap {
    param($JournalDoc)

    $entries = @()
    if ($JournalDoc -and $JournalDoc.PSObject.Properties['entries']) {
        $entries = @($JournalDoc.entries)
    }
    elseif ($JournalDoc -is [System.Array]) {
        $entries = @($JournalDoc)
    }

    $statsMap = @{}
    foreach ($entry in $entries) {
        $requestId = if ($entry.PSObject.Properties['request_id']) { [string]$entry.request_id } else { '' }
        $objectiveId = if ($entry.PSObject.Properties['objective_id']) { Normalize-ObjectiveIdText -Value ([string]$entry.objective_id) } else { '' }
        if ([string]::IsNullOrWhiteSpace($objectiveId)) {
            $objectiveId = Get-ObjectiveIdFromTaskReference -Value $requestId
        }
        if ([string]::IsNullOrWhiteSpace($objectiveId)) {
            continue
        }

        $status = if ($entry.PSObject.Properties['execution_status']) { [string]$entry.execution_status } else { '' }
        $statusKey = $status.Trim().ToLowerInvariant()
        if (@('completed', 'failed', 'in_progress') -notcontains $statusKey) {
            continue
        }

        if (-not $statsMap.ContainsKey($objectiveId)) {
            $statsMap[$objectiveId] = [ordered]@{
                total = 0
                completed = 0
                failed = 0
                in_progress = 0
                progress_units = 0.0
                last_request_id = ''
                last_execution_status = ''
                last_timestamp = ''
            }
        }

        $stats = $statsMap[$objectiveId]
        $stats.total = [int]$stats.total + 1
        if ($statusKey -eq 'completed') {
            $stats.completed = [int]$stats.completed + 1
        }
        elseif ($statusKey -eq 'failed') {
            $stats.failed = [int]$stats.failed + 1
        }
        elseif ($statusKey -eq 'in_progress') {
            $stats.in_progress = [int]$stats.in_progress + 1
        }

        $stats.progress_units = [double]$stats.progress_units + (Get-TaskProgressWeight -Status $status)
        $stats.last_request_id = $requestId
        $stats.last_execution_status = $status
        $stats.last_timestamp = if ($entry.PSObject.Properties['timestamp']) { [string]$entry.timestamp } else { '' }
    }

    $result = @{}
    foreach ($objectiveId in $statsMap.Keys) {
        $stats = $statsMap[$objectiveId]
        $taskCount = [int]$stats.total
        $progressUnits = [math]::Round([double]$stats.progress_units, 2)
        $percent = if ($taskCount -gt 0) { [int][math]::Round(($progressUnits / [double]$taskCount) * 100) } else { 0 }
        $result[$objectiveId] = [pscustomobject]@{
            available = [bool]($taskCount -gt 0)
            task_count = $taskCount
            completed_equivalent = $progressUnits
            percent = $percent
            by_status = [pscustomobject]@{
                completed = [int]$stats.completed
                failed = [int]$stats.failed
                in_progress = [int]$stats.in_progress
            }
            source = 'listener_journal'
            generated_at = (Get-Date).ToUniversalTime().ToString('o')
            last_request_id = [string]$stats.last_request_id
            last_execution_status = [string]$stats.last_execution_status
            last_timestamp = [string]$stats.last_timestamp
        }
    }

    return $result
}

function Write-Utf8NoBomText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $dir = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $normalized = ([string]$Content) -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($Path, $normalized, $utf8NoBom)
}

function Write-Utf8NoBomJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload,
        [int]$Depth = 20
    )

    $json = $Payload | ConvertTo-Json -Depth $Depth
    Write-Utf8NoBomText -Path $Path -Content $json
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -Path $Path)) {
        return ""
    }

    try {
        return [string](Get-FileHash -Path $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    }
    catch {
        return ""
    }
}

function Append-Utf8NoBomJsonLine {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload,
        [int]$Depth = 20
    )

    $dir = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $line = (($Payload | ConvertTo-Json -Depth $Depth -Compress) + "`n")
    [System.IO.File]::AppendAllText($Path, $line, $utf8NoBom)
}

function Get-TodPayload {
    param(
        [Parameter(Mandatory = $true)][string]$TodScript,
        [Parameter(Mandatory = $true)][string]$TodConfig,
        [Parameter(Mandatory = $true)][string]$ActionName
    )

    try {
        $raw = & $TodScript -Action $ActionName -ConfigPath $TodConfig -Top 10
        return ($raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-GitValue {
    param([Parameter(Mandatory = $true)][string]$CommandText)

    try {
        $value = Invoke-Expression $CommandText
        if ($null -eq $value) { return "" }
        return ([string]$value).Trim()
    }
    catch {
        return ""
    }
}

function Get-IdNumber {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return -1 }
    $digits = [regex]::Match($Value, "\d+")
    if (-not $digits.Success) { return -1 }
    return [int]$digits.Value
}

function Get-ObjectiveAuthorityReset {
    param([Parameter(Mandatory = $true)][string]$PathValue)

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) {
        return [pscustomobject]@{
            available = $false
            source_path = $PathValue
            active = $false
            authoritative_current_objective = ""
            rollback_from_objective = ""
            rollback_to_objective = ""
            max_valid_objective = ""
            invalidated_objectives = @()
            reason = ""
            effective_at = ""
        }
    }

    $authoritativeCurrentObjectiveRaw = ''
    if ($doc.PSObject.Properties['authoritative_current_objective']) {
        $authoritativeCurrentObjectiveRaw = [string]$doc.authoritative_current_objective
    }
    elseif ($doc.PSObject.Properties['rollback_to_objective']) {
        $authoritativeCurrentObjectiveRaw = [string]$doc.rollback_to_objective
    }
    $authoritativeCurrentObjective = Normalize-ObjectiveIdText -Value $authoritativeCurrentObjectiveRaw
    $rollbackFromObjective = if ($doc.PSObject.Properties['rollback_from_objective']) { Normalize-ObjectiveIdText -Value ([string]$doc.rollback_from_objective) } else { '' }
    $rollbackToObjective = if ($doc.PSObject.Properties['rollback_to_objective']) { Normalize-ObjectiveIdText -Value ([string]$doc.rollback_to_objective) } else { $authoritativeCurrentObjective }
    $maxValidObjective = if ($doc.PSObject.Properties['max_valid_objective']) { Normalize-ObjectiveIdText -Value ([string]$doc.max_valid_objective) } else { $authoritativeCurrentObjective }

    $invalidatedObjectives = @()
    if ($doc.PSObject.Properties['invalidate_objectives_above']) {
        $invalidatedObjectives = @(
            Convert-ToStringList -Value $doc.invalidate_objectives_above | ForEach-Object {
                Normalize-ObjectiveIdText -Value ([string]$_)
            } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
    }

    $active = $true
    if ($doc.PSObject.Properties['active']) {
        $active = [bool]$doc.active
    }

    $rollbackReason = ''
    if ($doc.PSObject.Properties['rollback_reason']) {
        $rollbackReason = [string]$doc.rollback_reason
    }
    elseif ($doc.PSObject.Properties['reason']) {
        $rollbackReason = [string]$doc.reason
    }

    $effectiveAt = ''
    if ($doc.PSObject.Properties['authoritative_effective_at']) {
        $effectiveAt = [string]$doc.authoritative_effective_at
    }

    return [pscustomobject]@{
        available = $true
        source_path = $PathValue
        active = [bool]$active
        authoritative_current_objective = $authoritativeCurrentObjective
        rollback_from_objective = $rollbackFromObjective
        rollback_to_objective = $rollbackToObjective
        max_valid_objective = $maxValidObjective
        invalidated_objectives = @($invalidatedObjectives)
        reason = $rollbackReason
        effective_at = $effectiveAt
    }
}

function Test-ObjectiveInvalidatedByAuthority {
    param(
        [string]$ObjectiveId,
        $AuthorityReset
    )

    if ($null -eq $AuthorityReset -or -not [bool]$AuthorityReset.available -or -not [bool]$AuthorityReset.active) {
        return $false
    }

    $normalizedObjective = Normalize-ObjectiveIdText -Value $ObjectiveId
    if ([string]::IsNullOrWhiteSpace($normalizedObjective)) {
        return $false
    }

    $explicitInvalidations = @()
    if ($AuthorityReset.PSObject.Properties['invalidated_objectives'] -and $null -ne $AuthorityReset.invalidated_objectives) {
        $explicitInvalidations = @($AuthorityReset.invalidated_objectives | ForEach-Object { Normalize-ObjectiveIdText -Value ([string]$_) })
    }

    if ($explicitInvalidations -contains $normalizedObjective) {
        return $true
    }

    $objectiveNumber = Get-IdNumber -Value $normalizedObjective
    $maxValidNumber = Get-IdNumber -Value ([string]$AuthorityReset.max_valid_objective)
    if ($objectiveNumber -ge 0 -and $maxValidNumber -ge 0 -and $objectiveNumber -gt $maxValidNumber) {
        return $true
    }

    return $false
}

function Convert-ToStringList {
    param($Value)

    if ($null -eq $Value) { return @() }

    $items = @()
    if ($Value -is [System.Array]) {
        $items = @($Value)
    }
    elseif ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
        $items = @($Value)
    }
    else {
        $items = @($Value)
    }

    $normalized = @()
    foreach ($item in $items) {
        $text = [string]$item
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            $normalized += $text
        }
    }

    return @($normalized)
}

function Convert-ToUtcDateOrNull {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        return ([datetime]$Value).ToUniversalTime()
    }
    catch {
        return $null
    }
}

function Resolve-ReliabilityAlertState {
    param(
        [string]$RawState,
        [string]$Trend,
        [int]$PendingApprovals,
        [bool]$RegressionPassed,
        [bool]$QualityGatePassed
    )

    $normalized = if ([string]::IsNullOrWhiteSpace($RawState)) { "" } else { $RawState.Trim().ToLowerInvariant() }
    if ($normalized -in @("stable", "warning", "degraded", "critical")) {
        return $normalized
    }

    $trendNorm = if ([string]::IsNullOrWhiteSpace($Trend)) { "unknown" } else { $Trend.Trim().ToLowerInvariant() }
    if (-not $RegressionPassed -and -not $QualityGatePassed) {
        return "critical"
    }
    if (-not $RegressionPassed -or $PendingApprovals -ge 100) {
        return "degraded"
    }
    if ($trendNorm -in @("declining", "watch", "warning") -or $PendingApprovals -gt 0) {
        return "warning"
    }

    return "stable"
}

function Get-ApprovalBacklogSnapshot {
    param(
        [Parameter(Mandatory = $true)]$State,
        [int]$StaleHours = 72
    )

    $records = @()
    if ($State.PSObject.Properties["engineering_loop"] -and $State.engineering_loop -and $State.engineering_loop.PSObject.Properties["cycle_records"]) {
        $records = @($State.engineering_loop.cycle_records)
    }

    $pending = @($records | Where-Object {
            ($_.PSObject.Properties["approval_pending"] -and [bool]$_.approval_pending) -or
            ($_.PSObject.Properties["approval_status"] -and ([string]$_.approval_status).ToLowerInvariant() -eq "pending_apply")
        })

    $now = (Get-Date).ToUniversalTime()
    $ageBuckets = [ordered]@{
        "lt_24h" = 0
        "h24_to_h72" = 0
        "gt_72h" = 0
        "unknown" = 0
    }

    $statusCounts = [ordered]@{}
    $sourceCounts = [ordered]@{}
    $promotable = @()
    $stale = @()
    $lowValue = @()

    foreach ($item in $pending) {
        $statusValue = if ($item.PSObject.Properties["approval_status"]) { [string]$item.approval_status } else { "pending_apply" }
        if ([string]::IsNullOrWhiteSpace($statusValue)) { $statusValue = "pending_apply" }
        $statusKey = $statusValue.Trim().ToLowerInvariant()
        if (-not $statusCounts.Contains($statusKey)) {
            $statusCounts[$statusKey] = 0
        }
        $statusCounts[$statusKey] = [int]$statusCounts[$statusKey] + 1

        $sourceKey = "engineering_loop"
        if ($item.PSObject.Properties["task_category"] -and -not [string]::IsNullOrWhiteSpace([string]$item.task_category)) {
            $sourceKey = "task_category:{0}" -f ([string]$item.task_category)
        }
        elseif ($item.PSObject.Properties["objective_id"] -and -not [string]::IsNullOrWhiteSpace([string]$item.objective_id)) {
            $sourceKey = "objective:{0}" -f ([string]$item.objective_id)
        }
        if (-not $sourceCounts.Contains($sourceKey)) {
            $sourceCounts[$sourceKey] = 0
        }
        $sourceCounts[$sourceKey] = [int]$sourceCounts[$sourceKey] + 1

        $createdAtRaw = if ($item.PSObject.Properties["created_at"]) { [string]$item.created_at } else { "" }
        $updatedAtRaw = if ($item.PSObject.Properties["updated_at"]) { [string]$item.updated_at } else { "" }
        $createdAtUtc = Convert-ToUtcDateOrNull -Value $createdAtRaw
        $updatedAtUtc = Convert-ToUtcDateOrNull -Value $updatedAtRaw
        $anchor = if ($null -ne $createdAtUtc) { $createdAtUtc } else { $updatedAtUtc }

        $ageHours = $null
        if ($null -eq $anchor) {
            $ageBuckets["unknown"] = [int]$ageBuckets["unknown"] + 1
        }
        else {
            $ageHours = [math]::Round(($now - $anchor).TotalHours, 2)
            if ($ageHours -lt 24) {
                $ageBuckets["lt_24h"] = [int]$ageBuckets["lt_24h"] + 1
            }
            elseif ($ageHours -le 72) {
                $ageBuckets["h24_to_h72"] = [int]$ageBuckets["h24_to_h72"] + 1
            }
            else {
                $ageBuckets["gt_72h"] = [int]$ageBuckets["gt_72h"] + 1
            }
        }

        $score = $null
        if ($item.PSObject.Properties["score_snapshot"] -and $item.score_snapshot -and $item.score_snapshot.PSObject.Properties["overall"] -and $item.score_snapshot.overall.PSObject.Properties["score"]) {
            $score = [double]$item.score_snapshot.overall.score
        }

        $maturityBand = if ($item.PSObject.Properties["maturity_band"]) { ([string]$item.maturity_band).ToLowerInvariant() } else { "" }
        $recordId = if ($item.PSObject.Properties["cycle_id"]) { [string]$item.cycle_id } elseif ($item.PSObject.Properties["run_id"]) { [string]$item.run_id } else { "unknown" }

        $summaryRow = [pscustomobject]@{
            id = $recordId
            objective_id = if ($item.PSObject.Properties["objective_id"]) { [string]$item.objective_id } else { "" }
            task_id = if ($item.PSObject.Properties["task_id"]) { [string]$item.task_id } else { "" }
            status = $statusKey
            source = $sourceKey
            age_hours = $ageHours
            maturity_band = $maturityBand
            score = if ($null -ne $score) { [math]::Round($score, 4) } else { $null }
        }

        if ($null -ne $ageHours -and $ageHours -ge $StaleHours) {
            $stale += $summaryRow
        }

        if ($maturityBand -in @("good", "strong") -and $null -ne $score -and $score -ge 0.65) {
            $promotable += $summaryRow
        }

        if ($maturityBand -in @("emerging", "early") -or ($null -ne $score -and $score -lt 0.45)) {
            $lowValue += $summaryRow
        }
    }

    return [pscustomobject]@{
        generated_at = $now.ToString("o")
        total_pending = @($pending).Count
        by_type = [pscustomobject]$statusCounts
        by_age = [pscustomobject]$ageBuckets
        by_source = [pscustomobject]$sourceCounts
        stale_count = @($stale).Count
        low_value_count = @($lowValue).Count
        promotable_count = @($promotable).Count
        stale = @($stale | Select-Object -First 10)
        low_value = @($lowValue | Select-Object -First 10)
        promotable = @($promotable | Select-Object -First 10)
    }
}

function Get-ObjectiveByStatusOrder {
    param(
        [Parameter(Mandatory = $true)]$Objectives,
        [Parameter(Mandatory = $true)][string[]]$Statuses
    )

    $statusSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($statusItem in @($Statuses)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$statusItem)) {
            [void]$statusSet.Add(([string]$statusItem).ToLowerInvariant())
        }
    }

    $objectiveHits = @()
    foreach ($objectiveItem in @($Objectives)) {
        $statusText = ""
        if ($objectiveItem.PSObject.Properties["status"]) {
            $statusText = ([string]$objectiveItem.status).ToLowerInvariant()
        }
        if ($statusSet.Contains($statusText)) {
            $objectiveHits += $objectiveItem
        }
    }

    if (@($objectiveHits).Count -eq 0) { return $null }

    $ordered = @($objectiveHits | Sort-Object @{ Expression = { Get-IdNumber -Value ([string]$_.id) }; Descending = $true })
    return $ordered[0]
}

function Get-MimSchemaVersionFromContextExport {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue
    )

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) { return "" }

    if ($doc.PSObject.Properties["schema_version"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.schema_version)) {
        return [string]$doc.schema_version
    }

    if ($doc.PSObject.Properties["status"] -and $doc.status -and $doc.status.PSObject.Properties["schema_version"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.status.schema_version)) {
        return [string]$doc.status.schema_version
    }

    if ($doc.PSObject.Properties["contract_version"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.contract_version)) {
        return [string]$doc.contract_version
    }

    return ""
}

function Ensure-ParentDirectoryForFile {
    param([Parameter(Mandatory = $true)][string]$FilePath)
    $dir = Split-Path -Parent $FilePath
    if (-not [string]::IsNullOrWhiteSpace($dir) -and -not (Test-Path -Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

function Get-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($Name)) {
        return ""
    }
    if (-not (Test-Path -Path $Path)) {
        return ""
    }

    $line = Get-Content -Path $Path | Where-Object {
        $_ -match ("^\s*{0}\s*=" -f [regex]::Escape($Name))
    } | Select-Object -First 1

    if ([string]::IsNullOrWhiteSpace([string]$line)) {
        return ""
    }

    return ([string]($line -replace ("^\s*{0}\s*=\s*" -f [regex]::Escape($Name)), "")).Trim()
}

function Resolve-MimSshSettingValue {
    param(
        [string]$ExplicitValue,
        [string]$EnvVarName,
        [string]$DotEnvPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitValue)) {
        return [string]$ExplicitValue
    }

    if (-not [string]::IsNullOrWhiteSpace($EnvVarName)) {
        $fromEnv = [string][Environment]::GetEnvironmentVariable($EnvVarName)
        if (-not [string]::IsNullOrWhiteSpace($fromEnv)) {
            return $fromEnv
        }

        $fromDotEnv = Get-DotEnvValue -Path $DotEnvPath -Name $EnvVarName
        if (-not [string]::IsNullOrWhiteSpace($fromDotEnv)) {
            return $fromDotEnv
        }
    }

    return ""
}

function Resolve-SshHostAlias {
    param([string]$RemoteHost)

    if ([string]::IsNullOrWhiteSpace($RemoteHost)) {
        return ""
    }

    # If this is already an IP or contains a dot, treat it as concrete hostname.
    if ($RemoteHost -match "^\d{1,3}(?:\.\d{1,3}){3}$" -or $RemoteHost -match "\.") {
        return $RemoteHost
    }

    $sshConfigPath = Join-Path $HOME ".ssh/config"
    if (-not (Test-Path -Path $sshConfigPath)) {
        return $RemoteHost
    }

    $inHostBlock = $false
    $matchedHost = $false
    $resolvedHostName = ""

    foreach ($rawLine in (Get-Content -Path $sshConfigPath)) {
        $line = [string]$rawLine
        $trim = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trim) -or $trim.StartsWith("#")) {
            continue
        }

        if ($trim -match "^(?i)Host\s+(.+)$") {
            $inHostBlock = $true
            $matchedHost = $false
            $resolvedHostName = ""

            $hostTokens = @($matches[1] -split "\s+")
            foreach ($token in $hostTokens) {
                if ([string]::Equals([string]$token, $RemoteHost, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $matchedHost = $true
                    break
                }
            }
            continue
        }

        if ($inHostBlock -and $matchedHost -and $trim -match "^(?i)HostName\s+(.+)$") {
            $resolvedHostName = [string]$matches[1]
            break
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($resolvedHostName)) {
        return $resolvedHostName
    }

    return $RemoteHost
}

function New-MimSshConnections {
    param(
        [Parameter(Mandatory = $true)][string]$HostAlias,
        [Parameter(Mandatory = $true)][string]$UserName,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Password
    )

    if (-not (Get-Module -ListAvailable -Name Posh-SSH)) {
        throw "Posh-SSH is not installed. Install-Module -Name Posh-SSH -Scope CurrentUser"
    }

    Import-Module Posh-SSH -ErrorAction Stop | Out-Null

    $resolvedHost = Resolve-SshHostAlias -RemoteHost $HostAlias
    $securePassword = ConvertTo-SecureString $Password -AsPlainText -Force
    $credential = New-Object System.Management.Automation.PSCredential ($UserName, $securePassword)

    $sshSession = New-SSHSession -ComputerName $resolvedHost -Port $Port -Credential $credential -AcceptKey -ConnectionTimeout 15000
    $sftpSession = New-SFTPSession -ComputerName $resolvedHost -Port $Port -Credential $credential -AcceptKey -ConnectionTimeout 15000

    return [pscustomobject]@{
        host_alias = $HostAlias
        resolved_host = $resolvedHost
        ssh = $sshSession
        sftp = $sftpSession
    }
}

function Close-MimSshConnections {
    param($Connections)

    if ($null -eq $Connections) { return }

    try {
        if ($Connections.sftp) {
            Remove-SFTPSession -SessionId ([int]$Connections.sftp.SessionId) | Out-Null
        }
    }
    catch {
    }

    try {
        if ($Connections.ssh) {
            Remove-SSHSession -SessionId ([int]$Connections.ssh.SessionId) | Out-Null
        }
    }
    catch {
    }
}

function Publish-TodStatusToMimArm {
    param(
        [Parameter(Mandatory = $true)][string]$LocalStatusPath,
        [string]$LocalTrainingStatusPath = "",
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [string]$LegacyReceiptPath = "",
        [string]$RemoteHost,
        [string]$RemoteUser,
        [int]$RemotePort,
        [string]$RemotePassword,
        [string]$RemoteRoot,
        [string]$RemoteToolsRoot,
        [string]$ConsumerTemplatePath,
        [string]$DotEnvPath
    )

    $dotEnvAbs = ""
    if (-not [string]::IsNullOrWhiteSpace($DotEnvPath)) {
        $dotEnvAbs = Get-LocalPath -PathValue $DotEnvPath
    }

    $status = [ordered]@{
        attempted = $true
        enabled = $true
        status = "pending"
        local_status_path = $LocalStatusPath
        local_training_status_path = $LocalTrainingStatusPath
        local_status_sha256 = Get-FileSha256 -Path $LocalStatusPath
        local_training_status_sha256 = ""
        receipt_path = $ReceiptPath
        legacy_receipt_path = $LegacyReceiptPath
        ssh_host = ""
        ssh_resolved_host = ""
        ssh_user = ""
        ssh_port = 22
        remote_root = ""
        remote_primary_path = ""
        remote_alias_path = ""
        remote_training_primary_path = ""
        remote_training_alias_path = ""
        remote_summary_path = ""
        mim_mirror_root = ""
        mim_mirror_ssh_host = ""
        mim_mirror_ssh_resolved_host = ""
        mim_mirror_ssh_user = ""
        mim_mirror_ssh_port = 22
        mim_mirror_primary_path = ""
        mim_mirror_alias_path = ""
        mim_mirror_training_primary_path = ""
        mim_mirror_training_alias_path = ""
        mim_mirror_status = "not_attempted"
        access_mode = "full"
        remote_access_status = "pending"
        remote_consumer_script_path = ""
        consumer_status = "not_run"
        uploaded_at = ""
        error = ""
    }

    $resolvedHost = Resolve-MimSshSettingValue -ExplicitValue $RemoteHost -EnvVarName "MIM_ARM_SSH_HOST" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedHost)) {
        $resolvedHost = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_HOST" -DotEnvPath $dotEnvAbs
    }

    $resolvedUser = Resolve-MimSshSettingValue -ExplicitValue $RemoteUser -EnvVarName "MIM_ARM_SSH_USER" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedUser)) {
        $resolvedUser = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_USER" -DotEnvPath $dotEnvAbs
    }
    if ([string]::IsNullOrWhiteSpace($resolvedUser)) {
        $resolvedUser = "testpilot"
    }

    $portText = ""
    if ($RemotePort -gt 0) {
        $portText = [string]$RemotePort
    }
    $resolvedPortText = Resolve-MimSshSettingValue -ExplicitValue $portText -EnvVarName "MIM_ARM_SSH_PORT" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedPortText)) {
        $resolvedPortText = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_PORT" -DotEnvPath $dotEnvAbs
    }

    $resolvedPort = 22
    if (-not [string]::IsNullOrWhiteSpace($resolvedPortText)) {
        $parsedPort = 0
        if ([int]::TryParse($resolvedPortText, [ref]$parsedPort) -and $parsedPort -gt 0) {
            $resolvedPort = $parsedPort
        }
    }

    $resolvedPassword = Resolve-MimSshSettingValue -ExplicitValue $RemotePassword -EnvVarName "MIM_ARM_SSH_HOST_PASS" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedPassword)) {
        $resolvedPassword = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_PASSWORD" -DotEnvPath $dotEnvAbs
    }

    $resolvedRoot = Resolve-MimSshSettingValue -ExplicitValue $RemoteRoot -EnvVarName "MIM_ARM_SSH_REMOTE_ROOT" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedRoot)) {
        $resolvedRoot = "/home/testpilot/mim_arm/runtime/shared"
    }

    $resolvedToolsRoot = Resolve-MimSshSettingValue -ExplicitValue $RemoteToolsRoot -EnvVarName "MIM_ARM_SSH_TOOLS_ROOT" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($resolvedToolsRoot)) {
        $resolvedToolsRoot = "/home/testpilot/mim_arm/runtime/tools"
    }

    $consumerTemplateAbs = ""
    if (-not [string]::IsNullOrWhiteSpace($ConsumerTemplatePath)) {
        $consumerTemplateAbs = Get-LocalPath -PathValue $ConsumerTemplatePath
    }

    $status.ssh_host = $resolvedHost
    $status.ssh_user = $resolvedUser
    $status.ssh_port = $resolvedPort
    $status.remote_root = $resolvedRoot
    $status.remote_primary_path = ("{0}/TOD_INTEGRATION_STATUS.latest.json" -f $resolvedRoot.TrimEnd('/'))
    $status.remote_alias_path = ("{0}/TOD_integration_status.latest.json" -f $resolvedRoot.TrimEnd('/'))
    $status.remote_training_primary_path = ("{0}/TOD_TRAINING_STATUS.latest.json" -f $resolvedRoot.TrimEnd('/'))
    $status.remote_training_alias_path = ("{0}/TOD_training_status.latest.json" -f $resolvedRoot.TrimEnd('/'))
    $status.remote_summary_path = ("{0}/TOD_AUTHORITY_SUMMARY.latest.json" -f $resolvedRoot.TrimEnd('/'))
    $status.remote_consumer_script_path = ("{0}/tod_authority_consumer.py" -f $resolvedToolsRoot.TrimEnd('/'))
    $status.mim_mirror_root = "/home/testpilot/mim/runtime/shared"

    $mirrorHost = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_HOST" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($mirrorHost)) {
        $mirrorHost = $resolvedHost
    }
    $mirrorUser = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_USER" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($mirrorUser)) {
        $mirrorUser = "testpilot"
    }
    $mirrorPortText = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_PORT" -DotEnvPath $dotEnvAbs
    $mirrorPort = 22
    if (-not [string]::IsNullOrWhiteSpace($mirrorPortText)) {
        $parsedMirrorPort = 0
        if ([int]::TryParse($mirrorPortText, [ref]$parsedMirrorPort) -and $parsedMirrorPort -gt 0) {
            $mirrorPort = $parsedMirrorPort
        }
    }
    $mirrorPassword = Resolve-MimSshSettingValue -ExplicitValue "" -EnvVarName "MIM_SSH_PASSWORD" -DotEnvPath $dotEnvAbs

    $status.mim_mirror_ssh_host = $mirrorHost
    $status.mim_mirror_ssh_user = $mirrorUser
    $status.mim_mirror_ssh_port = $mirrorPort
    $status.mim_mirror_primary_path = ("{0}/TOD_INTEGRATION_STATUS.latest.json" -f $status.mim_mirror_root.TrimEnd('/'))
    $status.mim_mirror_alias_path = ("{0}/TOD_integration_status.latest.json" -f $status.mim_mirror_root.TrimEnd('/'))
    $status.mim_mirror_training_primary_path = ("{0}/TOD_TRAINING_STATUS.latest.json" -f $status.mim_mirror_root.TrimEnd('/'))
    $status.mim_mirror_training_alias_path = ("{0}/TOD_training_status.latest.json" -f $status.mim_mirror_root.TrimEnd('/'))

    function Write-TodStatusReceipts {
        param([Parameter(Mandatory = $true)]$Payload)

        Write-Utf8NoBomJson -Path $ReceiptPath -Payload $Payload -Depth 8
        if (-not [string]::IsNullOrWhiteSpace($LegacyReceiptPath)) {
            Write-Utf8NoBomJson -Path $LegacyReceiptPath -Payload $Payload -Depth 8
        }
    }

    if (-not (Test-Path -Path $LocalStatusPath)) {
        $status.status = "local_status_missing"
        $status.error = "local_status_missing"
        $receipt = [pscustomobject]$status
        Write-TodStatusReceipts -Payload $receipt
        return $receipt
    }

    if ([string]::IsNullOrWhiteSpace($resolvedHost)) {
        $status.status = "missing_ssh_host"
        $status.error = "missing_ssh_host"
        $receipt = [pscustomobject]$status
        Write-TodStatusReceipts -Payload $receipt
        return $receipt
    }

    if ([string]::IsNullOrWhiteSpace($resolvedPassword) -or $resolvedPassword -eq "CHANGE_ME") {
        $status.status = "missing_ssh_password"
        $status.error = "missing_ssh_password"
        $receipt = [pscustomobject]$status
        Write-TodStatusReceipts -Payload $receipt
        return $receipt
    }

    $connections = $null
    $mirrorConnections = $null
    $tempUploadPaths = New-Object System.Collections.Generic.List[string]
    $consumerTemplateUploaded = $false
    try {
        $connections = New-MimSshConnections -HostAlias $resolvedHost -UserName $resolvedUser -Port $resolvedPort -Password $resolvedPassword
        $status.ssh_resolved_host = [string]$connections.resolved_host

        $mkdirResult = Invoke-SSHCommand -SessionId ([int]$connections.ssh.SessionId) -Command ("mkdir -p '{0}' '{1}'" -f $resolvedRoot, $resolvedToolsRoot) -TimeOut 30
        if ($mkdirResult.ExitStatus -ne 0) {
            throw "remote_root_create_failed"
        }

        $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) "tod-mim-arm-sync"
        if (-not (Test-Path -Path $tempDir)) {
            New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        }

        $status.status = "uploaded"
        $status.uploaded_at = (Get-Date).ToUniversalTime().ToString("o")
        $status.error = ""

        $payloadDoc = Get-Content -Path $LocalStatusPath -Raw | ConvertFrom-Json
        $trainingPayload = ""
        if (-not [string]::IsNullOrWhiteSpace($LocalTrainingStatusPath) -and (Test-Path -Path $LocalTrainingStatusPath)) {
            $trainingPayload = Get-Content -Path $LocalTrainingStatusPath -Raw
            $status.local_training_status_sha256 = Get-FileSha256 -Path $LocalTrainingStatusPath
        }
        $payloadDoc.tod_status_publish = [pscustomobject]$status
        $payload = $payloadDoc | ConvertTo-Json -Depth 8
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

        function New-TodStatusUploadTempFile {
            param(
                [Parameter(Mandatory = $true)][string]$LeafName,
                [Parameter(Mandatory = $true)][string]$Content
            )

            $tempLeafDir = Join-Path $tempDir ([guid]::NewGuid().ToString("N"))
            if (-not (Test-Path -Path $tempLeafDir)) {
                New-Item -ItemType Directory -Path $tempLeafDir -Force | Out-Null
            }
            [void]$tempUploadPaths.Add($tempLeafDir)
            $tempFilePath = Join-Path $tempLeafDir $LeafName
            [System.IO.File]::WriteAllText($tempFilePath, $Content, $utf8NoBom)
            [void]$tempUploadPaths.Add($tempFilePath)
            return $tempFilePath
        }

        $primaryUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_INTEGRATION_STATUS.latest.json" -Content $payload
        $aliasUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_integration_status.latest.json" -Content $payload

        Set-SFTPItem -SessionId ([int]$connections.sftp.SessionId) -Path $primaryUploadPath -Destination $resolvedRoot -Force -ErrorAction Stop | Out-Null
        Set-SFTPItem -SessionId ([int]$connections.sftp.SessionId) -Path $aliasUploadPath -Destination $resolvedRoot -Force -ErrorAction Stop | Out-Null
        if (-not [string]::IsNullOrWhiteSpace($trainingPayload)) {
            $trainingPrimaryUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_TRAINING_STATUS.latest.json" -Content $trainingPayload
            $trainingAliasUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_training_status.latest.json" -Content $trainingPayload
            Set-SFTPItem -SessionId ([int]$connections.sftp.SessionId) -Path $trainingPrimaryUploadPath -Destination $resolvedRoot -Force -ErrorAction Stop | Out-Null
            Set-SFTPItem -SessionId ([int]$connections.sftp.SessionId) -Path $trainingAliasUploadPath -Destination $resolvedRoot -Force -ErrorAction Stop | Out-Null
        }

        # Keep a MIM-facing copy in sync on the same SSH host; create mirror root when absent.
        $reusePrimaryConnection = [string]::Equals($mirrorHost, $resolvedHost, [System.StringComparison]::OrdinalIgnoreCase) -and [string]::Equals($mirrorUser, $resolvedUser, [System.StringComparison]::OrdinalIgnoreCase) -and ($mirrorPort -eq $resolvedPort) -and [string]::Equals($mirrorPassword, $resolvedPassword, [System.StringComparison]::Ordinal)
        if ($reusePrimaryConnection) {
            $mirrorConnections = $connections
        }
        else {
            if ([string]::IsNullOrWhiteSpace($mirrorPassword) -or $mirrorPassword -eq "CHANGE_ME") {
                throw "missing_mim_mirror_ssh_password"
            }
            $mirrorConnections = New-MimSshConnections -HostAlias $mirrorHost -UserName $mirrorUser -Port $mirrorPort -Password $mirrorPassword
        }
        $status.mim_mirror_ssh_resolved_host = [string]$mirrorConnections.resolved_host

        $mirrorMkdir = Invoke-SSHCommand -SessionId ([int]$mirrorConnections.ssh.SessionId) -Command ("mkdir -p '{0}'" -f $status.mim_mirror_root) -TimeOut 15
        if ($mirrorMkdir.ExitStatus -eq 0) {
            $mirrorPrimaryUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_INTEGRATION_STATUS.latest.json" -Content $payload
            $mirrorAliasUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_integration_status.latest.json" -Content $payload
            Set-SFTPItem -SessionId ([int]$mirrorConnections.sftp.SessionId) -Path $mirrorPrimaryUploadPath -Destination $status.mim_mirror_root -Force -ErrorAction Stop | Out-Null
            Set-SFTPItem -SessionId ([int]$mirrorConnections.sftp.SessionId) -Path $mirrorAliasUploadPath -Destination $status.mim_mirror_root -Force -ErrorAction Stop | Out-Null
            if (-not [string]::IsNullOrWhiteSpace($trainingPayload)) {
                $mirrorTrainingPrimaryUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_TRAINING_STATUS.latest.json" -Content $trainingPayload
                $mirrorTrainingAliasUploadPath = New-TodStatusUploadTempFile -LeafName "TOD_training_status.latest.json" -Content $trainingPayload
                Set-SFTPItem -SessionId ([int]$mirrorConnections.sftp.SessionId) -Path $mirrorTrainingPrimaryUploadPath -Destination $status.mim_mirror_root -Force -ErrorAction Stop | Out-Null
                Set-SFTPItem -SessionId ([int]$mirrorConnections.sftp.SessionId) -Path $mirrorTrainingAliasUploadPath -Destination $status.mim_mirror_root -Force -ErrorAction Stop | Out-Null
            }
            $status.mim_mirror_status = "mirrored"
        }
        else {
            $status.mim_mirror_status = "mirror_create_failed"
        }

        $remotePrimaryQuoted = [string]$status.remote_primary_path
        $remoteAliasQuoted = [string]$status.remote_alias_path
        $remoteTrainingPrimaryQuoted = [string]$status.remote_training_primary_path
        $remoteTrainingAliasQuoted = [string]$status.remote_training_alias_path
        $remoteSummaryQuoted = [string]$status.remote_summary_path
        $remoteConsumerQuoted = [string]$status.remote_consumer_script_path
        $mirrorPrimaryQuoted = [string]$status.mim_mirror_primary_path
        $mirrorAliasQuoted = [string]$status.mim_mirror_alias_path
        $mirrorTrainingPrimaryQuoted = [string]$status.mim_mirror_training_primary_path
        $mirrorTrainingAliasQuoted = [string]$status.mim_mirror_training_alias_path

        $accessCommand = @(
            "chmod 777 '{0}' '{1}'" -f $resolvedRoot, $resolvedToolsRoot,
            "chmod 666 '{0}' '{1}'" -f $remotePrimaryQuoted, $remoteAliasQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $remoteTrainingPrimaryQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $remoteTrainingAliasQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $remoteSummaryQuoted,
            "if [ -f '{0}' ]; then chmod 755 '{0}'; fi" -f $remoteConsumerQuoted,
            "if [ -d '{0}' ]; then chmod 777 '{0}'; fi" -f $status.mim_mirror_root,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $mirrorPrimaryQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $mirrorAliasQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $mirrorTrainingPrimaryQuoted,
            "if [ -f '{0}' ]; then chmod 666 '{0}'; fi" -f $mirrorTrainingAliasQuoted
        ) -join " ; "
        $accessResult = Invoke-SSHCommand -SessionId ([int]$connections.ssh.SessionId) -Command $accessCommand -TimeOut 30
        if ($accessResult.ExitStatus -eq 0) {
            $status.remote_access_status = "full_access_granted"
        }
        else {
            $status.remote_access_status = "access_update_failed"
        }

        if (-not [string]::IsNullOrWhiteSpace($consumerTemplateAbs) -and (Test-Path -Path $consumerTemplateAbs)) {
            Set-SFTPItem -SessionId ([int]$connections.sftp.SessionId) -Path $consumerTemplateAbs -Destination $resolvedToolsRoot -Force -ErrorAction Stop | Out-Null
            $consumerTemplateUploaded = $true
        }

        if ($consumerTemplateUploaded) {
            $consumerCommand = "python3 '{0}' --input '{1}' --output '{2}' || python '{0}' --input '{1}' --output '{2}'" -f $status.remote_consumer_script_path, $status.remote_primary_path, $status.remote_summary_path
            $consumerResult = Invoke-SSHCommand -SessionId ([int]$connections.ssh.SessionId) -Command $consumerCommand -TimeOut 30
            if ($consumerResult.ExitStatus -eq 0) {
                $status.consumer_status = "executed"
            }
            else {
                $status.consumer_status = "consumer_failed"
            }
        }
        else {
            $status.consumer_status = "consumer_template_missing"
        }

    }
    catch {
        $status.status = "upload_failed"
        $status.error = [string]$_.Exception.Message
    }
    finally {
        foreach ($tempUploadPath in @($tempUploadPaths)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$tempUploadPath) -and (Test-Path -Path ([string]$tempUploadPath))) {
                Remove-Item -Path ([string]$tempUploadPath) -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if (($null -ne $mirrorConnections) -and ($mirrorConnections -ne $connections)) {
            Close-MimSshConnections -Connections $mirrorConnections
        }
        Close-MimSshConnections -Connections $connections
    }

    $receipt = [pscustomobject]$status
    Write-TodStatusReceipts -Payload $receipt
    return $receipt
}

function Copy-IfSourceExists {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )

    if ([string]::IsNullOrWhiteSpace($SourcePath) -or [string]::IsNullOrWhiteSpace($DestinationPath)) {
        return $false
    }

    $srcAbs = Get-LocalPath -PathValue $SourcePath
    $dstAbs = Get-LocalPath -PathValue $DestinationPath
    if (-not (Test-Path -Path $srcAbs)) {
        return $false
    }

    $srcFull = [System.IO.Path]::GetFullPath($srcAbs)
    $dstFull = [System.IO.Path]::GetFullPath($dstAbs)
    if ([string]::Equals($srcFull, $dstFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }

    Ensure-ParentDirectoryForFile -FilePath $dstAbs
    Copy-Item -Path $srcAbs -Destination $dstAbs -Force
    return $true
}

function Copy-FromSshIfAvailable {
    param(
        [Parameter(Mandatory = $true)][string]$Scp,
        [Parameter(Mandatory = $true)][string]$RemoteHost,
        [int]$RemotePort = 22,
        [Parameter(Mandatory = $true)][string]$RemotePath,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [switch]$NonInteractive,
        [switch]$Required
    )

    $result = [pscustomobject]@{
        ok = $false
        remote_path = $RemotePath
        local_path = $LocalPath
        required = [bool]$Required
        error = ""
    }

    try {
        Ensure-ParentDirectoryForFile -FilePath $LocalPath

        $args = @()
        if ($NonInteractive) {
            $args += @("-o", "BatchMode=yes")
            $args += @("-o", "ConnectTimeout=10")
        }
        if ($RemotePort -gt 0) {
            $args += @("-P", [string]$RemotePort)
        }
        $args += @(("{0}:{1}" -f $RemoteHost, $RemotePath), $LocalPath)

        & $Scp @args 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Path -Path $LocalPath -PathType Leaf)) {
            $result.ok = $true
            return $result
        }

        if ($Required) {
            $result.error = "scp_failed"
        }
        else {
            $result.error = "optional_missing"
        }
    }
    catch {
        $result.error = [string]$_.Exception.Message
    }

    return $result
}

function Copy-FromSftpIfAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$SessionId,
        [Parameter(Mandatory = $true)][string]$RemotePath,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [switch]$Required
    )

    $result = [pscustomobject]@{
        ok = $false
        remote_path = $RemotePath
        local_path = $LocalPath
        required = [bool]$Required
        error = ""
    }

    try {
        Ensure-ParentDirectoryForFile -FilePath $LocalPath
        $destinationDir = Split-Path -Parent $LocalPath
        if ([string]::IsNullOrWhiteSpace($destinationDir)) {
            $destinationDir = Get-Location
        }
        Get-SFTPItem -SessionId $SessionId -Path $RemotePath -Destination $destinationDir -Force -ErrorAction Stop | Out-Null
        if (Test-Path -Path $LocalPath -PathType Leaf) {
            $result.ok = $true
            return $result
        }

        if ($Required) {
            $result.error = "sftp_failed"
        }
        else {
            $result.error = "optional_missing"
        }
    }
    catch {
        $errorText = [string]$_.Exception.Message
        if ([string]::IsNullOrWhiteSpace($errorText)) {
            $errorText = if ($Required) { "sftp_failed" } else { "optional_missing" }
        }
        if ($Required) {
            $result.error = $errorText
        }
        else {
            $result.error = $errorText
        }
    }

    return $result
}

function Invoke-MimSshRefresh {
    param(
        [Parameter(Mandatory = $true)][string]$Scp,
        [Parameter(Mandatory = $true)][string]$RemoteHost,
        [string]$RemoteUser,
        [int]$RemotePort,
        [string]$RemotePassword,
        [Parameter(Mandatory = $true)][string]$RemoteRoot,
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [string]$DotEnvPath,
        [switch]$AllowInteractiveSshPrompt
    )

    $stageAbs = Get-LocalPath -PathValue $StageRoot
    New-DirectoryIfMissing -PathValue $stageAbs

    $jsonRemote = ("{0}/MIM_CONTEXT_EXPORT.latest.json" -f $RemoteRoot.TrimEnd('/'))
    $yamlRemote = ("{0}/MIM_CONTEXT_EXPORT.latest.yaml" -f $RemoteRoot.TrimEnd('/'))
    $manifestRemote = ("{0}/MIM_MANIFEST.latest.json" -f $RemoteRoot.TrimEnd('/'))
    $packetRemote = ("{0}/MIM_TOD_HANDSHAKE_PACKET.latest.json" -f $RemoteRoot.TrimEnd('/'))
    $requestRemote = ("{0}/MIM_TOD_TASK_REQUEST.latest.json" -f $RemoteRoot.TrimEnd('/'))

    $jsonLocal = Join-Path $stageAbs "MIM_CONTEXT_EXPORT.latest.json"
    $yamlLocal = Join-Path $stageAbs "MIM_CONTEXT_EXPORT.latest.yaml"
    $manifestLocal = Join-Path $stageAbs "MIM_MANIFEST.latest.json"
    $packetLocal = Join-Path $stageAbs "MIM_TOD_HANDSHAKE_PACKET.latest.json"
    $requestLocal = Join-Path $stageAbs "MIM_TOD_TASK_REQUEST.latest.json"

    $dotEnvAbs = ""
    if (-not [string]::IsNullOrWhiteSpace($DotEnvPath)) {
        $dotEnvAbs = Get-LocalPath -PathValue $DotEnvPath
    }

    $sshUser = Resolve-MimSshSettingValue -ExplicitValue $RemoteUser -EnvVarName "MIM_SSH_USER" -DotEnvPath $dotEnvAbs
    if ([string]::IsNullOrWhiteSpace($sshUser)) { $sshUser = "testpilot" }

    $sshPortValue = ""
    if ($RemotePort -gt 0) {
        $sshPortValue = [string]$RemotePort
    }
    $sshPortText = Resolve-MimSshSettingValue -ExplicitValue $sshPortValue -EnvVarName "MIM_SSH_PORT" -DotEnvPath $dotEnvAbs
    $sshPort = 22
    if (-not [string]::IsNullOrWhiteSpace($sshPortText)) {
        $parsedPort = 0
        if ([int]::TryParse($sshPortText, [ref]$parsedPort) -and $parsedPort -gt 0) {
            $sshPort = $parsedPort
        }
    }

    $sshPassword = Resolve-MimSshSettingValue -ExplicitValue $RemotePassword -EnvVarName "MIM_SSH_PASSWORD" -DotEnvPath $dotEnvAbs
    $canUsePassword = (-not [string]::IsNullOrWhiteSpace($sshPassword)) -and ($sshPassword -ne "CHANGE_ME")
    $nonInteractiveScp = (-not [bool]$AllowInteractiveSshPrompt)
    $resolvedSftpHost = Resolve-SshHostAlias -RemoteHost $RemoteHost

    $jsonPull = $null
    $yamlPull = $null
    $manifestPull = $null
    $packetPull = $null
    $requestPull = $null
    $authMode = "scp"

    if ($canUsePassword -and (Get-Module -ListAvailable -Name Posh-SSH)) {
        $authMode = "sftp_password"
        Import-Module Posh-SSH -ErrorAction Stop | Out-Null

        $securePassword = ConvertTo-SecureString $sshPassword -AsPlainText -Force
        $credential = New-Object System.Management.Automation.PSCredential ($sshUser, $securePassword)

        $session = $null
        try {
            $session = New-SFTPSession -ComputerName $resolvedSftpHost -Port $sshPort -Credential $credential -AcceptKey -ConnectionTimeout 30000
            $jsonPull = Copy-FromSftpIfAvailable -SessionId ([int]$session.SessionId) -RemotePath $jsonRemote -LocalPath $jsonLocal -Required
            $yamlPull = Copy-FromSftpIfAvailable -SessionId ([int]$session.SessionId) -RemotePath $yamlRemote -LocalPath $yamlLocal -Required
            $manifestPull = Copy-FromSftpIfAvailable -SessionId ([int]$session.SessionId) -RemotePath $manifestRemote -LocalPath $manifestLocal
            $packetPull = Copy-FromSftpIfAvailable -SessionId ([int]$session.SessionId) -RemotePath $packetRemote -LocalPath $packetLocal
            $requestPull = Copy-FromSftpIfAvailable -SessionId ([int]$session.SessionId) -RemotePath $requestRemote -LocalPath $requestLocal
        }
        catch {
            $authMode = "scp"
        }
        finally {
            if ($null -ne $session) {
                Remove-SFTPSession -SessionId ([int]$session.SessionId) | Out-Null
            }
        }
    }

    if ($null -eq $jsonPull -or $null -eq $yamlPull -or $null -eq $manifestPull -or $null -eq $packetPull -or $null -eq $requestPull) {
        $scpTarget = $RemoteHost
        if ($scpTarget -notmatch "@") {
            $scpTarget = ("{0}@{1}" -f $sshUser, $scpTarget)
        }

        $jsonPull = Copy-FromSshIfAvailable -Scp $Scp -RemoteHost $scpTarget -RemotePort $sshPort -RemotePath $jsonRemote -LocalPath $jsonLocal -NonInteractive:$nonInteractiveScp -Required
        $yamlPull = Copy-FromSshIfAvailable -Scp $Scp -RemoteHost $scpTarget -RemotePort $sshPort -RemotePath $yamlRemote -LocalPath $yamlLocal -NonInteractive:$nonInteractiveScp -Required
        $manifestPull = Copy-FromSshIfAvailable -Scp $Scp -RemoteHost $scpTarget -RemotePort $sshPort -RemotePath $manifestRemote -LocalPath $manifestLocal -NonInteractive:$nonInteractiveScp
        $packetPull = Copy-FromSshIfAvailable -Scp $Scp -RemoteHost $scpTarget -RemotePort $sshPort -RemotePath $packetRemote -LocalPath $packetLocal -NonInteractive:$nonInteractiveScp
        $requestPull = Copy-FromSshIfAvailable -Scp $Scp -RemoteHost $scpTarget -RemotePort $sshPort -RemotePath $requestRemote -LocalPath $requestLocal -NonInteractive:$nonInteractiveScp
    }

    return [pscustomobject]@{
        ok = ([bool]$jsonPull.ok -and [bool]$yamlPull.ok)
        stage_root = $StageRoot
        stage_root_abs = $stageAbs
        resolved_sftp_host = $resolvedSftpHost
        source_json = $jsonLocal
        source_yaml = $yamlLocal
        source_manifest = $manifestLocal
        source_handshake_packet = $packetLocal
        source_task_request = $requestLocal
        auth_mode = $authMode
        non_interactive_scp = [bool]$nonInteractiveScp
        pulls = [pscustomobject]@{
            json = $jsonPull
            yaml = $yamlPull
            manifest = $manifestPull
            handshake_packet = $packetPull
            task_request = $requestPull
        }
    }
}

function Get-MimSharedSourceCandidates {
    param(
        [string]$ExplicitJsonPath,
        [string]$ExplicitYamlPath,
        [string]$ExplicitManifestPath,
        [string]$PreferredRoot,
        [string]$EnvRoot
    )

    $candidates = @()

    if ((-not [string]::IsNullOrWhiteSpace($ExplicitJsonPath)) -or (-not [string]::IsNullOrWhiteSpace($ExplicitYamlPath)) -or (-not [string]::IsNullOrWhiteSpace($ExplicitManifestPath))) {
        $explicitRoot = ""
        if (-not [string]::IsNullOrWhiteSpace($ExplicitJsonPath)) {
            $explicitRoot = Split-Path -Parent $ExplicitJsonPath
        }
        elseif (-not [string]::IsNullOrWhiteSpace($ExplicitYamlPath)) {
            $explicitRoot = Split-Path -Parent $ExplicitYamlPath
        }
        elseif (-not [string]::IsNullOrWhiteSpace($ExplicitManifestPath)) {
            $explicitRoot = Split-Path -Parent $ExplicitManifestPath
        }

        $candidates += [pscustomobject]@{
            root = $explicitRoot
            source_json = $ExplicitJsonPath
            source_yaml = $ExplicitYamlPath
            source_manifest = $ExplicitManifestPath
        }
    }

    $roots = @()
    if (-not [string]::IsNullOrWhiteSpace($PreferredRoot)) { $roots += $PreferredRoot }
    if (-not [string]::IsNullOrWhiteSpace($EnvRoot)) { $roots += $EnvRoot }
    $roots += "../MIM/runtime/shared"
    $roots += "../mim/runtime/shared"
    $roots += "/shared_state"

    $seen = @{}
    foreach ($root in $roots) {
        $rootText = [string]$root
        if ([string]::IsNullOrWhiteSpace($rootText)) { continue }
        if ($seen.ContainsKey($rootText)) { continue }
        $seen[$rootText] = $true

        $candidates += [pscustomobject]@{
            root = $rootText
            source_json = (Join-Path $rootText "MIM_CONTEXT_EXPORT.latest.json")
            source_yaml = (Join-Path $rootText "MIM_CONTEXT_EXPORT.latest.yaml")
            source_manifest = (Join-Path $rootText "MIM_MANIFEST.latest.json")
        }
    }

    return @($candidates)
}

function Resolve-MimSharedSourceCandidate {
    param([Parameter(Mandatory = $true)]$Candidates)

    $candidatePathsTried = @()
    $permissionDenied = $false
    $badFilename = $false

    foreach ($candidate in @($Candidates)) {
        $jsonPath = [string]$candidate.source_json
        $yamlPath = [string]$candidate.source_yaml
        $manifestPath = [string]$candidate.source_manifest
        $paths = @($jsonPath, $yamlPath, $manifestPath)

        foreach ($path in $paths) {
            if (-not [string]::IsNullOrWhiteSpace($path)) {
                $candidatePathsTried += $path
            }
        }

        if ([string]::IsNullOrWhiteSpace($jsonPath) -or [string]::IsNullOrWhiteSpace($yamlPath)) {
            $badFilename = $true
            continue
        }

        try {
            $rootPath = [string]$candidate.root
            if ([string]::IsNullOrWhiteSpace($rootPath)) {
                $rootPath = Split-Path -Parent ([string]$candidate.source_json)
            }

            $rootAbs = Get-LocalPath -PathValue $rootPath
            if (-not (Test-Path -Path $rootAbs)) {
                continue
            }

            $jsonAbs = Get-LocalPath -PathValue $jsonPath
            $yamlAbs = Get-LocalPath -PathValue $yamlPath
            $manifestAbs = if ([string]::IsNullOrWhiteSpace($manifestPath)) { "" } else { Get-LocalPath -PathValue $manifestPath }

            $hasJson = Test-Path -Path $jsonAbs -PathType Leaf
            $hasYaml = Test-Path -Path $yamlAbs -PathType Leaf
            $hasManifest = if ([string]::IsNullOrWhiteSpace($manifestAbs)) { $false } else { Test-Path -Path $manifestAbs -PathType Leaf }

            if ($hasJson -and $hasYaml) {
                return [pscustomobject]@{
                    resolved = $true
                    candidate = $candidate
                    candidate_paths_tried = @($candidatePathsTried)
                    failure_reason = ""
                }
            }

            $badFilename = $true
        }
        catch [System.UnauthorizedAccessException] {
            $permissionDenied = $true
        }
        catch {
            $badFilename = $true
        }
    }

    $reason = "path_not_found"
    if ($permissionDenied) {
        $reason = "permission_denied"
    }
    elseif ($badFilename) {
        $reason = "bad_filename"
    }

    return [pscustomobject]@{
        resolved = $false
        candidate = $null
        candidate_paths_tried = @($candidatePathsTried)
        failure_reason = $reason
    }
}

function Get-MimStatusSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [double]$StaleAfterHours = 6
    )

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) {
        return [pscustomobject]@{
            available = $false
            source_path = $PathValue
            generated_at = ""
            age_hours = $null
            stale_after_hours = $StaleAfterHours
            is_stale = $true
            objective_active = ""
            phase = ""
            blockers = ""
        }
    }

    $generatedAt = ""
    if ($doc.PSObject.Properties["generated_at"]) {
        $generatedAt = [string]$doc.generated_at
    }
    elseif ($doc.PSObject.Properties["exported_at"]) {
        $generatedAt = [string]$doc.exported_at
    }

    $objectiveActive = ""
    $phase = ""
    $blockers = ""
    if ($doc.PSObject.Properties["status"] -and $doc.status) {
        if ($doc.status.PSObject.Properties["objective_active"]) { $objectiveActive = [string]$doc.status.objective_active }
        if ($doc.status.PSObject.Properties["phase"]) { $phase = [string]$doc.status.phase }
        if ($doc.status.PSObject.Properties["blockers"]) { $blockers = [string]$doc.status.blockers }
    }
    else {
        if ($doc.PSObject.Properties["objective_active"]) { $objectiveActive = [string]$doc.objective_active }
        if ($doc.PSObject.Properties["phase"]) { $phase = [string]$doc.phase }
        if ($doc.PSObject.Properties["blockers"]) {
            $rawBlockers = $doc.blockers
            if ($rawBlockers -is [System.Array] -or ($rawBlockers -is [System.Collections.IEnumerable] -and -not ($rawBlockers -is [string]))) {
                $blockerItems = @($rawBlockers | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
                $blockers = (@($blockerItems) -join "; ")
            }
            else {
                $blockers = [string]$rawBlockers
            }
        }
    }

    $ageHours = $null
    $isStale = $true
    $generatedUtc = Convert-ToUtcDateOrNull -Value $generatedAt
    if ($null -ne $generatedUtc) {
        $ageHours = [math]::Round(((Get-Date).ToUniversalTime() - $generatedUtc).TotalHours, 2)
        $isStale = ($ageHours -gt $StaleAfterHours)
    }

    return [pscustomobject]@{
        available = $true
        source_path = $PathValue
        generated_at = $generatedAt
        age_hours = $ageHours
        stale_after_hours = $StaleAfterHours
        is_stale = [bool]$isStale
        objective_active = $objectiveActive
        phase = $phase
        blockers = $blockers
    }
}

function Get-MimHandshakePacketSnapshot {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return [pscustomobject]@{
            available = $false
            source_path = ""
            generated_at = ""
            handshake_version = ""
            objective_active = ""
            latest_completed_objective = ""
            current_next_objective = ""
            schema_version = ""
            release_tag = ""
            regression_status = ""
            regression_tests = ""
            prod_promotion_status = ""
            prod_smoke_status = ""
            blockers = @()
        }
    }

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) {
        return [pscustomobject]@{
            available = $false
            source_path = $PathValue
            generated_at = ""
            handshake_version = ""
            objective_active = ""
            latest_completed_objective = ""
            current_next_objective = ""
            schema_version = ""
            release_tag = ""
            regression_status = ""
            regression_tests = ""
            prod_promotion_status = ""
            prod_smoke_status = ""
            blockers = @()
        }
    }

    $truth = $null
    if ($doc.PSObject.Properties["truth"] -and $doc.truth) {
        $truth = $doc.truth
    }
    else {
        $truth = $doc
    }

    $blockers = @()
    if ($truth.PSObject.Properties["blockers"] -and $null -ne $truth.blockers) {
        $blockers = Convert-ToStringList -Value $truth.blockers
    }

    return [pscustomobject]@{
        available = $true
        source_path = $PathValue
        generated_at = if ($doc.PSObject.Properties["generated_at"]) { [string]$doc.generated_at } else { "" }
        handshake_version = if ($doc.PSObject.Properties["handshake_version"]) { [string]$doc.handshake_version } else { "" }
        objective_active = if ($truth.PSObject.Properties["objective_active"]) { [string]$truth.objective_active } else { "" }
        latest_completed_objective = if ($truth.PSObject.Properties["latest_completed_objective"]) { [string]$truth.latest_completed_objective } else { "" }
        current_next_objective = if ($truth.PSObject.Properties["current_next_objective"]) { [string]$truth.current_next_objective } else { "" }
        schema_version = if ($truth.PSObject.Properties["schema_version"]) { [string]$truth.schema_version } else { "" }
        release_tag = if ($truth.PSObject.Properties["release_tag"]) { [string]$truth.release_tag } else { "" }
        regression_status = if ($truth.PSObject.Properties["regression_status"]) { [string]$truth.regression_status } else { "" }
        regression_tests = if ($truth.PSObject.Properties["regression_tests"]) { [string]$truth.regression_tests } else { "" }
        prod_promotion_status = if ($truth.PSObject.Properties["prod_promotion_status"]) { [string]$truth.prod_promotion_status } else { "" }
        prod_smoke_status = if ($truth.PSObject.Properties["prod_smoke_status"]) { [string]$truth.prod_smoke_status } else { "" }
        blockers = @($blockers)
    }
}

function Get-LiveTaskRequestSnapshot {
    param([string]$PathValue)

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) {
        return [pscustomobject]@{
            available = $false
            source_path = $PathValue
            generated_at = ""
            request_id = ""
            task_id = ""
            objective_id = ""
            normalized_objective_id = ""
            correlation_id = ""
            promotion_applied = $false
            promotion_reason = ""
        }
    }

    $requestId = if ($doc.PSObject.Properties["request_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.request_id)) {
        [string]$doc.request_id
    }
    elseif ($doc.PSObject.Properties["task_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.task_id)) {
        [string]$doc.task_id
    }
    else {
        ""
    }

    $objectiveId = ""
    if ($doc.PSObject.Properties["objective_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.objective_id)) {
        $objectiveId = [string]$doc.objective_id
    }
    elseif (-not [string]::IsNullOrWhiteSpace($requestId)) {
        $requestMatch = [regex]::Match($requestId, '^objective-(?<objective>\d+)-task-\d+$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if ($requestMatch.Success) {
            $objectiveId = [string]$requestMatch.Groups['objective'].Value
        }
    }

    return [pscustomobject]@{
        available = $true
        source_path = $PathValue
        generated_at = if ($doc.PSObject.Properties["generated_at"]) { [string]$doc.generated_at } else { "" }
        request_id = $requestId
        task_id = if ($doc.PSObject.Properties["task_id"]) { [string]$doc.task_id } else { "" }
        objective_id = $objectiveId
        normalized_objective_id = Normalize-ObjectiveIdText -Value $objectiveId
        correlation_id = if ($doc.PSObject.Properties["correlation_id"]) { [string]$doc.correlation_id } else { "" }
        promotion_applied = $false
        promotion_reason = ""
    }
}

function Get-LiveTaskRequestSnapshotTimestamp {
    param($Snapshot)

    if ($null -eq $Snapshot) {
        return $null
    }

    $generatedAt = if ($Snapshot.PSObject.Properties['generated_at']) { [string]$Snapshot.generated_at } else { '' }
    return Convert-ToUtcDateOrNull -Value $generatedAt
}

function Get-PreferredLiveTaskRequestSnapshot {
    param(
        [Parameter(Mandatory = $true)][string[]]$CandidatePaths
    )

    $bestSnapshot = $null
    $bestTimestamp = $null

    foreach ($candidate in @($CandidatePaths | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace([string]$candidate)) {
            continue
        }

        $snapshot = Get-LiveTaskRequestSnapshot -PathValue ([string]$candidate)
        if (-not [bool]$snapshot.available) {
            continue
        }

        $snapshotTimestamp = Get-LiveTaskRequestSnapshotTimestamp -Snapshot $snapshot
        if ($null -eq $bestSnapshot) {
            $bestSnapshot = $snapshot
            $bestTimestamp = $snapshotTimestamp
            continue
        }

        if ($null -ne $snapshotTimestamp -and ($null -eq $bestTimestamp -or $snapshotTimestamp -gt $bestTimestamp)) {
            $bestSnapshot = $snapshot
            $bestTimestamp = $snapshotTimestamp
        }
    }

    if ($null -ne $bestSnapshot) {
        return $bestSnapshot
    }

    $fallbackPath = if (@($CandidatePaths).Count -gt 0) { [string]$CandidatePaths[0] } else { '' }
    return Get-LiveTaskRequestSnapshot -PathValue $fallbackPath
}

function Get-ListenerDecisionSnapshot {
    param(
        [string]$PathValue,
        $AuthorityReset = $null
    )

    $doc = Get-JsonFileIfExists -PathValue $PathValue
    if ($null -eq $doc) {
        return [pscustomobject]@{
            available = $false
            source_path = $PathValue
            generated_at = ""
            request_id = ""
            task_id = ""
            objective_id = ""
            normalized_objective_id = ""
            correlation_id = ""
            decision_outcome = ""
            reason_code = ""
            summary = ""
            boundary_class = ""
            ack_state = ""
            execution_state = ""
            blocker_classification = ""
            next_step_recommendation = ""
        }
    }

    $requestId = if ($doc.PSObject.Properties["request_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.request_id)) {
        [string]$doc.request_id
    }
    elseif ($doc.PSObject.Properties["task_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.task_id)) {
        [string]$doc.task_id
    }
    else {
        ""
    }

    $objectiveId = ""
    if ($doc.PSObject.Properties["requested_objective_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.requested_objective_id)) {
        $objectiveId = [string]$doc.requested_objective_id
    }
    elseif ($doc.PSObject.Properties["objective_id"] -and -not [string]::IsNullOrWhiteSpace([string]$doc.objective_id)) {
        $objectiveId = [string]$doc.objective_id
    }
    elseif (-not [string]::IsNullOrWhiteSpace($requestId)) {
        $objectiveId = Get-ObjectiveIdFromTaskReference -Value $requestId
    }

    $decisionOutcome = if ($doc.PSObject.Properties["decision_outcome"]) { [string]$doc.decision_outcome } else { "" }
    $reasonCode = if ($doc.PSObject.Properties["reason_code"]) { [string]$doc.reason_code } else { "" }
    $summary = if ($doc.PSObject.Properties["summary"]) { [string]$doc.summary } else { "" }
    $boundaryClass = if ($doc.PSObject.Properties["boundary_class"]) { [string]$doc.boundary_class } else { "" }
    $ackState = if ($doc.PSObject.Properties["ack_state"]) { [string]$doc.ack_state } else { "" }
    $executionState = if ($doc.PSObject.Properties["execution_state"]) { [string]$doc.execution_state } else { "" }
    $blockerClassification = if ($doc.PSObject.Properties["blocker_classification"]) { [string]$doc.blocker_classification } else { "" }
    $nextStepRecommendation = if ($doc.PSObject.Properties["next_step_recommendation"]) { [string]$doc.next_step_recommendation } else { "" }
    $suppressedReason = ""

    if ([string]::Equals($reasonCode, 'authority_reset_ceiling_exceeded', [System.StringComparison]::OrdinalIgnoreCase)) {
        $authorityResetIsActive = $false
        if ($null -ne $AuthorityReset -and $AuthorityReset.PSObject.Properties['active']) {
            try {
                $authorityResetIsActive = [bool]$AuthorityReset.active
            }
            catch {
                $authorityResetIsActive = $false
            }
        }

        if (-not $authorityResetIsActive) {
            $decisionOutcome = 'ignored_stale_listener_decision'
            $summary = 'Ignored stale authority-reset rejection because objective authority reset is inactive.'
            $ackState = ''
            $executionState = 'ignored'
            $blockerClassification = 'stale_listener_artifact'
            $nextStepRecommendation = ''
            $suppressedReason = 'inactive_authority_reset'
        }
    }

    return [pscustomobject]@{
        available = $true
        source_path = $PathValue
        generated_at = if ($doc.PSObject.Properties["generated_at"]) { [string]$doc.generated_at } else { "" }
        request_id = $requestId
        task_id = if ($doc.PSObject.Properties["task_id"]) { [string]$doc.task_id } else { "" }
        objective_id = $objectiveId
        normalized_objective_id = Normalize-ObjectiveIdText -Value $objectiveId
        correlation_id = if ($doc.PSObject.Properties["correlation_id"]) { [string]$doc.correlation_id } else { "" }
        decision_outcome = $decisionOutcome
        reason_code = $reasonCode
        summary = $summary
        boundary_class = $boundaryClass
        ack_state = $ackState
        execution_state = $executionState
        blocker_classification = $blockerClassification
        next_step_recommendation = $nextStepRecommendation
        suppressed_reason = $suppressedReason
    }
}

function Get-ObjectiveAlignment {
    param(
        [Parameter(Mandatory = $true)][string]$TodObjective,
        [string]$MimObjectiveActive,
        [string]$MimObjectiveSource
    )

    $todNumber = Get-IdNumber -Value $TodObjective
    $mimObjectiveRaw = if ([string]::IsNullOrWhiteSpace($MimObjectiveActive)) { "" } else { [string]$MimObjectiveActive }
    $mimNumber = Get-IdNumber -Value $mimObjectiveRaw

    $alignmentStatus = "unknown"
    $aligned = $false
    $delta = $null
    if ($todNumber -ge 0 -and $mimNumber -ge 0) {
        $aligned = ($todNumber -eq $mimNumber)
        $delta = ($todNumber - $mimNumber)
        $alignmentStatus = if ($aligned) { "in_sync" } else { "mismatch" }
    }

    return [pscustomobject]@{
        status = $alignmentStatus
        aligned = [bool]$aligned
        tod_current_objective = $TodObjective
        mim_objective_active = $mimObjectiveRaw
        mim_objective_source = if ([string]::IsNullOrWhiteSpace($MimObjectiveSource)) { "unknown" } else { $MimObjectiveSource }
        delta = $delta
    }
}

function Test-IsTerminalExecutionStatus {
    param([string]$Status)

    if ([string]::IsNullOrWhiteSpace($Status)) {
        return $false
    }

    $normalized = ([string]$Status).Trim().ToLowerInvariant()
    return @('completed', 'succeeded', 'already_processed', 'stale_request_ignored', 'stale_backfill_ignored') -contains $normalized
}

function Get-BridgeCanonicalEvidence {
    param(
        $MimRefresh,
        $MimHandshake,
        $LiveTaskRequest,
        $ObjectiveAlignment,
        $TodStatusPublish
    )

    $refreshAttempted = [bool]($MimRefresh -and $MimRefresh.PSObject.Properties["attempted"] -and $MimRefresh.attempted)
    $copiedManifest = [bool]($MimRefresh -and $MimRefresh.PSObject.Properties["copied_manifest"] -and $MimRefresh.copied_manifest)
    $sourceManifest = if ($MimRefresh -and $MimRefresh.PSObject.Properties["source_manifest"]) { [string]$MimRefresh.source_manifest } else { "" }
    $sourceHandshakePacket = if ($MimRefresh -and $MimRefresh.PSObject.Properties["source_handshake_packet"]) { [string]$MimRefresh.source_handshake_packet } else { "" }
    $handshakeAvailable = [bool]($MimHandshake -and $MimHandshake.PSObject.Properties["available"] -and $MimHandshake.available)

    $liveTaskAvailable = [bool]($LiveTaskRequest -and $LiveTaskRequest.PSObject.Properties["available"] -and $LiveTaskRequest.available)
    $liveTaskRequestId = if ($LiveTaskRequest -and $LiveTaskRequest.PSObject.Properties["request_id"]) { [string]$LiveTaskRequest.request_id } else { "" }
    $liveTaskId = if ($LiveTaskRequest -and $LiveTaskRequest.PSObject.Properties["task_id"] -and -not [string]::IsNullOrWhiteSpace([string]$LiveTaskRequest.task_id)) { [string]$LiveTaskRequest.task_id } else { $liveTaskRequestId }
    $liveTaskObjective = if ($LiveTaskRequest -and $LiveTaskRequest.PSObject.Properties["normalized_objective_id"]) { [string]$LiveTaskRequest.normalized_objective_id } else { "" }
    $liveTaskPromotionApplied = [bool]($LiveTaskRequest -and $LiveTaskRequest.PSObject.Properties["promotion_applied"] -and $LiveTaskRequest.promotion_applied)
    $objectiveInSync = [bool]($ObjectiveAlignment -and $ObjectiveAlignment.PSObject.Properties["status"] -and [string]$ObjectiveAlignment.status -eq "in_sync")
    $sharedTruthPath = Join-Path $repoRoot 'runtime/shared/TOD_MIM_SHARED_TRUTH.latest.json'
    $sharedTruth = Get-JsonFileIfExists -PathValue $sharedTruthPath
    $canonicalTaskId = if ($sharedTruth -and $sharedTruth.PSObject.Properties['task_id'] -and -not [string]::IsNullOrWhiteSpace([string]$sharedTruth.task_id)) {
        [string]$sharedTruth.task_id
    }
    elseif ($sharedTruth -and $sharedTruth.PSObject.Properties['request_id'] -and -not [string]::IsNullOrWhiteSpace([string]$sharedTruth.request_id)) {
        [string]$sharedTruth.request_id
    }
    else {
        ""
    }
    $canonicalObjective = if ($ObjectiveAlignment -and $ObjectiveAlignment.PSObject.Properties["mim_objective_active"] -and -not [string]::IsNullOrWhiteSpace([string]$ObjectiveAlignment.mim_objective_active)) {
        Normalize-ObjectiveIdText -Value ([string]$ObjectiveAlignment.mim_objective_active)
    }
    elseif ($MimHandshake -and $MimHandshake.PSObject.Properties["current_next_objective"] -and -not [string]::IsNullOrWhiteSpace([string]$MimHandshake.current_next_objective)) {
        Normalize-ObjectiveIdText -Value ([string]$MimHandshake.current_next_objective)
    }
    elseif ($MimHandshake -and $MimHandshake.PSObject.Properties["objective_active"] -and -not [string]::IsNullOrWhiteSpace([string]$MimHandshake.objective_active)) {
        Normalize-ObjectiveIdText -Value ([string]$MimHandshake.objective_active)
    }
    else {
        ""
    }
    $liveTaskMatchesCanonical = [bool](
        -not $liveTaskAvailable -or
        [string]::IsNullOrWhiteSpace($canonicalObjective) -or
        [string]::IsNullOrWhiteSpace($liveTaskObjective) -or
        [string]::Equals($liveTaskObjective, $canonicalObjective, [System.StringComparison]::OrdinalIgnoreCase)
    )
    $liveTaskMatchesCanonicalTask = [bool](
        -not $liveTaskAvailable -or
        [string]::IsNullOrWhiteSpace($canonicalTaskId) -or
        [string]::IsNullOrWhiteSpace($liveTaskId) -or
        [string]::Equals($liveTaskId, $canonicalTaskId, [System.StringComparison]::OrdinalIgnoreCase)
    )
    $liveTaskMatchesCanonical = [bool]($liveTaskMatchesCanonical -and $liveTaskMatchesCanonicalTask)

    $statusUploaded = [bool]($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["status"] -and [string]$TodStatusPublish.status -eq "uploaded")
    $mirrorSatisfied = [bool]($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["mim_mirror_status"] -and [string]$TodStatusPublish.mim_mirror_status -eq "mirrored")
    $remoteAccessSatisfied = [bool]($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["remote_access_status"] -and [string]$TodStatusPublish.remote_access_status -eq "full_access_granted")
    $consumerSatisfied = [bool]($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["consumer_status"] -and [string]$TodStatusPublish.consumer_status -eq "executed")
    $remotePublishVerified = [bool]($statusUploaded -and $mirrorSatisfied -and $remoteAccessSatisfied -and $consumerSatisfied)

    $explicitRefreshSatisfied = [bool](
        $refreshAttempted -and
        $copiedManifest -and
        (-not [string]::IsNullOrWhiteSpace($sourceManifest)) -and
        (-not [string]::IsNullOrWhiteSpace($sourceHandshakePacket)) -and
        $handshakeAvailable -and
        $liveTaskMatchesCanonical
    )

    $liveBridgePublishSatisfied = [bool]($liveTaskAvailable -and $objectiveInSync -and $remotePublishVerified -and $liveTaskMatchesCanonical)
    $canonicalRefreshSatisfied = [bool]($explicitRefreshSatisfied -or $liveBridgePublishSatisfied)

    $failureSignals = @()
    if (-not $liveTaskAvailable) {
        $failureSignals += "listener_task_request_missing"
    }
    if (-not $liveTaskMatchesCanonical) {
        $failureSignals += "live_task_request_objective_mismatch"
    }
    if (-not $liveTaskMatchesCanonicalTask) {
        $failureSignals += "live_task_request_task_mismatch"
    }
    if ($liveTaskAvailable -and -not $liveTaskPromotionApplied -and -not $liveTaskMatchesCanonical) {
        $failureSignals += "live_task_request_not_promoted"
    }
    if (-not $objectiveInSync) {
        $failureSignals += "objective_alignment_not_in_sync"
    }
    if (-not $statusUploaded) {
        $failureSignals += "tod_status_publish_not_uploaded"
    }
    if (-not $mirrorSatisfied) {
        $failureSignals += "mim_status_not_mirrored"
    }
    if (-not $remoteAccessSatisfied) {
        $failureSignals += "remote_access_not_granted"
    }
    if (-not $consumerSatisfied) {
        $failureSignals += "remote_consumer_not_executed"
    }

    $evidenceSource = "missing"
    $status = "fail"
    if ($explicitRefreshSatisfied) {
        $evidenceSource = "explicit_refresh"
        $status = "pass"
    }
    elseif ($liveBridgePublishSatisfied) {
        $evidenceSource = "live_bridge_publish"
        $status = "pass"
    }
    elseif ($liveTaskAvailable -and $objectiveInSync -and ($statusUploaded -or $mirrorSatisfied -or $consumerSatisfied)) {
        $evidenceSource = "partial_live_bridge_publish"
        $status = "warning"
    }

    return [pscustomobject]@{
        status = $status
        canonical_refresh_satisfied = [bool]$canonicalRefreshSatisfied
        evidence_source = $evidenceSource
        explicit_refresh_satisfied = [bool]$explicitRefreshSatisfied
        live_bridge_publish_satisfied = [bool]$liveBridgePublishSatisfied
        remote_publish_verified = [bool]$remotePublishVerified
        live_task_request_available = [bool]$liveTaskAvailable
        live_task_request_id = $liveTaskRequestId
        live_task_task_id = $liveTaskId
        live_task_objective = $liveTaskObjective
        canonical_task_id = $canonicalTaskId
        objective_in_sync = [bool]$objectiveInSync
        status_uploaded = [bool]$statusUploaded
        mim_mirror_status = if ($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["mim_mirror_status"]) { [string]$TodStatusPublish.mim_mirror_status } else { "" }
        remote_access_status = if ($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["remote_access_status"]) { [string]$TodStatusPublish.remote_access_status } else { "" }
        consumer_status = if ($TodStatusPublish -and $TodStatusPublish.PSObject.Properties["consumer_status"]) { [string]$TodStatusPublish.consumer_status } else { "" }
        failure_signals = @($failureSignals)
    }
}

function Get-BridgeOperatorGuidance {
    param($BridgeCanonicalEvidence)

    if ($null -eq $BridgeCanonicalEvidence) {
        return @(
            [pscustomobject]@{
                code = "bridge_guidance_unavailable"
                severity = "warning"
                summary = "Bridge guidance is unavailable because canonical evidence was not generated."
                recommended_action = "Refresh shared-state sync and inspect listener-stage bridge artifacts."
            }
        )
    }

    $signals = @()
    if ($BridgeCanonicalEvidence.PSObject.Properties['failure_signals'] -and $null -ne $BridgeCanonicalEvidence.failure_signals) {
        $signals = @($BridgeCanonicalEvidence.failure_signals | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    if ([bool]$BridgeCanonicalEvidence.canonical_refresh_satisfied) {
        return @(
            [pscustomobject]@{
                code = "bridge_healthy"
                severity = "info"
                summary = ("Bridge evidence is healthy via {0}." -f [string]$BridgeCanonicalEvidence.evidence_source)
                recommended_action = "No recovery is required; only escalate if fresh ACK/result mutation stops or remote publish verification drops."
            }
        )
    }

    $guidance = New-Object System.Collections.Generic.List[object]
    foreach ($signal in $signals) {
        switch ([string]$signal) {
            "listener_task_request_missing" {
                $guidance.Add([pscustomobject]@{
                    code = "listener_task_request_missing"
                    severity = "warning"
                    summary = "Live task request evidence is missing from the listener stage."
                    recommended_action = "Verify the listener request file is being written before restarting broader bridge components."
                })
            }
            "live_task_request_objective_mismatch" {
                $guidance.Add([pscustomobject]@{
                    code = "live_task_request_objective_mismatch"
                    severity = "critical"
                    summary = "Handshake truth and the live task-request packet disagree about the active objective."
                    recommended_action = "Treat the live publication surface as stale until it matches the canonical objective or an explicit ACK explains the divergence."
                })
            }
            "live_task_request_not_promoted" {
                $guidance.Add([pscustomobject]@{
                    code = "live_task_request_not_promoted"
                    severity = "critical"
                    summary = "The live task request was not promoted to the current canonical objective."
                    recommended_action = "Inspect the publisher or promotion path that should republish the live task-request surface from the canonical objective."
                })
            }
            "objective_alignment_not_in_sync" {
                $guidance.Add([pscustomobject]@{
                    code = "objective_alignment_not_in_sync"
                    severity = "warning"
                    summary = "TOD and MIM objectives are not in sync."
                    recommended_action = "Refresh MIM context and shared-state sync first; do not assume the listener is frozen."
                })
            }
            "tod_status_publish_not_uploaded" {
                $guidance.Add([pscustomobject]@{
                    code = "tod_status_publish_not_uploaded"
                    severity = "critical"
                    summary = "TOD status was not uploaded to the remote MIM path."
                    recommended_action = "Republish TOD shared state and verify SSH host, remote path, and receipt generation."
                })
            }
            "mim_status_not_mirrored" {
                $guidance.Add([pscustomobject]@{
                    code = "mim_status_not_mirrored"
                    severity = "critical"
                    summary = "Remote upload completed but the MIM mirror copy was not verified."
                    recommended_action = "Treat this as a remote delivery problem; do not restart the listener unless local ACK/result mutation also stalls."
                })
            }
            "remote_access_not_granted" {
                $guidance.Add([pscustomobject]@{
                    code = "remote_access_not_granted"
                    severity = "critical"
                    summary = "Remote access permissions were not fully granted on published TOD status artifacts."
                    recommended_action = "Repair remote permissions and rerun the publish path."
                })
            }
            "remote_consumer_not_executed" {
                $guidance.Add([pscustomobject]@{
                    code = "remote_consumer_not_executed"
                    severity = "critical"
                    summary = "Remote consumer execution did not complete."
                    recommended_action = "Re-run the publish path and inspect the remote consumer script/runtime rather than recycling the listener."
                })
            }
        }
    }

    return $guidance.ToArray()
}

$sharedDirAbs = Get-LocalPath -PathValue $SharedStateDir
New-DirectoryIfMissing -PathValue $sharedDirAbs

$mimRefresh = [pscustomobject]@{
    attempted = ([bool]$RefreshMimContextFromShared -or [bool]$RefreshMimContextFromSsh)
    copied_json = $false
    copied_yaml = $false
    copied_manifest = $false
    source_json = $MimSharedContextExportPath
    source_yaml = $MimSharedContextExportYamlPath
    source_manifest = $MimSharedManifestPath
    source_handshake_packet = ""
    resolved_source_root = ""
    candidate_paths_tried = @()
    failure_reason = ""
    ssh_attempted = [bool]$RefreshMimContextFromSsh
    ssh_host = ""
    ssh_resolved_host = ""
    ssh_remote_root = ""
    ssh_stage_root = ""
    ssh_auth_mode = ""
    ssh_pull = $null
}

if ($RefreshMimContextFromSsh) {
    $mimRefresh.ssh_host = $MimSshHost
    $mimRefresh.ssh_remote_root = $MimSshSharedRoot
    $mimRefresh.ssh_stage_root = $MimSshStagingRoot

    $sshRefresh = Invoke-MimSshRefresh -Scp $ScpCommand -RemoteHost $MimSshHost -RemoteUser $MimSshUser -RemotePort $MimSshPort -RemotePassword $MimSshPassword -RemoteRoot $MimSshSharedRoot -StageRoot $MimSshStagingRoot -DotEnvPath $DotEnvPath -AllowInteractiveSshPrompt:$AllowInteractiveSshPrompt
    $mimRefresh.ssh_pull = $sshRefresh.pulls
    $mimRefresh.ssh_resolved_host = [string]$sshRefresh.resolved_sftp_host
    $mimRefresh.ssh_auth_mode = [string]$sshRefresh.auth_mode
    if ($sshRefresh.ok) {
        $MimSharedExportRoot = $MimSshStagingRoot
        $MimSharedContextExportPath = $sshRefresh.source_json
        $MimSharedContextExportYamlPath = $sshRefresh.source_yaml
        $MimSharedManifestPath = $sshRefresh.source_manifest
        $mimRefresh.source_handshake_packet = $sshRefresh.source_handshake_packet
    }
    else {
        $mimRefresh.failure_reason = "ssh_pull_failed"
    }
}

if ($RefreshMimContextFromShared -or $RefreshMimContextFromSsh) {
    $envSharedRoot = [string]$env:MIM_SHARED_EXPORT_ROOT
    $sharedCandidates = Get-MimSharedSourceCandidates -ExplicitJsonPath $MimSharedContextExportPath -ExplicitYamlPath $MimSharedContextExportYamlPath -ExplicitManifestPath $MimSharedManifestPath -PreferredRoot $MimSharedExportRoot -EnvRoot $envSharedRoot
    $resolvedShared = Resolve-MimSharedSourceCandidate -Candidates $sharedCandidates
    $mimRefresh.candidate_paths_tried = @($resolvedShared.candidate_paths_tried)
    if ([string]::IsNullOrWhiteSpace([string]$mimRefresh.failure_reason)) {
        $mimRefresh.failure_reason = [string]$resolvedShared.failure_reason
    }

    if ($resolvedShared.resolved -and $null -ne $resolvedShared.candidate) {
        $selected = $resolvedShared.candidate
        $mimRefresh.source_json = [string]$selected.source_json
        $mimRefresh.source_yaml = [string]$selected.source_yaml
        $mimRefresh.source_manifest = [string]$selected.source_manifest
        $mimRefresh.resolved_source_root = [string]$selected.root

        try {
            $mimRefresh.copied_json = [bool](Copy-IfSourceExists -SourcePath ([string]$selected.source_json) -DestinationPath $MimContextExportPath)
            $mimRefresh.copied_yaml = [bool](Copy-IfSourceExists -SourcePath ([string]$selected.source_yaml) -DestinationPath $MimContextExportYamlPath)
            $mimRefresh.copied_manifest = [bool](Copy-IfSourceExists -SourcePath ([string]$selected.source_manifest) -DestinationPath $MimManifestPath)
            if ($mimRefresh.copied_json -and $mimRefresh.copied_yaml) {
                $mimRefresh.failure_reason = ""
            }
            else {
                $mimRefresh.failure_reason = "copy_incomplete"
            }
        }
        catch [System.UnauthorizedAccessException] {
            $mimRefresh.failure_reason = "permission_denied"
        }
        catch {
            $mimRefresh.failure_reason = "copy_failed"
        }
    }
}

$currentBuildStatePath = Join-Path $sharedDirAbs "current_build_state.json"
$objectivesPath = Join-Path $sharedDirAbs "objectives.json"
$contractsPath = Join-Path $sharedDirAbs "contracts.json"
$nextActionsPath = Join-Path $sharedDirAbs "next_actions.json"
$devJournalPath = Join-Path $sharedDirAbs "dev_journal.jsonl"
$latestSummaryPath = Join-Path $sharedDirAbs "latest_summary.md"
$chatgptUpdatePath = Join-Path $sharedDirAbs "chatgpt_update.md"
$chatgptUpdateJsonPath = Join-Path $sharedDirAbs "chatgpt_update.json"
$objectiveAuthorityResetPath = Join-Path $sharedDirAbs "objective_authority_reset.json"
$sharedDevLogPlanPath = Join-Path $sharedDirAbs "shared_development_log_plan.json"
$integrationStatusPath = Join-Path $sharedDirAbs "integration_status.json"
$todStatusPublishReceiptPath = Join-Path $sharedDirAbs "TOD_MIM_ARM_STATUS_UPLOAD_RECEIPT.latest.json"
$todStatusPublishLegacyReceiptPath = Join-Path $sharedDirAbs "TOD_INTEGRATION_STATUS_UPLOAD_RECEIPT.latest.json"
$executionEvidencePath = Join-Path $sharedDirAbs "execution_evidence.json"
$objectiveRoadmapPath = Join-Path $sharedDirAbs "tod_objective_roadmap.json"
$executionReadinessSignalPath = Join-Path $sharedDirAbs "tod_operator_chat_sweep_artifact_smoke.latest.json"
$listenerRequestAbs = Get-LocalPath -PathValue $ListenerRequestPath
$listenerDecisionAbs = Get-LocalPath -PathValue $ListenerDecisionPath
$listenerStageDirAbs = Split-Path -Parent $listenerRequestAbs
$listenerJournalPath = Join-Path $listenerStageDirAbs "TOD_LOOP_JOURNAL.latest.json"
$listenerResultPath = Join-Path $listenerStageDirAbs "TOD_MIM_TASK_RESULT.latest.json"
$defaultSharedStateAbs = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "shared_state"))
$allowAmbientObjectivePromotion = [string]::Equals($sharedDirAbs, $defaultSharedStateAbs, [System.StringComparison]::OrdinalIgnoreCase)

$todScriptAbs = Get-LocalPath -PathValue $TodScriptPath
$todConfigAbs = Get-LocalPath -PathValue $TodConfigPath
$stateAbs = Get-LocalPath -PathValue $StatePath

if (-not (Test-Path -Path $todScriptAbs)) { throw "TOD script not found: $todScriptAbs" }
if (-not (Test-Path -Path $todConfigAbs)) { throw "TOD config not found: $todConfigAbs" }
if (-not (Test-Path -Path $stateAbs)) { throw "TOD state not found: $stateAbs" }

$state = $null
$stateLoadWarning = ""
$maxStateReadBytes = 256MB
$skipFullStateRead = $false

try {
    $stateFileInfo = Get-Item -Path $stateAbs -ErrorAction Stop
    if ($stateFileInfo.Length -gt $maxStateReadBytes) {
        $stateLoadWarning = ("state.json too large for safe full load ({0} MiB > {1} MiB); using objectives ledger fallback" -f [math]::Round(($stateFileInfo.Length / 1MB), 2), [math]::Round(($maxStateReadBytes / 1MB), 2))
        $skipFullStateRead = $true
    }
}
catch {
    $stateLoadWarning = [string]$_.Exception.Message
    $skipFullStateRead = $true
}

if (-not $skipFullStateRead) {
    try {
        $state = Get-JsonFileContent -PathValue $StatePath
    }
    catch {
        $stateLoadWarning = [string]$_.Exception.Message
    }
}

if (-not [string]::IsNullOrWhiteSpace($stateLoadWarning)) {
    Write-Warning ("[TOD-SHARED-SYNC] Unable to load full TOD state; using objectives ledger fallback: {0}" -f $stateLoadWarning)
}

if (-not $state) {
    $state = [pscustomobject]@{}
}
$testSummary = Get-JsonFileIfExists -PathValue $TestSummaryPath
$smokeSummary = Get-JsonFileIfExists -PathValue $SmokeSummaryPath
$qualityGate = Get-JsonFileIfExists -PathValue $QualityGatePath
$trainingStatus = Get-JsonFileIfExists -PathValue $TrainingStatusPath
$approvalReduction = Get-JsonFileIfExists -PathValue $ApprovalReductionPath
$manifest = Get-JsonFileIfExists -PathValue $ManifestPath

$capabilities = Get-TodPayload -TodScript $todScriptAbs -TodConfig $todConfigAbs -ActionName "get-capabilities"
$executionReadinessPayload = Get-TodPayload -TodScript $todScriptAbs -TodConfig $todConfigAbs -ActionName "get-execution-readiness"
$engineeringSignal = Get-TodPayload -TodScript $todScriptAbs -TodConfig $todConfigAbs -ActionName "get-engineering-signal"
$reliabilityPayload = Get-TodPayload -TodScript $todScriptAbs -TodConfig $todConfigAbs -ActionName "get-reliability"
$reliabilityDashboard = Get-TodPayload -TodScript $todScriptAbs -TodConfig $todConfigAbs -ActionName "show-reliability-dashboard"

$branch = Get-GitValue -CommandText "git rev-parse --abbrev-ref HEAD"
$commitSha = Get-GitValue -CommandText "git rev-parse HEAD"
$releaseTag = if (-not [string]::IsNullOrWhiteSpace($ReleaseTagOverride)) { $ReleaseTagOverride } else { Get-GitValue -CommandText "git describe --tags --abbrev=0 2>$null" }
$listenerRequestDoc = Get-JsonFileIfExists -PathValue $listenerRequestAbs
$listenerDecisionDoc = Get-JsonFileIfExists -PathValue $listenerDecisionAbs
$listenerJournalDoc = Get-JsonFileIfExists -PathValue $listenerJournalPath
$listenerResultDoc = Get-JsonFileIfExists -PathValue $listenerResultPath
$listenerObjectiveProgressMap = Get-ListenerObjectiveProgressMap -JournalDoc $listenerJournalDoc
$objectiveAuthorityReset = Get-ObjectiveAuthorityReset -PathValue $objectiveAuthorityResetPath

$objectives = @()
if ($state -and $state.PSObject.Properties["objectives"]) {
    $objectives = @($state.objectives)
}
elseif (Test-Path -Path $objectivesPath) {
    try {
        $fallbackLedger = Get-Content -Path $objectivesPath -Raw | ConvertFrom-Json
        if ($fallbackLedger -and $fallbackLedger.PSObject.Properties["objectives"]) {
            $objectives = @($fallbackLedger.objectives | ForEach-Object {
                    [pscustomobject]@{
                        id = if ($_.PSObject.Properties["objective_id"]) { Normalize-ObjectiveIdText -Value ([string]$_.objective_id) } elseif ($_.PSObject.Properties["id"]) { Normalize-ObjectiveIdText -Value ([string]$_.id) } else { "" }
                        title = if ($_.PSObject.Properties["title"]) { [string]$_.title } else { "" }
                        status = if ($_.PSObject.Properties["status"]) { [string]$_.status } else { "open" }
                    }
                })
        }
    }
    catch {
        $objectives = @()
    }
}

$listenerResultStatus = if ($listenerResultDoc -and $listenerResultDoc.PSObject.Properties["status"]) { ([string]$listenerResultDoc.status).Trim().ToLowerInvariant() } else { "" }
$listenerResultRequestId = if ($listenerResultDoc -and $listenerResultDoc.PSObject.Properties["request_id"]) { [string]$listenerResultDoc.request_id } else { "" }
$listenerRequestTaskId = if ($listenerRequestDoc -and $listenerRequestDoc.PSObject.Properties["task_id"]) { [string]$listenerRequestDoc.task_id } else { "" }
$listenerCompletionObjective = ""
if ($listenerResultDoc -and $listenerResultDoc.PSObject.Properties["objective_id"]) {
    $listenerCompletionObjective = Normalize-ObjectiveIdText -Value ([string]$listenerResultDoc.objective_id)
}
if ([string]::IsNullOrWhiteSpace($listenerCompletionObjective)) {
    $listenerCompletionObjective = Get-ObjectiveIdFromTaskReference -Value $listenerResultRequestId
}
if ([string]::IsNullOrWhiteSpace($listenerCompletionObjective)) {
    $listenerCompletionObjective = Get-ObjectiveIdFromTaskReference -Value $listenerRequestTaskId
}

$listenerCompletionStable =
    (Test-IsTerminalExecutionStatus -Status $listenerResultStatus) -and
    -not [string]::IsNullOrWhiteSpace($listenerCompletionObjective) -and
    (
        [string]::IsNullOrWhiteSpace($listenerRequestTaskId) -or
        [string]::Equals($listenerRequestTaskId, $listenerResultRequestId, [System.StringComparison]::OrdinalIgnoreCase)
    )

if ($listenerCompletionStable) {
    $matchedListenerObjective = $false
    foreach ($objective in $objectives) {
        $normalizedObjectiveId = ""
        if ($objective.PSObject.Properties["id"]) {
            $normalizedObjectiveId = Normalize-ObjectiveIdText -Value ([string]$objective.id)
        }

        if (-not [string]::Equals($normalizedObjectiveId, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }

        if (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedObjectiveId -AuthorityReset $objectiveAuthorityReset) {
            continue
        }

        if ($objective.PSObject.Properties["status"]) {
            $objective.status = "completed"
        }
        else {
            $objective | Add-Member -NotePropertyName status -NotePropertyValue "completed" -Force
        }

        if ($objective.PSObject.Properties["updated_at"]) {
            $objective.updated_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        $matchedListenerObjective = $true
    }

    if (-not $matchedListenerObjective) {
        $objectives += [pscustomobject]@{
            id = $listenerCompletionObjective
            title = ("Objective {0}" -f $listenerCompletionObjective)
            status = "completed"
        }
    }
}

if ([bool]$objectiveAuthorityReset.available -and [bool]$objectiveAuthorityReset.active) {
    $sanitizedObjectives = New-Object System.Collections.Generic.List[object]
    $authorityObjectivePresent = $false

    foreach ($objective in $objectives) {
        $normalizedObjectiveId = if ($objective.PSObject.Properties['id']) { Normalize-ObjectiveIdText -Value ([string]$objective.id) } else { '' }
        if ([string]::IsNullOrWhiteSpace($normalizedObjectiveId)) {
            continue
        }

        if (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedObjectiveId -AuthorityReset $objectiveAuthorityReset) {
            if ($objective.PSObject.Properties['status']) {
                $objective.status = 'invalidated'
            }
            else {
                $objective | Add-Member -NotePropertyName status -NotePropertyValue 'invalidated' -Force
            }
            continue
        }

        if ([string]::Equals($normalizedObjectiveId, [string]$objectiveAuthorityReset.authoritative_current_objective, [System.StringComparison]::OrdinalIgnoreCase)) {
            $authorityObjectivePresent = $true
            if ($objective.PSObject.Properties['status']) {
                $objective.status = 'in_progress'
            }
            else {
                $objective | Add-Member -NotePropertyName status -NotePropertyValue 'in_progress' -Force
            }
        }

        $sanitizedObjectives.Add($objective)
    }

    if (-not $authorityObjectivePresent -and -not [string]::IsNullOrWhiteSpace([string]$objectiveAuthorityReset.authoritative_current_objective)) {
        $sanitizedObjectives.Add([pscustomobject]@{
            id = [string]$objectiveAuthorityReset.authoritative_current_objective
            title = ('Objective {0} authority-reset baseline' -f [string]$objectiveAuthorityReset.authoritative_current_objective)
            status = 'in_progress'
        })
    }

    $objectives = @($sanitizedObjectives.ToArray())
}

$latestCompleted = Get-ObjectiveByStatusOrder -Objectives $objectives -Statuses @("completed", "closed", "done", "reviewed_pass")
$currentInProgress = Get-ObjectiveByStatusOrder -Objectives $objectives -Statuses @("in_progress", "open", "planned")

$latestCompletedObjective = if ($null -ne $latestCompleted) { Normalize-ObjectiveIdText -Value ([string]$latestCompleted.id) } else { "none" }
$currentObjective = if ($null -ne $currentInProgress) { Normalize-ObjectiveIdText -Value ([string]$currentInProgress.id) } else { "none" }
$fallbackCurrentObjective = $currentObjective

if ([bool]$objectiveAuthorityReset.available -and [bool]$objectiveAuthorityReset.active -and -not [string]::IsNullOrWhiteSpace([string]$objectiveAuthorityReset.authoritative_current_objective)) {
    $currentObjective = [string]$objectiveAuthorityReset.authoritative_current_objective
    $fallbackCurrentObjective = $currentObjective
}

$listenerCompletedObjectiveBlocksCurrent = $listenerCompletionStable -and -not [string]::IsNullOrWhiteSpace($listenerCompletionObjective)
if ($listenerCompletedObjectiveBlocksCurrent -and [string]::Equals($currentObjective, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
    $currentObjective = $fallbackCurrentObjective
}

$schemaVersion = if ($manifest -and $manifest.PSObject.Properties["schema_version"]) { [string]$manifest.schema_version } else { "unknown" }
$currentProdTestStatus = [pscustomobject]@{
    tests = [pscustomobject]@{
        available = ($null -ne $testSummary)
        passed_all = if ($testSummary -and $testSummary.PSObject.Properties["passed_all"]) { [bool]$testSummary.passed_all } else { $false }
        passed = if ($testSummary -and $testSummary.PSObject.Properties["passed"]) { [int]$testSummary.passed } else { 0 }
        failed = if ($testSummary -and $testSummary.PSObject.Properties["failed"]) { [int]$testSummary.failed } else { 0 }
        total = if ($testSummary -and $testSummary.PSObject.Properties["total"]) { [int]$testSummary.total } else { 0 }
        generated_at = if ($testSummary -and $testSummary.PSObject.Properties["generated_at"]) { [string]$testSummary.generated_at } else { "" }
    }
    smoke = [pscustomobject]@{
        available = ($null -ne $smokeSummary)
        passed_all = if ($smokeSummary -and $smokeSummary.PSObject.Properties["passed_all"]) { [bool]$smokeSummary.passed_all } else { $false }
        generated_at = if ($smokeSummary -and $smokeSummary.PSObject.Properties["generated_at"]) { [string]$smokeSummary.generated_at } else { "" }
    }
}

$activeCapabilities = @()
if ($capabilities) {
    if ($capabilities.PSObject.Properties["execution"] -and $capabilities.execution.PSObject.Properties["engines"]) {
        foreach ($e in @($capabilities.execution.engines)) {
            $activeCapabilities += "engine:$([string]$e)"
        }
    }
    if ($capabilities.PSObject.Properties["endpoints"]) {
        foreach ($ep in @($capabilities.endpoints)) {
            $activeCapabilities += "endpoint:$([string]$ep)"
        }
    }
}
$activeCapabilities = @($activeCapabilities | Sort-Object -Unique)

$lastRegressionResult = [pscustomobject]@{
    passed_all = if ($testSummary -and $testSummary.PSObject.Properties["passed_all"]) { [bool]$testSummary.passed_all } else { $false }
    passed = if ($testSummary -and $testSummary.PSObject.Properties["passed"]) { [int]$testSummary.passed } else { 0 }
    failed = if ($testSummary -and $testSummary.PSObject.Properties["failed"]) { [int]$testSummary.failed } else { 0 }
    total = if ($testSummary -and $testSummary.PSObject.Properties["total"]) { [int]$testSummary.total } else { 0 }
    generated_at = if ($testSummary -and $testSummary.PSObject.Properties["generated_at"]) { [string]$testSummary.generated_at } else { "" }
}

$lastPromotionResult = [pscustomobject]@{
    available = ($null -ne $qualityGate)
    gate_ok = if ($qualityGate -and $qualityGate.PSObject.Properties["ok"]) { [bool]$qualityGate.ok } else { $false }
    run_success_rate = if ($qualityGate -and $qualityGate.PSObject.Properties["summary"] -and $qualityGate.summary.PSObject.Properties["run_success_rate"]) { [double]$qualityGate.summary.run_success_rate } else { 0.0 }
    deterministic_failure_runs = if ($qualityGate -and $qualityGate.PSObject.Properties["summary"] -and $qualityGate.summary.PSObject.Properties["deterministic_failure_runs"]) { [int]$qualityGate.summary.deterministic_failure_runs } else { 0 }
    transient_lock_failure_runs = if ($qualityGate -and $qualityGate.PSObject.Properties["summary"] -and $qualityGate.summary.PSObject.Properties["transient_lock_failure_runs"]) { [int]$qualityGate.summary.transient_lock_failure_runs } else { 0 }
    generated_at = if ($qualityGate -and $qualityGate.PSObject.Properties["generated_at"]) { [string]$qualityGate.generated_at } else { "" }
}

$approvalBacklog = Get-ApprovalBacklogSnapshot -State $state
$reliabilityAlertRaw = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["current_alert_state"]) { [string]$reliabilityPayload.current_alert_state } else { "" }
$trendForNormalization = if ($engineeringSignal -and $engineeringSignal.PSObject.Properties["trend_direction"]) { [string]$engineeringSignal.trend_direction } else { "unknown" }
$reliabilityAlertNormalized = Resolve-ReliabilityAlertState -RawState $reliabilityAlertRaw -Trend $trendForNormalization -PendingApprovals ([int]$approvalBacklog.total_pending) -RegressionPassed ([bool]$lastRegressionResult.passed_all) -QualityGatePassed ([bool]$lastPromotionResult.gate_ok)

$knownLocalDrift = [pscustomobject]@{
    trend = if ($engineeringSignal -and $engineeringSignal.PSObject.Properties["trend_direction"]) { [string]$engineeringSignal.trend_direction } else { "unknown" }
    reliability_alert_state = $reliabilityAlertNormalized
    reliability_alert_state_raw = if ([string]::IsNullOrWhiteSpace($reliabilityAlertRaw)) { "unknown" } else { $reliabilityAlertRaw }
    pending_approvals = [int]$approvalBacklog.total_pending
}

$todCatchupRoadmap = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-catchup-roadmap-v1"
    anchor = [pscustomobject]@{
        current_objective = $currentObjective
        next_objective = $NextProposedObjective
    }
    objectives = @(
        [pscustomobject]@{ id = "TOD-17"; title = "Execution reliability stabilization"; status = if ($NextProposedObjective -eq "TOD-17") { "next" } else { "planned" } }
        [pscustomobject]@{ id = "TOD-18"; title = "Constraint evaluation integration"; status = "planned" }
        [pscustomobject]@{ id = "TOD-19"; title = "Autonomy boundary awareness"; status = "planned" }
        [pscustomobject]@{ id = "TOD-20"; title = "Cross-domain execution coordination"; status = "planned" }
        [pscustomobject]@{ id = "TOD-21"; title = "Perception event handling"; status = "planned" }
        [pscustomobject]@{ id = "TOD-22"; title = "Inquiry-driven execution pause/resume"; status = "planned" }
    )
}
Write-Utf8NoBomJson -Path $objectiveRoadmapPath -Payload $todCatchupRoadmap -Depth 12

$mimSchemaVersion = Get-MimSchemaVersionFromContextExport -PathValue $MimContextExportPath
if ([string]::IsNullOrWhiteSpace($mimSchemaVersion)) {
    $mimSchemaVersion = Get-MimSchemaVersionFromContextExport -PathValue $MimManifestPath
}
$mimManifestDoc = Get-JsonFileIfExists -PathValue $MimManifestPath
$mimContextDoc = Get-JsonFileIfExists -PathValue $MimContextExportPath

$mimContractVersion = ""
if ($mimManifestDoc -and $mimManifestDoc.PSObject.Properties["contract_version"] -and -not [string]::IsNullOrWhiteSpace([string]$mimManifestDoc.contract_version)) {
    $mimContractVersion = [string]$mimManifestDoc.contract_version
}
elseif ($mimManifestDoc -and $mimManifestDoc.PSObject.Properties["manifest"] -and $mimManifestDoc.manifest -and $mimManifestDoc.manifest.PSObject.Properties["contract_version"] -and -not [string]::IsNullOrWhiteSpace([string]$mimManifestDoc.manifest.contract_version)) {
    $mimContractVersion = [string]$mimManifestDoc.manifest.contract_version
}
elseif ($mimContextDoc -and $mimContextDoc.PSObject.Properties["contract_version"] -and -not [string]::IsNullOrWhiteSpace([string]$mimContextDoc.contract_version)) {
    $mimContractVersion = [string]$mimContextDoc.contract_version
}

$mimStatus = Get-MimStatusSnapshot -PathValue $MimContextExportPath -StaleAfterHours $MimStatusStaleAfterHours
$mimSharedCandidateRoots = @(
    [string]$mimRefresh.resolved_source_root,
    [string]$MimSharedExportRoot,
    [string]$env:MIM_SHARED_EXPORT_ROOT
) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique

$handshakeCandidatePaths = @()
if (-not [string]::IsNullOrWhiteSpace([string]$mimRefresh.source_handshake_packet)) {
    $handshakeCandidatePaths += [string]$mimRefresh.source_handshake_packet
}
foreach ($candidateRoot in @($mimSharedCandidateRoots)) {
    $handshakeCandidatePaths += (Join-Path ([string]$candidateRoot) "MIM_TOD_HANDSHAKE_PACKET.latest.json")
}

$resolvedHandshakePath = ""
foreach ($candidate in @($handshakeCandidatePaths | Select-Object -Unique)) {
    if ([string]::IsNullOrWhiteSpace([string]$candidate)) { continue }
    $candidateAbs = Get-LocalPath -PathValue ([string]$candidate)
    if (Test-Path -Path $candidateAbs -PathType Leaf) {
        $resolvedHandshakePath = [string]$candidate
        break
    }
}

$mimHandshake = Get-MimHandshakePacketSnapshot -PathValue $resolvedHandshakePath
if ([string]::IsNullOrWhiteSpace($mimSchemaVersion) -and [bool]$mimHandshake.available -and -not [string]::IsNullOrWhiteSpace([string]$mimHandshake.schema_version)) {
    $mimSchemaVersion = [string]$mimHandshake.schema_version
}

$liveTaskRequestCandidatePaths = @()
if (-not [string]::IsNullOrWhiteSpace([string]$ListenerRequestPath)) {
    $liveTaskRequestCandidatePaths += [string]$ListenerRequestPath
}
foreach ($candidateRoot in @($mimSharedCandidateRoots)) {
    $liveTaskRequestCandidatePaths += (Join-Path ([string]$candidateRoot) 'MIM_TOD_TASK_REQUEST.latest.json')
}

$liveTaskRequest = Get-PreferredLiveTaskRequestSnapshot -CandidatePaths $liveTaskRequestCandidatePaths
$listenerDecision = Get-ListenerDecisionSnapshot -PathValue $ListenerDecisionPath -AuthorityReset $objectiveAuthorityReset

# If legacy context export is stale but live task telemetry is fresh, treat live task as current status freshness.
if ([bool]$mimStatus.is_stale -and [bool]$liveTaskRequest.available -and -not [string]::IsNullOrWhiteSpace([string]$liveTaskRequest.generated_at)) {
    $liveGeneratedUtc = Convert-ToUtcDateOrNull -Value ([string]$liveTaskRequest.generated_at)
    if ($null -ne $liveGeneratedUtc) {
        $liveAgeHours = [math]::Round(((Get-Date).ToUniversalTime() - $liveGeneratedUtc).TotalHours, 2)
        if ($liveAgeHours -le $MimStatusStaleAfterHours) {
            $mimStatus.generated_at = [string]$liveTaskRequest.generated_at
            $mimStatus.age_hours = $liveAgeHours
            $mimStatus.is_stale = $false
            $mimStatus.source_path = [string]$liveTaskRequest.source_path
            if ([string]::IsNullOrWhiteSpace([string]$mimStatus.objective_active)) {
                $mimStatus.objective_active = [string]$liveTaskRequest.normalized_objective_id
            }
            if ([string]::IsNullOrWhiteSpace([string]$mimStatus.phase)) {
                $mimStatus.phase = "live_task_active"
            }
            if (-not $mimStatus.PSObject.Properties["freshness_override"]) {
                $mimStatus | Add-Member -NotePropertyName freshness_override -NotePropertyValue "live_task_request" -Force
            }
        }
    }
}

$mimObjectiveForAlignment = [string]$mimStatus.objective_active
$mimObjectiveSource = "context_export"
if ([bool]$mimHandshake.available -and -not [string]::IsNullOrWhiteSpace([string]$mimHandshake.objective_active)) {
    $mimObjectiveForAlignment = [string]$mimHandshake.objective_active
    $mimObjectiveSource = "handshake_packet"
}

$normalizedCurrentObjective = Normalize-ObjectiveIdText -Value $currentObjective
$normalizedMimObjectiveForAlignment = Normalize-ObjectiveIdText -Value $mimObjectiveForAlignment
$normalizedHandshakeNextObjective = if ([bool]$mimHandshake.available) { Normalize-ObjectiveIdText -Value ([string]$mimHandshake.current_next_objective) } else { "" }
$normalizedLiveRequestObjective = if ($liveTaskRequest) { [string]$liveTaskRequest.normalized_objective_id } else { "" }
$liveTaskRequestId = if ($liveTaskRequest -and $liveTaskRequest.PSObject.Properties['request_id']) { [string]$liveTaskRequest.request_id } else { '' }
$listenerDecisionRequestId = if ($listenerDecision -and $listenerDecision.PSObject.Properties['request_id']) { [string]$listenerDecision.request_id } else { '' }
$normalizedListenerDecisionObjective = if ($listenerDecision -and $listenerDecision.PSObject.Properties['normalized_objective_id']) { Normalize-ObjectiveIdText -Value ([string]$listenerDecision.normalized_objective_id) } elseif ($listenerDecision -and $listenerDecision.PSObject.Properties['objective_id']) { Normalize-ObjectiveIdText -Value ([string]$listenerDecision.objective_id) } else { '' }
$listenerDecisionExecutionState = if ($listenerDecision -and $listenerDecision.PSObject.Properties['execution_state']) { ([string]$listenerDecision.execution_state).Trim().ToLowerInvariant() } else { '' }
$listenerDecisionOutcome = if ($listenerDecision -and $listenerDecision.PSObject.Properties['decision_outcome']) { ([string]$listenerDecision.decision_outcome).Trim().ToLowerInvariant() } else { '' }
$liveRequestResolvedByListenerCompletion =
    $listenerCompletionStable -and
    -not [string]::IsNullOrWhiteSpace($liveTaskRequestId) -and
    [string]::Equals($liveTaskRequestId, $listenerResultRequestId, [System.StringComparison]::OrdinalIgnoreCase)
$authorityObjectiveForAlignment = if ([bool]$objectiveAuthorityReset.available -and [bool]$objectiveAuthorityReset.active -and -not [string]::IsNullOrWhiteSpace([string]$objectiveAuthorityReset.authoritative_current_objective)) {
    Normalize-ObjectiveIdText -Value ([string]$objectiveAuthorityReset.authoritative_current_objective)
} else { "" }

if (-not [string]::IsNullOrWhiteSpace($authorityObjectiveForAlignment)) {
    $mimObjectiveInvalidatedByAuthority = [string]::IsNullOrWhiteSpace($normalizedMimObjectiveForAlignment) -or (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedMimObjectiveForAlignment -AuthorityReset $objectiveAuthorityReset)
    if ($mimObjectiveInvalidatedByAuthority) {
        $mimObjectiveForAlignment = $authorityObjectiveForAlignment
        $normalizedMimObjectiveForAlignment = $authorityObjectiveForAlignment
        $mimObjectiveSource = "objective_authority_reset"
    }

    if (-not [string]::IsNullOrWhiteSpace($normalizedHandshakeNextObjective) -and (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedHandshakeNextObjective -AuthorityReset $objectiveAuthorityReset)) {
        $normalizedHandshakeNextObjective = $authorityObjectiveForAlignment
    }
}

if ($listenerCompletedObjectiveBlocksCurrent) {
    $retainCompletedObjectiveAsActive = $false
    if ([string]::Equals($normalizedLiveRequestObjective, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
        $retainCompletedObjectiveAsActive = -not $liveRequestResolvedByListenerCompletion
    }
    elseif ([string]::Equals($normalizedHandshakeNextObjective, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
        $retainCompletedObjectiveAsActive = $true
    }
    elseif ([string]::Equals($normalizedMimObjectiveForAlignment, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
        $retainCompletedObjectiveAsActive = $true
    }

    if (-not $retainCompletedObjectiveAsActive) {
        if ([string]::Equals($normalizedLiveRequestObjective, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
            $normalizedLiveRequestObjective = ""
            if ($liveTaskRequest) {
                $liveTaskRequest.promotion_applied = $false
                $liveTaskRequest.promotion_reason = 'cleared_after_matched_terminal_result'
            }
        }
        if ([string]::Equals($normalizedMimObjectiveForAlignment, $listenerCompletionObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
            $normalizedMimObjectiveForAlignment = ""
            $mimObjectiveForAlignment = ""
        }
    }
}

if ($allowAmbientObjectivePromotion -and -not [string]::IsNullOrWhiteSpace($normalizedLiveRequestObjective) -and $normalizedLiveRequestObjective -ne "none") {
    $promoteFromLiveRequest = $false
    $promotionReason = ""

    if (-not [string]::IsNullOrWhiteSpace($normalizedHandshakeNextObjective) -and [string]::Equals($normalizedLiveRequestObjective, $normalizedHandshakeNextObjective, [System.StringComparison]::OrdinalIgnoreCase)) {
        $promoteFromLiveRequest = $true
        $promotionReason = "request_matches_handshake_next_objective"
    }
    elseif (-not [string]::IsNullOrWhiteSpace($normalizedMimObjectiveForAlignment)) {
        $liveRequestNumber = Get-IdNumber -Value $normalizedLiveRequestObjective
        $canonicalObjectiveNumber = Get-IdNumber -Value $normalizedMimObjectiveForAlignment
        if ($liveRequestNumber -ge 0 -and $canonicalObjectiveNumber -ge 0 -and $liveRequestNumber -gt $canonicalObjectiveNumber) {
            $promoteFromLiveRequest = $true
            $promotionReason = "request_objective_ahead_of_canonical_export"
        }
    }

    if (-not $promoteFromLiveRequest) {
        $listenerConfirmsLiveRequest =
            -not [string]::IsNullOrWhiteSpace($normalizedListenerDecisionObjective) -and
            [string]::Equals($normalizedLiveRequestObjective, $normalizedListenerDecisionObjective, [System.StringComparison]::OrdinalIgnoreCase) -and
            -not [string]::IsNullOrWhiteSpace($liveTaskRequestId) -and
            [string]::Equals($liveTaskRequestId, $listenerDecisionRequestId, [System.StringComparison]::OrdinalIgnoreCase)
        $listenerStateAllowsPromotion = @('ready_to_execute', 'waiting_on_dependency', 'execute_now') -contains $listenerDecisionExecutionState
        $listenerOutcomeAllowsPromotion = @('execute', 'acknowledge_and_wait_on_dependency') -contains $listenerDecisionOutcome
        if ($listenerConfirmsLiveRequest -and ($listenerStateAllowsPromotion -or $listenerOutcomeAllowsPromotion)) {
            $promoteFromLiveRequest = $true
            $promotionReason = "request_confirmed_by_listener_decision"
        }
    }

    if ($promoteFromLiveRequest -and (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedLiveRequestObjective -AuthorityReset $objectiveAuthorityReset)) {
        $promoteFromLiveRequest = $false
    }

    if ($promoteFromLiveRequest) {
        $currentObjective = $normalizedLiveRequestObjective
        $normalizedCurrentObjective = $normalizedLiveRequestObjective
        $mimObjectiveForAlignment = $normalizedLiveRequestObjective
        $normalizedMimObjectiveForAlignment = $normalizedLiveRequestObjective
        $mimObjectiveSource = "live_task_request"
        $liveTaskRequest.promotion_applied = $true
        $liveTaskRequest.promotion_reason = $promotionReason
    }
}

if ($allowAmbientObjectivePromotion -and -not [string]::IsNullOrWhiteSpace($normalizedMimObjectiveForAlignment) -and $normalizedMimObjectiveForAlignment -ne "none") {
    $canonicalObjectiveAllowed = -not (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $normalizedMimObjectiveForAlignment -AuthorityReset $objectiveAuthorityReset)
    if ($canonicalObjectiveAllowed -and ([string]::IsNullOrWhiteSpace($normalizedCurrentObjective) -or $normalizedCurrentObjective -eq "none" -or $normalizedCurrentObjective -ne $normalizedMimObjectiveForAlignment)) {
        # Canonical live MIM objective should win over stale local pins/open-objective fallbacks.
        $currentObjective = $normalizedMimObjectiveForAlignment
        $normalizedCurrentObjective = $normalizedMimObjectiveForAlignment
    }
}

$listenerDecisionReasonCode = if ($listenerDecision -and $listenerDecision.PSObject.Properties['reason_code']) { ([string]$listenerDecision.reason_code).Trim().ToLowerInvariant() } else { '' }
$listenerCanonicalObjective = @(
    $normalizedMimObjectiveForAlignment,
    $normalizedLiveRequestObjective,
    $normalizedHandshakeNextObjective
) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1
if (
    [string]::Equals($listenerDecisionReasonCode, 'objective_mismatch', [System.StringComparison]::OrdinalIgnoreCase) -and
    -not [string]::IsNullOrWhiteSpace($listenerCanonicalObjective) -and
    [string]::Equals($normalizedListenerDecisionObjective, $listenerCanonicalObjective, [System.StringComparison]::OrdinalIgnoreCase) -and
    -not [string]::Equals($normalizedCurrentObjective, $listenerCanonicalObjective, [System.StringComparison]::OrdinalIgnoreCase) -and
    -not (Test-ObjectiveInvalidatedByAuthority -ObjectiveId $listenerCanonicalObjective -AuthorityReset $objectiveAuthorityReset)
) {
    $currentObjective = $listenerCanonicalObjective
    $normalizedCurrentObjective = $listenerCanonicalObjective
    if ([string]::IsNullOrWhiteSpace($normalizedMimObjectiveForAlignment)) {
        $mimObjectiveForAlignment = $listenerCanonicalObjective
        $normalizedMimObjectiveForAlignment = $listenerCanonicalObjective
        $mimObjectiveSource = 'listener_objective_mismatch_recovery'
    }
}

$allCopied = [bool]$mimRefresh.copied_json -and [bool]$mimRefresh.copied_yaml
if ($RefreshMimContextFromShared -and $allCopied -and [bool]$mimStatus.is_stale) {
    $mimRefresh.failure_reason = "stale_export"
}
$objectiveAlignment = Get-ObjectiveAlignment -TodObjective $currentObjective -MimObjectiveActive $mimObjectiveForAlignment -MimObjectiveSource $mimObjectiveSource
$todContractVersion = if ($manifest -and $manifest.PSObject.Properties["contract_version"] -and -not [string]::IsNullOrWhiteSpace([string]$manifest.contract_version)) {
    [string]$manifest.contract_version
}
elseif ($manifest -and $manifest.PSObject.Properties["schema_version"] -and -not [string]::IsNullOrWhiteSpace([string]$manifest.schema_version)) {
    [string]$manifest.schema_version
}
else {
    ""
}

$schemaCompatible = (-not [string]::IsNullOrWhiteSpace($mimSchemaVersion)) -and ($manifest -and $manifest.PSObject.Properties["schema_version"] -and -not [string]::IsNullOrWhiteSpace([string]$manifest.schema_version)) -and ([string]$mimSchemaVersion -eq [string]$manifest.schema_version)
$contractCompatible = (-not [string]::IsNullOrWhiteSpace($mimContractVersion)) -and (-not [string]::IsNullOrWhiteSpace($todContractVersion)) -and ([string]$mimContractVersion -eq [string]$todContractVersion)
$compatibility = [bool]($contractCompatible -or $schemaCompatible)

$compatibilityReason = if ($contractCompatible) {
    "contract_version_match"
}
elseif ($schemaCompatible) {
    "schema_version_match"
}
else {
    "no_contract_or_schema_match"
}

$todStatusPublish = [pscustomobject]@{
    attempted = [bool]$PublishTodStatusToMimArm
    enabled = [bool]$PublishTodStatusToMimArm
    status = if ([bool]$PublishTodStatusToMimArm) { "pending" } else { "not_requested" }
    local_status_path = $integrationStatusPath
    local_status_sha256 = ""
    receipt_path = $todStatusPublishReceiptPath
    ssh_host = ""
    ssh_resolved_host = ""
    ssh_user = ""
    ssh_port = 0
    remote_root = ""
    remote_primary_path = ""
    remote_alias_path = ""
    remote_summary_path = ""
    mim_mirror_root = ""
    mim_mirror_primary_path = ""
    mim_mirror_alias_path = ""
    mim_mirror_status = if ([bool]$PublishTodStatusToMimArm) { "pending" } else { "not_requested" }
    access_mode = "full"
    remote_access_status = if ([bool]$PublishTodStatusToMimArm) { "pending" } else { "not_requested" }
    remote_consumer_script_path = ""
    consumer_status = if ([bool]$PublishTodStatusToMimArm) { "pending" } else { "not_requested" }
    uploaded_at = ""
    error = ""
}

$bridgeCanonicalEvidence = Get-BridgeCanonicalEvidence -MimRefresh $mimRefresh -MimHandshake $mimHandshake -LiveTaskRequest $liveTaskRequest -ObjectiveAlignment $objectiveAlignment -TodStatusPublish $todStatusPublish
$bridgeOperatorGuidance = Get-BridgeOperatorGuidance -BridgeCanonicalEvidence $bridgeCanonicalEvidence

$integrationStatus = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-integration-status-v1"
    mim_schema = if ([string]::IsNullOrWhiteSpace($mimSchemaVersion)) { "unknown" } else { $mimSchemaVersion }
    tod_contract = if ([string]::IsNullOrWhiteSpace($todContractVersion)) { "unknown" } else { $todContractVersion }
    mim_contract = if ([string]::IsNullOrWhiteSpace($mimContractVersion)) { "unknown" } else { $mimContractVersion }
    compatible = [bool]$compatibility
    compatibility_reason = $compatibilityReason
    mim_status = $mimStatus
    mim_handshake = $mimHandshake
    live_task_request = $liveTaskRequest
    listener_decision = $listenerDecision
    mim_refresh = $mimRefresh
    objective_alignment = $objectiveAlignment
    bridge_canonical_evidence = $bridgeCanonicalEvidence
    bridge_operator_guidance = @($bridgeOperatorGuidance)
    training_status = $trainingStatus
    tod_status_publish = $todStatusPublish
    objective_authority_reset = $objectiveAuthorityReset
}
Write-Utf8NoBomJson -Path $integrationStatusPath -Payload $integrationStatus -Depth 8

if ($PublishTodStatusToMimArm) {
    $todStatusPublish = Publish-TodStatusToMimArm -LocalStatusPath $integrationStatusPath -LocalTrainingStatusPath $TrainingStatusPath -ReceiptPath $todStatusPublishReceiptPath -LegacyReceiptPath $todStatusPublishLegacyReceiptPath -RemoteHost $MimArmSshHost -RemoteUser $MimArmSshUser -RemotePort $MimArmSshPort -RemotePassword $MimArmSshPassword -RemoteRoot $MimArmSshRemoteRoot -RemoteToolsRoot $MimArmSshToolsRoot -ConsumerTemplatePath $MimArmConsumerTemplatePath -DotEnvPath $DotEnvPath
    $bridgeCanonicalEvidence = Get-BridgeCanonicalEvidence -MimRefresh $mimRefresh -MimHandshake $mimHandshake -LiveTaskRequest $liveTaskRequest -ObjectiveAlignment $objectiveAlignment -TodStatusPublish $todStatusPublish
    $bridgeOperatorGuidance = Get-BridgeOperatorGuidance -BridgeCanonicalEvidence $bridgeCanonicalEvidence
    $integrationStatus.bridge_canonical_evidence = $bridgeCanonicalEvidence
    $integrationStatus.bridge_operator_guidance = @($bridgeOperatorGuidance)
    $integrationStatus.tod_status_publish = $todStatusPublish
    Write-Utf8NoBomJson -Path $integrationStatusPath -Payload $integrationStatus -Depth 8
}

$retryTrendRows = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["retry_trend"] -and $null -ne $reliabilityPayload.retry_trend) { @($reliabilityPayload.retry_trend) } else { @() }
$executionEvidence = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-execution-evidence-v1"
    execution_readiness = [pscustomobject]@{
        signal_name = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["signal_name"]) { [string]$executionReadinessPayload.signal_name } else { "execution-readiness" }
        capability_name = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["capability_name"]) { [string]$executionReadinessPayload.capability_name } else { "TOD Sweep Certification Capability" }
        artifact_path = [string]$executionReadinessSignalPath
        status = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["status"]) { [string]$executionReadinessPayload.readiness.status } else { "unknown" }
        valid = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["valid"]) { [bool]$executionReadinessPayload.readiness.valid } else { $false }
        execution_allowed = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["execution_allowed"]) { [bool]$executionReadinessPayload.readiness.execution_allowed } else { $false }
        authoritative = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["authoritative"]) { [bool]$executionReadinessPayload.readiness.authoritative } else { $true }
        reason = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["reason"]) { [string]$executionReadinessPayload.readiness.reason } else { "unknown" }
        detail = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["detail"]) { [string]$executionReadinessPayload.readiness.detail } else { "" }
        freshness_state = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["freshness_state"]) { [string]$executionReadinessPayload.readiness.freshness_state } else { "unknown" }
        artifact_generated_at = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["artifact_generated_at"]) { [string]$executionReadinessPayload.readiness.artifact_generated_at } else { "" }
        artifact_age_minutes = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["artifact_age_minutes"]) { $executionReadinessPayload.readiness.artifact_age_minutes } else { $null }
        execution_max_artifact_age_minutes = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["execution_max_artifact_age_minutes"]) { [int]$executionReadinessPayload.readiness.execution_max_artifact_age_minutes } else { $null }
        display_max_artifact_age_minutes = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["display_max_artifact_age_minutes"]) { [int]$executionReadinessPayload.readiness.display_max_artifact_age_minutes } else { $null }
        authoritative_surface = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["authoritative_surface"]) { [string]$executionReadinessPayload.authoritative_surface } else { "direct_artifact_smoke" }
        non_authoritative_surfaces = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["non_authoritative_surfaces"]) { @($executionReadinessPayload.non_authoritative_surfaces) } else { @("wrapper_pester_output") }
        block_actions = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["policy"] -and $executionReadinessPayload.policy.PSObject.Properties["block_actions"]) { @($executionReadinessPayload.policy.block_actions) } else { @() }
        degrade_actions = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["policy"] -and $executionReadinessPayload.policy.PSObject.Properties["degrade_actions"]) { @($executionReadinessPayload.policy.degrade_actions) } else { @() }
        block_states = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["policy"] -and $executionReadinessPayload.policy.PSObject.Properties["block_states"]) { @($executionReadinessPayload.policy.block_states) } else { @() }
        degrade_states = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["policy"] -and $executionReadinessPayload.policy.PSObject.Properties["degrade_states"]) { @($executionReadinessPayload.policy.degrade_states) } else { @() }
        history = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["history"]) { $executionReadinessPayload.history } else { $null }
    }
    execution_reliability = [pscustomobject]@{
        current_alert_state = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["current_alert_state"]) { [string]$reliabilityPayload.current_alert_state } else { "unknown" }
        reliability_alert_reasons = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["reliability_alert_reasons"]) { @($reliabilityPayload.reliability_alert_reasons) } else { @() }
        engine_reliability_score = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["engine_reliability_score"]) { $reliabilityPayload.engine_reliability_score } else { $null }
    }
    constraint_evaluation_outcomes = [pscustomobject]@{
        drift_warnings = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["drift_warnings"]) { @($reliabilityPayload.drift_warnings) } else { @() }
        guardrail_trend = if ($reliabilityPayload -and $reliabilityPayload.PSObject.Properties["guardrail_trend"]) { $reliabilityPayload.guardrail_trend } else { $null }
    }
    retry_fallback_metrics = @($retryTrendRows | ForEach-Object {
            [pscustomobject]@{
                engine = if ($_.PSObject.Properties["engine"]) { [string]$_.engine } else { "unknown" }
                recent_retry_rate = if ($_.PSObject.Properties["recent_retry_rate"]) { [double]$_.recent_retry_rate } else { 0.0 }
                baseline_retry_rate = if ($_.PSObject.Properties["baseline_retry_rate"]) { [double]$_.baseline_retry_rate } else { 0.0 }
                recent_fallback_rate = if ($_.PSObject.Properties["recent_fallback_rate"]) { [double]$_.recent_fallback_rate } else { 0.0 }
                baseline_fallback_rate = if ($_.PSObject.Properties["baseline_fallback_rate"]) { [double]$_.baseline_fallback_rate } else { 0.0 }
            }
        })
    performance_deltas = @($retryTrendRows | ForEach-Object {
            $recentScore = if ($_.PSObject.Properties["recent_engine_score"]) { [double]$_.recent_engine_score } else { 0.0 }
            $baselineScore = if ($_.PSObject.Properties["baseline_engine_score"]) { [double]$_.baseline_engine_score } else { 0.0 }
            [pscustomobject]@{
                engine = if ($_.PSObject.Properties["engine"]) { [string]$_.engine } else { "unknown" }
                engine_score_recent = $recentScore
                engine_score_baseline = $baselineScore
                engine_score_delta = ($recentScore - $baselineScore)
            }
        })
}
Write-Utf8NoBomJson -Path $executionEvidencePath -Payload $executionEvidence -Depth 20

$currentBuildState = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    machine = $env:COMPUTERNAME
    repo = [pscustomobject]@{
        name = "TOD"
        root = $repoRoot
        branch = $branch
        latest_commit_sha = $commitSha
    }
    latest_objective_completed = $latestCompletedObjective
    current_schema_version = $schemaVersion
    current_release_tag = $releaseTag
    current_prod_test_status = $currentProdTestStatus
    execution_readiness = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"]) { $executionReadinessPayload.readiness } else { [pscustomobject]@{ status = "unknown"; valid = $false } }
    execution_readiness_history = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["history"]) { $executionReadinessPayload.history } else { $null }
    objective_authority_reset = $objectiveAuthorityReset
    active_capabilities = @($activeCapabilities)
    known_local_drift = $knownLocalDrift
    last_regression_result = $lastRegressionResult
    last_promotion_result = $lastPromotionResult
}

Write-Utf8NoBomJson -Path $currentBuildStatePath -Payload $currentBuildState -Depth 20

$existingObjectives = @()
if (Test-Path -Path $objectivesPath) {
    try {
        $existingObjDoc = Get-Content -Path $objectivesPath -Raw | ConvertFrom-Json
        if ($existingObjDoc -and $existingObjDoc.PSObject.Properties["objectives"]) {
            $existingObjectives = @($existingObjDoc.objectives)
        }
    }
    catch {
        $existingObjectives = @()
    }
}

$existingMap = @{}
foreach ($eo in $existingObjectives) {
    if ($eo.PSObject.Properties["objective_id"]) {
        $existingMap[[string]$eo.objective_id] = $eo
    }
}

$objectiveRecords = @()
foreach ($obj in $objectives) {
    $oid = [string]$obj.id
    $prior = if ($existingMap.ContainsKey($oid)) { $existingMap[$oid] } else { $null }

    $priorDocsRaw = $null
    if ($prior -and $prior.PSObject.Properties["docs_paths"]) {
        $priorDocsRaw = $prior.docs_paths
    }
    $normalizedDocsPaths = @(Convert-ToStringList -Value $priorDocsRaw)

    $priorCapabilitiesRaw = $null
    if ($prior -and $prior.PSObject.Properties["notable_capabilities_added"]) {
        $priorCapabilitiesRaw = $prior.notable_capabilities_added
    }
    $normalizedNotableCapabilities = @(Convert-ToStringList -Value $priorCapabilitiesRaw)

    $progressSnapshot = $null
    if ($listenerObjectiveProgressMap.ContainsKey($oid)) {
        $progressSnapshot = $listenerObjectiveProgressMap[$oid]
    }
    elseif ($prior -and $prior.PSObject.Properties['progress_snapshot'] -and $prior.progress_snapshot) {
        $progressSnapshot = $prior.progress_snapshot
    }

    if ($null -eq $progressSnapshot) {
        $progressSnapshot = [pscustomobject]@{
            available = $false
            task_count = 0
            completed_equivalent = 0.0
            percent = 0
            by_status = [pscustomobject]@{
                completed = 0
                failed = 0
                in_progress = 0
            }
            source = ''
            generated_at = ''
            last_request_id = ''
            last_execution_status = ''
            last_timestamp = ''
        }
    }

    $objectiveRecords += [pscustomobject]@{
        objective_number = Get-IdNumber -Value $oid
        objective_id = $oid
        title = if ($obj.PSObject.Properties["title"]) { [string]$obj.title } else { "" }
        status = if ($obj.PSObject.Properties["status"]) { [string]$obj.status } else { "unknown" }
        focused_gate_result = if ($qualityGate -and $qualityGate.PSObject.Properties["ok"]) { if ([bool]$qualityGate.ok) { "pass" } else { "attention" } } else { "unknown" }
        full_regression_result = if ($testSummary -and $testSummary.PSObject.Properties["passed_all"]) { if ([bool]$testSummary.passed_all) { "pass" } else { "attention" } } else { "unknown" }
        promoted = if ($prior -and $prior.PSObject.Properties["promoted"]) { [bool]$prior.promoted } else { $false }
        prod_verified = if ($prior -and $prior.PSObject.Properties["prod_verified"]) { [bool]$prior.prod_verified } else { $false }
        docs_paths = @($normalizedDocsPaths)
        notable_capabilities_added = @($normalizedNotableCapabilities)
        machine_repo_primarily_affected = if ($prior -and $prior.PSObject.Properties["machine_repo_primarily_affected"]) { [string]$prior.machine_repo_primarily_affected } else { ("{0}:TOD" -f $env:COMPUTERNAME) }
        progress_snapshot = $progressSnapshot
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
}

$objectiveLedger = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-shared-state-sync-v1"
    objective_count = @($objectiveRecords).Count
    objectives = @($objectiveRecords | Sort-Object objective_number)
}
Write-Utf8NoBomJson -Path $objectivesPath -Payload $objectiveLedger -Depth 20

$contracts = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-shared-state-sync-v1"
    manifest_schema_versions = [pscustomobject]@{
        sample_manifest_contract_version = if ($manifest -and $manifest.PSObject.Properties["contract_version"]) { [string]$manifest.contract_version } else { "unknown" }
        sample_manifest_schema_version = if ($manifest -and $manifest.PSObject.Properties["schema_version"]) { [string]$manifest.schema_version } else { "unknown" }
        tod_mim_shared_contract_doc = "v1"
        execution_feedback_contract_doc = "v1"
        shared_development_log_contract_doc = "v1"
    }
    shared_development_log = [pscustomobject]@{
        contract_doc = "docs/tod-shared-development-log-contract-v1.md"
        plan_file = "shared_state/shared_development_log_plan.json"
    }
    capability_registry = @(
        [pscustomobject]@{
            name = "TOD Sweep Certification Capability"
            signal_name = "execution-readiness"
            source_artifact = "shared_state/tod_operator_chat_sweep_artifact_smoke.latest.json"
            command = ".\\scripts\\TOD.ps1 -Action get-execution-readiness"
            authoritative_surface = "direct_artifact_smoke"
            non_authoritative_surfaces = @("wrapper_pester_output")
            objective_anchor = "Objective 87"
            policy_target = "Objective 90"
            purpose = @("gating task execution", "validating environment stability", "triggering fallback or degrade mode")
            status = if ($executionReadinessPayload -and $executionReadinessPayload.PSObject.Properties["readiness"] -and $executionReadinessPayload.readiness.PSObject.Properties["status"]) { [string]$executionReadinessPayload.readiness.status } else { "unknown" }
        }
    )
    exposed_capabilities = @($activeCapabilities)
    important_endpoints = if ($capabilities -and $capabilities.PSObject.Properties["endpoints"]) { @($capabilities.endpoints) } else { @() }
    shared_models = @("Objective", "Task", "Result", "Review", "JournalEntry", "Manifest")
    interoperability_expectations = @(
        "TOD plans and executes within policy boundaries.",
        "MIM persists shared operational memory and lifecycle feedback.",
        "Execution feedback uses execution_id correlation and terminal status mapping.",
        "Shared-state files in shared_state are canonical sync layer for parallel sessions."
    )
}
Write-Utf8NoBomJson -Path $contractsPath -Payload $contracts -Depth 20

$pendingInboxCount = 0
$contextInbox = Get-LocalPath -PathValue $ContextSyncInboxPath
if (Test-Path -Path $contextInbox) {
    $pendingInboxCount = @((Get-ChildItem -Path $contextInbox -File -Filter "*.json")).Count
}

$blockers = @()
if ($knownLocalDrift.pending_approvals -gt 0) {
    $blockers += ("pending approvals ({0})" -f $knownLocalDrift.pending_approvals)
}
if ($pendingInboxCount -gt 0) {
    $blockers += ("context updates pending ingest ({0})" -f $pendingInboxCount)
}
if ($mimStatus.is_stale) {
    $mimAgeForBlocker = "unknown"
    if ($null -ne $mimStatus.age_hours) {
        $mimAgeForBlocker = [string]$mimStatus.age_hours
    }
    $blockers += ("mim status stale ({0}h > {1}h)" -f $mimAgeForBlocker, [string]$mimStatus.stale_after_hours)
}
if ([string]$objectiveAlignment.status -eq "mismatch" -and -not ([bool]$listenerDecision.available -and [string]::Equals([string]$listenerDecision.suppressed_reason, 'inactive_authority_reset', [System.StringComparison]::OrdinalIgnoreCase))) {
    $blockers += ("objective mismatch tod={0} mim={1}" -f [string]$objectiveAlignment.tod_current_objective, [string]$objectiveAlignment.mim_objective_active)
}
if ([bool]$listenerDecision.available) {
    $decisionOutcome = [string]$listenerDecision.decision_outcome
    $decisionReason = if (-not [string]::IsNullOrWhiteSpace([string]$listenerDecision.reason_code)) { [string]$listenerDecision.reason_code } else { [string]$listenerDecision.blocker_classification }
    switch ($decisionOutcome) {
        "acknowledge_and_wait_on_dependency" {
            $blockers += ("listener waiting on dependency ({0})" -f $(if ([string]::IsNullOrWhiteSpace($decisionReason)) { 'unspecified' } else { $decisionReason }))
        }
        "escalate_hard_boundary" {
            $blockers += ("listener escalated hard boundary ({0})" -f $(if ([string]::IsNullOrWhiteSpace($decisionReason)) { 'unspecified' } else { $decisionReason }))
        }
        "reject_with_specific_policy_reason" {
            $blockers += ("listener rejected request ({0})" -f $(if ([string]::IsNullOrWhiteSpace($decisionReason)) { 'unspecified' } else { $decisionReason }))
        }
    }
}
if (@($blockers).Count -eq 0) {
    $blockers += "none"
}

$mimFreshnessAlert = $null
$recommendedRecoveryActions = @()
if ([bool]$mimStatus.is_stale) {
    $mimAgeText = "unknown"
    if ($null -ne $mimStatus.age_hours) {
        $mimAgeText = [string]$mimStatus.age_hours
    }

    $mimFreshnessAlert = [pscustomobject]@{
        severity = "critical"
        reason = "mim_context_stale"
        stale_age_hours = $mimAgeText
        stale_after_hours = [string]$mimStatus.stale_after_hours
        detected_at = (Get-Date).ToUniversalTime().ToString("o")
    }

    $recommendedRecoveryActions = @(
        "Run TOD sync with SSH pull: .\\scripts\\Invoke-TODSharedStateSync.ps1 -RefreshMimContextFromSsh -RefreshAgentMimReadiness",
        "If still stale, regenerate MIM export at source: .venv/bin/python scripts/export_mim_context.py --output-dir runtime/shared",
        "Verify remote export timestamp advanced: /home/testpilot/mim/runtime/shared/MIM_CONTEXT_EXPORT.latest.json"
    )
}

if ([bool]$listenerDecision.available -and -not [string]::IsNullOrWhiteSpace([string]$listenerDecision.next_step_recommendation)) {
    $recommendedRecoveryActions += ("Listener recommendation: {0}" -f [string]$listenerDecision.next_step_recommendation)
}

$recommendedRecoveryActions = @($recommendedRecoveryActions | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Unique)

$failedRegressionTestNames = @()
if ($testSummary -and $testSummary.PSObject.Properties["failed_tests"] -and $null -ne $testSummary.failed_tests) {
    $failedRegressionTestNames = @($testSummary.failed_tests | ForEach-Object {
            if ($_.PSObject.Properties["name"]) { [string]$_.name } else { "" }
        } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
}

$nextActions = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-shared-state-sync-v1"
    current_objective_in_progress = $currentObjective
    next_proposed_objective = $NextProposedObjective
    blockers = @($blockers)
    mim_freshness_alert = $mimFreshnessAlert
    recommended_recovery_actions = @($recommendedRecoveryActions)
    training_status = $trainingStatus
    required_verification = @(
        "focused quality gate",
        "full regression suite",
        "smoke and health checks",
        "context exchange export + ingest status"
    )
    integration_work_pending_across_boxes = @(
        "MIM consumes latest shared_state/current_build_state.json",
        "Collaborators drop updates into tod/inbox/context-sync/updates",
        "TOD ingests updates and records them in context-updates-log"
    )
    failing_regression_tests = @($failedRegressionTestNames)
    approval_backlog_snapshot = $approvalBacklog
    integration_status = $integrationStatus
    objective_authority_reset = $objectiveAuthorityReset
    tod_catchup_roadmap = @($todCatchupRoadmap.objectives)
    approval_reduction_summary = if ($approvalReduction) {
        [pscustomobject]@{
            generated_at = if ($approvalReduction.PSObject.Properties["generated_at"]) { [string]$approvalReduction.generated_at } else { "" }
            source = if ($approvalReduction.PSObject.Properties["source"]) { [string]$approvalReduction.source } else { "" }
            totals = if ($approvalReduction.PSObject.Properties["totals"]) { $approvalReduction.totals } else { $null }
            queue_sizes = if ($approvalReduction.PSObject.Properties["queues"] -and $approvalReduction.queues) {
                [pscustomobject]@{
                    promotable_first = if ($approvalReduction.queues.PSObject.Properties["promotable_first"]) { [int]@($approvalReduction.queues.promotable_first).Count } else { 0 }
                    low_value_review = if ($approvalReduction.queues.PSObject.Properties["low_value_review"]) { [int]@($approvalReduction.queues.low_value_review).Count } else { 0 }
                    duplicate_groups = if ($approvalReduction.queues.PSObject.Properties["duplicate_groups"]) { [int]@($approvalReduction.queues.duplicate_groups).Count } else { 0 }
                    duplicate_suppression_candidates = if ($approvalReduction.queues.PSObject.Properties["duplicate_suppression_candidates"]) { [int]@($approvalReduction.queues.duplicate_suppression_candidates).Count } else { 0 }
                }
            }
            else {
                $null
            }
        }
    }
    else {
        $null
    }
}
Write-Utf8NoBomJson -Path $nextActionsPath -Payload $nextActions -Depth 20

$sharedDevLogPlan = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-shared-state-sync-v1"
    contract_version = "tod-shared-development-log-contract-v1"
    purpose = "Shared development logging and handoff protocol between TOD, MIM, and collaborators."
    ownership = [pscustomobject]@{
        tod = @(
            "Publish canonical build/objective state snapshots.",
            "Ingest collaborator updates from context inbox.",
            "Append objective-level sync events to dev journal."
        )
        mim = @(
            "Consume shared_state updates for planning and memory persistence.",
            "Publish structured planning/status updates to TOD context inbox.",
            "Correlate execution lifecycle feedback with objective state."
        )
        collaborators = @(
            "Submit structured updates with source, summary, and project scope.",
            "Use canonical shared_state files as source-of-truth during parallel work.",
            "Avoid direct edits to canonical state artifacts."
        )
    }
    cadence = [pscustomobject]@{
        event_driven = @(
            "after focused quality gate",
            "after full regression",
            "after context ingest/export cycle",
            "after objective transition"
        )
        periodic = [pscustomobject]@{
            minimum = "daily"
            recommended = "per active development session"
        }
    }
    mim_charter = [pscustomobject]@{
        identity = @(
            "Space-aware and intention-driven",
            "Inquisitive and exploratory",
            "Controls its own space, systems, and development",
            "Improves itself through experience, exploration, and concept formation"
        )
        boundaries = [pscustomobject]@{
            hard_stops = @(
                "human safety",
                "ordinary wrongdoing",
                "self-destruction"
            )
            soft_boundaries = @(
                "exploration",
                "trial-and-error",
                "questioning assumptions",
                "testing policy edges in observable and recoverable ways"
            )
        }
    }
    channels = [pscustomobject]@{
        canonical_state_files = @(
            "shared_state/current_build_state.json",
            "shared_state/objectives.json",
            "shared_state/contracts.json",
            "shared_state/next_actions.json",
            "shared_state/shared_development_log_plan.json"
        )
        append_only_logs = @(
            "shared_state/dev_journal.jsonl",
            "tod/out/context-sync/context-updates-log.jsonl"
        )
        handoff_snapshots = @(
            "shared_state/chatgpt_update.md",
            "shared_state/chatgpt_update.json",
            "shared_state/latest_summary.md"
        )
        inbox = "tod/inbox/context-sync/updates"
        processed_updates = "tod/out/context-sync/processed"
    }
    merge_rules = @(
        "append-only for journal and context update logs",
        "use UTC ISO-8601 timestamps in all entries",
        "never overwrite canonical snapshot files manually",
        "prefer objective-scoped summaries over freeform notes",
        "ingested updates must preserve original payload in log record"
    )
}
Write-Utf8NoBomJson -Path $sharedDevLogPlanPath -Payload $sharedDevLogPlan -Depth 20

$journalEntry = [pscustomobject]@{
    timestamp = (Get-Date).ToUniversalTime().ToString("o")
    machine = $env:COMPUTERNAME
    repo = "TOD"
    objective = $currentObjective
    action = "shared_state_sync"
    summary = "Regenerated shared_state snapshots and contracts; objective ledger refreshed."
    commit_sha = $commitSha
    validation_result = [pscustomobject]@{
        regression_passed = [bool]$lastRegressionResult.passed_all
        quality_gate_ok = [bool]$lastPromotionResult.gate_ok
        smoke_passed = if ($smokeSummary -and $smokeSummary.PSObject.Properties["passed_all"]) { [bool]$smokeSummary.passed_all } else { $false }
    }
}
Append-Utf8NoBomJsonLine -Path $devJournalPath -Payload $journalEntry -Depth 12

$summaryLines = @()
$summaryLines += "# Shared State Summary"
$summaryLines += ""
$summaryLines += "Generated: $($currentBuildState.generated_at)"
$summaryLines += "Machine: $($env:COMPUTERNAME)"
$summaryLines += "Repo: TOD"
$summaryLines += "Branch: $branch"
$summaryLines += "Commit: $commitSha"
$summaryLines += "Release tag: $releaseTag"
$summaryLines += ""
$summaryLines += "## Build State"
$summaryLines += "- Latest objective completed: $latestCompletedObjective"
$summaryLines += "- Current objective in progress: $currentObjective"
$summaryLines += "- Test status: passed=$($lastRegressionResult.passed) failed=$($lastRegressionResult.failed) total=$($lastRegressionResult.total)"
$summaryLines += "- Quality gate ok: $([bool]$lastPromotionResult.gate_ok)"
$summaryLines += "- MIM freshness alert: $(if ([bool]$mimStatus.is_stale) { 'CRITICAL' } else { 'OK' }) (age_hours=$($mimStatus.age_hours), threshold=$($mimStatus.stale_after_hours))"
$summaryLines += "- Drift trend: $($knownLocalDrift.trend)"
$summaryLines += "- Objective alignment source: $($objectiveAlignment.mim_objective_source)"
$summaryLines += "- Bridge evidence source: $($integrationStatus.bridge_canonical_evidence.evidence_source)"
$summaryLines += "- Bridge remote publish verified: $([bool]$integrationStatus.bridge_canonical_evidence.remote_publish_verified)"
$summaryLines += "- Bridge guidance: $(if (@($integrationStatus.bridge_operator_guidance).Count -gt 0) { ((@($integrationStatus.bridge_operator_guidance | ForEach-Object { [string]$_.summary })) -join '; ') } else { 'none' })"
$summaryLines += "- Handshake truth available: $([bool]$mimHandshake.available)"
if ([bool]$mimHandshake.available) {
    $summaryLines += "- Handshake objective_active: $($mimHandshake.objective_active)"
    $summaryLines += "- Handshake latest_completed: $($mimHandshake.latest_completed_objective)"
    $summaryLines += "- Handshake next_objective: $($mimHandshake.current_next_objective)"
    $summaryLines += "- Handshake release_tag: $($mimHandshake.release_tag)"
    $summaryLines += "- Handshake regression: $($mimHandshake.regression_status)"
    $summaryLines += "- Handshake prod_promotion: $($mimHandshake.prod_promotion_status)"
    $summaryLines += "- Handshake prod_smoke: $($mimHandshake.prod_smoke_status)"
}
$summaryLines += ""
$summaryLines += "## Next Actions"
foreach ($item in @($nextActions.required_verification)) {
    $summaryLines += "- $item"
}
$summaryLines += ""
$summaryLines += "## Canonical Files"
$summaryLines += "- current_build_state.json"
$summaryLines += "- objectives.json"
$summaryLines += "- contracts.json"
$summaryLines += "- next_actions.json"
$summaryLines += "- shared_development_log_plan.json"
$summaryLines += "- dev_journal.jsonl"
$summaryLines += "- latest_summary.md"

$summaryLines -join [Environment]::NewLine | Set-Content -Path $latestSummaryPath

$chatgptSnapshot = [pscustomobject]@{
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    source = "tod-shared-state-sync-v1"
    objective = [pscustomobject]@{
        current_in_progress = $currentObjective
        latest_completed = $latestCompletedObjective
        next_proposed = $NextProposedObjective
        objective_count = @($objectiveRecords).Count
    }
    repo = [pscustomobject]@{
        root = $repoRoot
        branch = $branch
        commit = $commitSha
        release_tag = $releaseTag
    }
    validation = [pscustomobject]@{
        regression = $lastRegressionResult
        quality_gate = $lastPromotionResult
        smoke = $currentProdTestStatus.smoke
    }
    handshake_truth_summary = [pscustomobject]@{
        available = [bool]$mimHandshake.available
        source_path = [string]$mimHandshake.source_path
        generated_at = [string]$mimHandshake.generated_at
        objective_active = [string]$mimHandshake.objective_active
        latest_completed_objective = [string]$mimHandshake.latest_completed_objective
        current_next_objective = [string]$mimHandshake.current_next_objective
        schema_version = [string]$mimHandshake.schema_version
        release_tag = [string]$mimHandshake.release_tag
        regression_status = [string]$mimHandshake.regression_status
        regression_tests = [string]$mimHandshake.regression_tests
        prod_promotion_status = [string]$mimHandshake.prod_promotion_status
        prod_smoke_status = [string]$mimHandshake.prod_smoke_status
        blockers = @($mimHandshake.blockers)
        alignment_source = [string]$objectiveAlignment.mim_objective_source
    }
    bridge = [pscustomobject]@{
        canonical_evidence = $bridgeCanonicalEvidence
        operator_guidance = @($bridgeOperatorGuidance)
    }
    drift = $knownLocalDrift
    blockers = @($blockers)
    capabilities = @($activeCapabilities)
    important_files = [pscustomobject]@{
        current_build_state = $currentBuildStatePath
        objectives = $objectivesPath
        contracts = $contractsPath
        next_actions = $nextActionsPath
        integration_status = $integrationStatusPath
        tod_status_publish_receipt = $todStatusPublishReceiptPath
        execution_evidence = $executionEvidencePath
        tod_objective_roadmap = $objectiveRoadmapPath
        approval_reduction_summary = if ($approvalReduction) { (Get-LocalPath -PathValue $ApprovalReductionPath) } else { "" }
        shared_development_log_plan = $sharedDevLogPlanPath
        dev_journal = $devJournalPath
        latest_summary = $latestSummaryPath
        chatgpt_update = $chatgptUpdatePath
        chatgpt_update_json = $chatgptUpdateJsonPath
    }
}

Write-Utf8NoBomJson -Path $chatgptUpdateJsonPath -Payload $chatgptSnapshot -Depth 20

$chatgptLines = @()
$chatgptLines += "# TOD ChatGPT Development Update"
$chatgptLines += ""
$chatgptLines += "Generated: $($chatgptSnapshot.generated_at)"
$chatgptLines += ""
$chatgptLines += "## Objective Status"
$chatgptLines += "- Current objective in progress: $currentObjective"
$chatgptLines += "- Latest completed objective: $latestCompletedObjective"
$chatgptLines += "- Next proposed objective: $NextProposedObjective"
$chatgptLines += "- Total objectives tracked: $(@($objectiveRecords).Count)"
$chatgptLines += ""
$chatgptLines += "## Build + Repo"
$chatgptLines += "- Branch: $branch"
$chatgptLines += "- Commit: $commitSha"
$chatgptLines += "- Release tag: $releaseTag"
$chatgptLines += ""
$chatgptLines += "## Validation"
$chatgptLines += "- Regression passed: $([bool]$lastRegressionResult.passed_all) (passed=$($lastRegressionResult.passed), failed=$($lastRegressionResult.failed), total=$($lastRegressionResult.total))"
$chatgptLines += "- Quality gate ok: $([bool]$lastPromotionResult.gate_ok)"
$chatgptLines += "- Smoke passed: $([bool]$currentProdTestStatus.smoke.passed_all)"
$chatgptLines += ""
$chatgptLines += "## Drift + Blockers"
$chatgptLines += "- Trend: $($knownLocalDrift.trend)"
$chatgptLines += "- Reliability alert: $($knownLocalDrift.reliability_alert_state)"
$chatgptLines += "- Pending approvals: $($knownLocalDrift.pending_approvals)"
foreach ($item in @($blockers)) {
    $chatgptLines += "- Blocker: $item"
}
$chatgptLines += "- Approval triage by type: $(($approvalBacklog.by_type | ConvertTo-Json -Compress))"
$chatgptLines += "- Approval triage by age: $(($approvalBacklog.by_age | ConvertTo-Json -Compress))"
$chatgptLines += "- Approval triage by source: $(($approvalBacklog.by_source | ConvertTo-Json -Compress))"
$chatgptLines += "- Approval triage counts: stale=$($approvalBacklog.stale_count) low_value=$($approvalBacklog.low_value_count) promotable=$($approvalBacklog.promotable_count)"
$chatgptLines += "- Integration status: mim_schema=$($integrationStatus.mim_schema) tod_contract=$($integrationStatus.tod_contract) compatible=$([bool]$integrationStatus.compatible)"
$chatgptLines += "- MIM freshness: available=$([bool]$integrationStatus.mim_status.available) stale=$([bool]$integrationStatus.mim_status.is_stale) age_hours=$($integrationStatus.mim_status.age_hours)"
$chatgptLines += "- Objective alignment: status=$($integrationStatus.objective_alignment.status) tod=$($integrationStatus.objective_alignment.tod_current_objective) mim=$($integrationStatus.objective_alignment.mim_objective_active)"
$chatgptLines += "- Objective alignment source: $($integrationStatus.objective_alignment.mim_objective_source)"
$chatgptLines += "- Listener decision: available=$([bool]$integrationStatus.listener_decision.available) outcome=$($integrationStatus.listener_decision.decision_outcome) reason=$($integrationStatus.listener_decision.reason_code) execution_state=$($integrationStatus.listener_decision.execution_state)"
$chatgptLines += "- Bridge canonical evidence: source=$($integrationStatus.bridge_canonical_evidence.evidence_source) canonical_refresh=$([bool]$integrationStatus.bridge_canonical_evidence.canonical_refresh_satisfied) remote_publish_verified=$([bool]$integrationStatus.bridge_canonical_evidence.remote_publish_verified)"
$chatgptLines += "- Bridge failure signals: $(if (@($integrationStatus.bridge_canonical_evidence.failure_signals).Count -gt 0) { (@($integrationStatus.bridge_canonical_evidence.failure_signals) -join '; ') } else { 'none' })"
$chatgptLines += "- Bridge operator guidance: $(if (@($integrationStatus.bridge_operator_guidance).Count -gt 0) { ((@($integrationStatus.bridge_operator_guidance | ForEach-Object { [string]$_.recommended_action })) -join '; ') } else { 'none' })"
$chatgptLines += "- TOD status publish: status=$($integrationStatus.tod_status_publish.status) host=$($integrationStatus.tod_status_publish.ssh_host) remote=$($integrationStatus.tod_status_publish.remote_primary_path) summary=$($integrationStatus.tod_status_publish.remote_summary_path) consumer=$($integrationStatus.tod_status_publish.consumer_status)"
$chatgptLines += "- Handshake truth available: $([bool]$mimHandshake.available)"
if ([bool]$mimHandshake.available) {
    $chatgptLines += "- Handshake objective_active: $($mimHandshake.objective_active)"
    $chatgptLines += "- Handshake latest_completed_objective: $($mimHandshake.latest_completed_objective)"
    $chatgptLines += "- Handshake current_next_objective: $($mimHandshake.current_next_objective)"
    $chatgptLines += "- Handshake schema_version: $($mimHandshake.schema_version)"
    $chatgptLines += "- Handshake release_tag: $($mimHandshake.release_tag)"
    $chatgptLines += "- Handshake regression: $($mimHandshake.regression_status) ($($mimHandshake.regression_tests))"
    $chatgptLines += "- Handshake prod promotion: $($mimHandshake.prod_promotion_status)"
    $chatgptLines += "- Handshake prod smoke: $($mimHandshake.prod_smoke_status)"
    $chatgptLines += "- Handshake blockers: $(if (@($mimHandshake.blockers).Count -gt 0) { (@($mimHandshake.blockers) -join '; ') } else { 'none' })"
}
$chatgptLines += "- Catch-up roadmap: $(($todCatchupRoadmap.objectives | ForEach-Object { [string]$_.id }) -join ', ')"
$chatgptLines += "- Approval reduction snapshot present: $(if ($approvalReduction) { 'true' } else { 'false' })"
if ($approvalReduction -and $approvalReduction.PSObject.Properties["totals"]) {
    $chatgptLines += "- Approval reduction totals: $(($approvalReduction.totals | ConvertTo-Json -Compress))"
}
$chatgptLines += "- Failing regression tests: $(if (@($failedRegressionTestNames).Count -gt 0) { (@($failedRegressionTestNames) -join '; ') } else { 'none' })"
$chatgptLines += ""
$chatgptLines += "## Canonical Shared State Files"
$chatgptLines += "- $currentBuildStatePath"
$chatgptLines += "- $objectivesPath"
$chatgptLines += "- $contractsPath"
$chatgptLines += "- $nextActionsPath"
$chatgptLines += "- $sharedDevLogPlanPath"
$chatgptLines += "- $devJournalPath"
$chatgptLines += "- $latestSummaryPath"
$chatgptLines += "- $chatgptUpdateJsonPath"

$chatgptLines -join [Environment]::NewLine | Set-Content -Path $chatgptUpdatePath

$result = [pscustomobject]@{
    ok = $true
    source = "tod-shared-state-sync-v1"
    generated_at = (Get-Date).ToUniversalTime().ToString("o")
    output_dir = $sharedDirAbs
    files = [pscustomobject]@{
        current_build_state = $currentBuildStatePath
        objectives = $objectivesPath
        contracts = $contractsPath
        next_actions = $nextActionsPath
        integration_status = $integrationStatusPath
        tod_status_publish_receipt = $todStatusPublishReceiptPath
        tod_status_publish_receipt_legacy = $todStatusPublishLegacyReceiptPath
        execution_evidence = $executionEvidencePath
        tod_objective_roadmap = $objectiveRoadmapPath
        shared_development_log_plan = $sharedDevLogPlanPath
        dev_journal = $devJournalPath
        latest_summary = $latestSummaryPath
        chatgpt_update = $chatgptUpdatePath
        chatgpt_update_json = $chatgptUpdateJsonPath
    }
    quick_status = [pscustomobject]@{
        branch = $branch
        commit = $commitSha
        current_objective_in_progress = $currentObjective
        regression_passed = [bool]$lastRegressionResult.passed_all
        quality_gate_ok = [bool]$lastPromotionResult.gate_ok
        tod_status_publish = [string]$integrationStatus.tod_status_publish.status
    }
}

if ($RefreshAgentMimReadiness) {
    $agentMimReadiness = $null
    try {
        $agentMimReadiness = & (Join-Path $PSScriptRoot "Invoke-TODAgentMimReadinessCycle.ps1") -EmitJson | ConvertFrom-Json
        $result.files | Add-Member -NotePropertyName "agentmim_readiness" -NotePropertyValue "shared_state/agentmim/MIM_TOD_AGENTMIM_READINESS.latest.json" -Force
        $result.quick_status | Add-Member -NotePropertyName "agentmim_status" -NotePropertyValue ([string]$agentMimReadiness.status) -Force
        $result.quick_status | Add-Member -NotePropertyName "agentmim_strict_passed" -NotePropertyValue ([int]$agentMimReadiness.summary.strict_gate_passed) -Force
        $result.quick_status | Add-Member -NotePropertyName "agentmim_strict_failed" -NotePropertyValue ([int]$agentMimReadiness.summary.strict_gate_failed) -Force
    }
    catch {
        $result | Add-Member -NotePropertyName "agentmim_readiness_error" -NotePropertyValue $_.Exception.Message -Force
    }
}

$result | ConvertTo-Json -Depth 12 | Write-Output
