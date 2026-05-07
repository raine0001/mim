param(
    [string]$SharedRoot = 'runtime/shared',
    [string]$SharedTruthPath = 'runtime/shared/TOD_MIM_SHARED_TRUTH.latest.json',
    [string]$IntegrationStatusPath = 'shared_state/integration_status.json',
    [string]$ReconcileScriptPath = 'scripts/reconcile_tod_mim_shared_truth.py',
    [string]$ReconcileOutputPath = 'runtime/shared/TOD_MIM_SHARED_TRUTH.latest.json',
    [switch]$SkipReconcile,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-LocalPath {
    param([Parameter(Mandatory = $true)][string]$PathValue)

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PathValue))
}

function Read-JsonFileIfExists {
    param([Parameter(Mandatory = $true)][string]$PathValue)

    if (-not (Test-Path -Path $PathValue)) {
        return $null
    }

    try {
        return (Get-Content -Raw -Path $PathValue | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)]$Payload,
        [int]$Depth = 20
    )

    $directory = Split-Path -Parent $PathValue
    if (-not [string]::IsNullOrWhiteSpace($directory) -and -not (Test-Path -Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $json = ($Payload | ConvertTo-Json -Depth $Depth)
    [System.IO.File]::WriteAllText($PathValue, $json, $utf8NoBom)
}

function Get-UtcNowIso {
    return (Get-Date).ToUniversalTime().ToString('o')
}

function Get-StringProperty {
    param(
        [AllowNull()]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $InputObject) {
        return ''
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name) -and $null -ne $InputObject[$Name]) {
            return [string]$InputObject[$Name]
        }
        return ''
    }

    if ($InputObject.PSObject.Properties[$Name] -and $null -ne $InputObject.$Name) {
        return [string]$InputObject.$Name
    }

    return ''
}

function Get-ObjectProperty {
    param(
        [AllowNull()]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $InputObject) {
        return $null
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) {
            return $InputObject[$Name]
        }
        return $null
    }

    if ($InputObject.PSObject.Properties[$Name]) {
        return $InputObject.$Name
    }

    return $null
}

function Get-NormalizedObjectiveToken {
    param([AllowEmptyString()][string]$ObjectiveId)

    if ([string]::IsNullOrWhiteSpace($ObjectiveId)) {
        return ''
    }

    $match = [regex]::Match($ObjectiveId, '(\d+)$')
    if ($match.Success) {
        return [string]$match.Groups[1].Value
    }

    return [string]$ObjectiveId
}

function Get-ArtifactLane {
    param([AllowNull()]$Payload)

    $summaryPayload = Get-ObjectProperty -InputObject $Payload -Name 'summary'
    $liveTaskPayload = Get-ObjectProperty -InputObject $Payload -Name 'live_task_request'

    $objectiveId = ''
    foreach ($candidate in @(
        (Get-StringProperty -InputObject $Payload -Name 'objective_id'),
        (Get-StringProperty -InputObject $Payload -Name 'source_objective'),
        (Get-StringProperty -InputObject $summaryPayload -Name 'objective_id'),
        (Get-StringProperty -InputObject $liveTaskPayload -Name 'objective_id')
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $objectiveId = $candidate
            break
        }
    }

    $taskId = ''
    foreach ($candidate in @(
        (Get-StringProperty -InputObject $Payload -Name 'task_id'),
        (Get-StringProperty -InputObject $Payload -Name 'selected_task_id'),
        (Get-StringProperty -InputObject $Payload -Name 'current_task_id'),
        (Get-StringProperty -InputObject $summaryPayload -Name 'task_id'),
        (Get-StringProperty -InputObject $liveTaskPayload -Name 'task_id')
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $taskId = $candidate
            break
        }
    }

    $requestId = ''
    foreach ($candidate in @(
        (Get-StringProperty -InputObject $Payload -Name 'request_id'),
        (Get-StringProperty -InputObject $summaryPayload -Name 'request_id'),
        (Get-StringProperty -InputObject $liveTaskPayload -Name 'request_id')
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $requestId = $candidate
            break
        }
    }

    return [pscustomobject]@{
        objective_id = $objectiveId
        normalized_objective_id = Get-NormalizedObjectiveToken -ObjectiveId $objectiveId
        task_id = $taskId
        request_id = $requestId
    }
}

function Test-ArtifactNeedsRecoupling {
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)][string]$CanonicalObjectiveId,
        [Parameter(Mandatory = $true)][string]$CanonicalTaskId
    )

    $lane = Get-ArtifactLane -Payload $Payload
    $canonicalObjectiveToken = Get-NormalizedObjectiveToken -ObjectiveId $CanonicalObjectiveId

    if (-not [string]::IsNullOrWhiteSpace([string]$lane.normalized_objective_id) -and [string]$lane.normalized_objective_id -ne $canonicalObjectiveToken) {
        return $true
    }

    if (-not [string]::IsNullOrWhiteSpace([string]$lane.task_id) -and [string]$lane.task_id -ne $CanonicalTaskId) {
        return $true
    }

    return $false
}

function New-CoupledBlockerRecord {
    param([Parameter(Mandatory = $true)][string]$Summary)

    return @(
        [ordered]@{
            type = 'blocker'
            reason_code = 'canonical_latest_artifact_needs_refresh'
            file = 'runtime/shared'
            function = 'Invoke-TODCanonicalLatestArtifactRecoupling'
            reason = $Summary
            task_id = $script:canonicalTaskId
        }
    )
}

function New-CanonicalRecouplingPayload {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactName,
        [Parameter(Mandatory = $true)][string]$Timestamp,
        [Parameter(Mandatory = $true)][string]$Summary,
        [Parameter(Mandatory = $true)][string]$TaskTitle,
        [Parameter(Mandatory = $true)][string]$CurrentAction,
        [Parameter(Mandatory = $true)][string]$NextStep,
        [Parameter(Mandatory = $true)][string]$CanonicalState,
        [Parameter(Mandatory = $true)][string]$CanonicalPhase,
        [Parameter(Mandatory = $true)][string]$CanonicalSource,
        [Parameter(Mandatory = $true)][string]$CanonicalLaneSource
    )

    $base = [ordered]@{
        generated_at = $Timestamp
        updated_at = $Timestamp
        source = 'tod-canonical-latest-recoupling-v1'
        surface = 'tod-canonical-latest-recoupling'
        session_key = 'tod-canonical-latest-recoupling'
        request_id = $script:canonicalRequestId
        task_id = $script:canonicalTaskId
        objective_id = $script:canonicalObjectiveId
        normalized_objective_id = $script:canonicalObjectiveToken
        title = $TaskTitle
        summary = $Summary
        reason_code = 'canonical_latest_artifact_needs_refresh'
        recovery_state = 'needs_refresh'
        canonical_recoupling = [ordered]@{
            source = $CanonicalSource
            canonical_lane_source = $CanonicalLaneSource
            target_objective_id = $script:canonicalObjectiveId
            target_task_id = $script:canonicalTaskId
            refreshed_at = $Timestamp
        }
    }

    switch ($ArtifactName) {
        'TOD_ACTIVE_OBJECTIVE.latest.json' {
            return [ordered]@{} + $base + @{
                packet_type = 'tod-active-objective-v1'
                status = 'blocked'
                execution_state = 'blocked_with_reason'
                current_action = $CurrentAction
                next_step = $NextStep
                phase = $CanonicalPhase
                blocker_detail = $Summary
                execution_evidence = [ordered]@{
                    source = 'tod-canonical-latest-recoupling-v1'
                    summary = $Summary
                    meaningful_evidence = @()
                    blockers = New-CoupledBlockerRecord -Summary $Summary
                }
            }
        }
        'TOD_ACTIVE_TASK.latest.json' {
            return [ordered]@{} + $base + @{
                packet_type = 'tod-active-task-v1'
                status = 'blocked'
                execution_state = 'blocked_with_reason'
                current_action = $CurrentAction
                next_step = $NextStep
                next_validation = 'refresh canonical latest artifacts and republish direct TOD execution evidence'
                task_focus = $Summary
                phase = $CanonicalPhase
                execution_evidence = [ordered]@{
                    selection_kind = 'canonical_recoupling'
                    reason_selected = $Summary
                    expected_evidence = @('canonical_latest_artifact_refresh')
                    validation_plan = @('refresh canonical latest artifacts and republish direct TOD execution evidence')
                }
            }
        }
        'TOD_ACTIVITY_STREAM.latest.json' {
            return [ordered]@{} + $base + @{
                packet_type = 'tod-activity-stream-v1'
                event = 'canonical_latest_artifact_recoupled'
                status = 'blocked'
                phase = $CanonicalPhase
                execution_state = 'blocked_with_reason'
                current_action = $CurrentAction
                next_step = $NextStep
                next_validation = 'refresh canonical latest artifacts and republish direct TOD execution evidence'
                execution_evidence = [ordered]@{
                    selection_kind = 'canonical_recoupling'
                    reason_selected = $Summary
                    expected_evidence = @('canonical_latest_artifact_refresh')
                    validation_plan = @('refresh canonical latest artifacts and republish direct TOD execution evidence')
                }
            }
        }
        'TOD_VALIDATION_RESULT.latest.json' {
            return [ordered]@{} + $base + @{
                packet_type = 'tod-validation-result-v1'
                status = 'blocked'
                phase = $CanonicalPhase
                validation_target = 'refresh canonical latest artifacts'
                validation_summary = $Summary
                evidence = [ordered]@{
                    matched_files = @()
                    command_output = ''
                }
                checks = @(
                    [ordered]@{
                        name = 'canonical_latest_artifact_refresh'
                        passed = $false
                        required = $true
                    }
                )
                blockers = New-CoupledBlockerRecord -Summary $Summary
            }
        }
        'TOD_EXECUTION_RESULT.latest.json' {
            return [ordered]@{} + $base + @{
                packet_type = 'tod-execution-result-v1'
                status = 'blocked'
                phase = $CanonicalPhase
                execution_state = 'blocked_with_reason'
                validation_summary = $Summary
                current_action = $CurrentAction
                next_step = $NextStep
                wait_reason = $Summary
                rollback_state = 'not_needed'
                command_output = ''
                files_changed = @()
                commands_run = @()
                validation_results = @(
                    [ordered]@{
                        name = 'canonical_latest_artifact_refresh'
                        passed = $false
                        required = $true
                    }
                )
                blockers = New-CoupledBlockerRecord -Summary $Summary
                execution_evidence = [ordered]@{
                    source = 'tod-canonical-latest-recoupling-v1'
                    summary = $Summary
                    matched_files = @()
                    files_changed = @()
                    validation_checks = @(
                        [ordered]@{
                            name = 'canonical_latest_artifact_refresh'
                            passed = $false
                            required = $true
                        }
                    )
                    validation_passed = $false
                    command_output = ''
                    rollback_state = 'not_needed'
                    recovery_state = 'needs_refresh'
                    review_decision = 'revise'
                    reason_code = 'canonical_latest_artifact_needs_refresh'
                    no_op_detected = $false
                    task_class = 'implementation'
                    meaningful_evidence = @()
                    blockers = New-CoupledBlockerRecord -Summary $Summary
                    diff_summary = ''
                    commands_run = @()
                    validation_results = @(
                        [ordered]@{
                            name = 'canonical_latest_artifact_refresh'
                            passed = $false
                            required = $true
                        }
                    )
                    confidence = ''
                    rollback_hint = ''
                }
            }
        }
        'TOD_EXECUTION_TRUTH.latest.json' {
            $truthRow = [ordered]@{}
            $truthRow.generated_at = $Timestamp
            $truthRow.objective_id = $script:canonicalObjectiveId
            $truthRow.task_id = $script:canonicalTaskId
            $truthRow.execution_id = 'canonical-latest-artifact-recoupling'
            $truthRow.request_id = $script:canonicalRequestId
            $truthRow.execution_state = 'blocked_with_reason'
            $truthRow.status = 'blocked'
            $truthRow.summary = $Summary
            $truthRow.current_action = $CurrentAction
            $truthRow.next_step = $NextStep
            $truthRow.next_validation = 'refresh canonical latest artifacts'
            $truthRow.validation_passed = $false
            $truthRow.reason_code = 'canonical_latest_artifact_needs_refresh'
            $truthRow.recovery_state = 'needs_refresh'
            $truthRow.execution_evidence = [ordered]@{
                source = 'tod-canonical-latest-recoupling-v1'
                summary = $Summary
                matched_files = @()
                files_changed = @()
                validation_checks = @(
                    [ordered]@{
                        name = 'canonical_latest_artifact_refresh'
                        passed = $false
                        required = $true
                    }
                )
                validation_passed = $false
                command_output = ''
                rollback_state = 'not_needed'
                recovery_state = 'needs_refresh'
                review_decision = 'revise'
                reason_code = 'canonical_latest_artifact_needs_refresh'
                no_op_detected = $false
                task_class = 'implementation'
                meaningful_evidence = @()
                blockers = New-CoupledBlockerRecord -Summary $Summary
                diff_summary = ''
                commands_run = @()
                validation_results = @(
                    [ordered]@{
                        name = 'canonical_latest_artifact_refresh'
                        passed = $false
                        required = $true
                    }
                )
                confidence = ''
                rollback_hint = ''
            }
            return [ordered]@{
                generated_at = $Timestamp
                source = 'tod-canonical-latest-recoupling-v1'
                summary = [ordered]@{
                    execution_count = 1
                    latest_execution_at = $Timestamp
                    objective_id = $script:canonicalObjectiveId
                    task_id = $script:canonicalTaskId
                    request_id = $script:canonicalRequestId
                    summary = $Summary
                    current_action = $CurrentAction
                    next_step = $NextStep
                    validation_passed = $false
                    reason_code = 'canonical_latest_artifact_needs_refresh'
                }
                recent_execution_truth = @($truthRow)
                canonical_recoupling = $base.canonical_recoupling
            }
        }
        'TOD_NEXT_TASK_SELECTION.latest.json' {
            return [ordered]@{
                generated_at = $Timestamp
                source = 'tod-canonical-latest-recoupling-v1'
                source_objective = $script:canonicalObjectiveId
                selected_task_id = $script:canonicalTaskId
                reason_selected = $Summary
                request_id = $script:canonicalRequestId
                selected_task_title = $TaskTitle
                selected_task_scope = $Summary
                selection_kind = 'canonical_recoupling'
                expected_evidence = @('canonical_latest_artifact_refresh')
                validation_plan = @('refresh canonical latest artifacts and republish direct TOD execution evidence')
                dispatch_status = 'blocked_with_reason'
                status = 'blocked'
                completion_status = 'needs_refresh'
                result_reason = $Summary
                summary = $Summary
                canonical_recoupling = $base.canonical_recoupling
                last_terminal_outcome = [ordered]@{
                    classification = 'needs_refresh'
                    execution_state = 'blocked_with_reason'
                    reason_code = 'canonical_latest_artifact_needs_refresh'
                    summary = $Summary
                }
            }
        }
        default {
            throw "Unsupported artifact name: $ArtifactName"
        }
    }
}

function Write-SupersededArtifactRecord {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)]$PreviousPayload,
        [Parameter(Mandatory = $true)]$ReplacementPayload,
        [Parameter(Mandatory = $true)][string]$Timestamp
    )

    $directory = Split-Path -Parent $ArtifactPath
    $artifactName = Split-Path -Leaf $ArtifactPath
    $supersededRoot = Join-Path $directory 'superseded'
    $artifactRoot = Join-Path $supersededRoot $artifactName
    if (-not (Test-Path -Path $artifactRoot)) {
        New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
    }

    $record = [ordered]@{
        generated_at = $Timestamp
        reason_code = 'canonical_latest_artifact_recoupled'
        artifact_name = $artifactName
        target_path = $ArtifactPath
        previous_lane = Get-ArtifactLane -Payload $PreviousPayload
        canonical_lane = [ordered]@{
            objective_id = $script:canonicalObjectiveId
            task_id = $script:canonicalTaskId
            request_id = $script:canonicalRequestId
        }
        previous_payload = $PreviousPayload
        replacement_payload = $ReplacementPayload
    }

    $stamp = ([datetime]::Parse($Timestamp)).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
    $recordPath = Join-Path $artifactRoot ("{0}.superseded.json" -f $stamp)
    $latestPath = Join-Path $artifactRoot 'latest.superseded.json'
    Write-JsonFile -PathValue $recordPath -Payload $record
    Write-JsonFile -PathValue $latestPath -Payload $record

    return [pscustomobject]@{
        record_path = $recordPath
        latest_superseded_path = $latestPath
    }
}

function Get-Residual0631Artifacts {
    param([Parameter(Mandatory = $true)][string]$RootPath)

    $matches = @()
    foreach ($file in @(Get-ChildItem -Path $RootPath -Filter '*.latest.json' -File -ErrorAction SilentlyContinue)) {
        $payload = Read-JsonFileIfExists -PathValue $file.FullName
        if ($null -eq $payload) {
            continue
        }

        $lane = Get-ArtifactLane -Payload $payload
        if ([string]$lane.normalized_objective_id -eq '0631') {
            $matches += [pscustomobject]@{
                name = $file.Name
                objective_id = [string]$lane.objective_id
                task_id = [string]$lane.task_id
            }
        }
    }

    return @($matches)
}

function Get-ExecutionLockView {
    param([AllowNull()]$Payload)

    if ($null -eq $Payload) {
        return $null
    }

    $currentProcessing = Get-ObjectProperty -InputObject $Payload -Name 'current_processing'
    $taskId = Get-StringProperty -InputObject $Payload -Name 'task_id'
    if ([string]::IsNullOrWhiteSpace($taskId)) {
        $taskId = Get-StringProperty -InputObject $currentProcessing -Name 'task_id'
    }
    if ([string]::IsNullOrWhiteSpace($taskId)) {
        return $null
    }

    return [pscustomobject]@{
        objective_id = Get-StringProperty -InputObject $Payload -Name 'objective_id'
        task_id = $taskId
        request_id = Get-StringProperty -InputObject $Payload -Name 'request_id'
        source = Get-StringProperty -InputObject $Payload -Name 'source'
        writer = Get-StringProperty -InputObject $Payload -Name 'writer'
        path = ''
    }
}

$sharedRootPath = Get-LocalPath -PathValue $SharedRoot
$sharedTruthAbs = Get-LocalPath -PathValue $SharedTruthPath
$integrationStatusAbs = Get-LocalPath -PathValue $IntegrationStatusPath
$reconcileScriptAbs = Get-LocalPath -PathValue $ReconcileScriptPath
$reconcileOutputAbs = Get-LocalPath -PathValue $ReconcileOutputPath
$executionLockAbs = Join-Path $sharedRootPath 'TOD_EXECUTION_LOCK.latest.json'

$sharedTruth = Read-JsonFileIfExists -PathValue $sharedTruthAbs
if ($null -eq $sharedTruth) {
    throw "Shared truth artifact not found or unreadable: $sharedTruthAbs"
}

$executionLock = Get-ExecutionLockView -Payload (Read-JsonFileIfExists -PathValue $executionLockAbs)
if ($null -ne $executionLock) {
    $executionLock.path = $executionLockAbs
}

$integrationStatus = Read-JsonFileIfExists -PathValue $integrationStatusAbs

$script:canonicalObjectiveId = Get-StringProperty -InputObject $sharedTruth -Name 'objective_id'
$script:canonicalTaskId = Get-StringProperty -InputObject $sharedTruth -Name 'task_id'
$script:canonicalRequestId = Get-StringProperty -InputObject $sharedTruth -Name 'request_id'
if ([string]::IsNullOrWhiteSpace($script:canonicalRequestId)) {
    $script:canonicalRequestId = $script:canonicalTaskId
}
$script:canonicalObjectiveToken = Get-NormalizedObjectiveToken -ObjectiveId $script:canonicalObjectiveId

if ([string]::IsNullOrWhiteSpace($script:canonicalObjectiveId) -or [string]::IsNullOrWhiteSpace($script:canonicalTaskId)) {
    throw "Shared truth does not expose a canonical objective/task lane."
}

if ($null -ne $executionLock) {
    if (-not [string]::IsNullOrWhiteSpace([string]$executionLock.objective_id) -and -not [string]::Equals([string]$executionLock.objective_id, [string]$script:canonicalObjectiveId, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("Execution lock objective mismatch: shared_truth={0} execution_lock={1}" -f $script:canonicalObjectiveId, [string]$executionLock.objective_id)
    }
    if (-not [string]::Equals([string]$executionLock.task_id, [string]$script:canonicalTaskId, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("Execution lock task mismatch: shared_truth={0} execution_lock={1}" -f $script:canonicalTaskId, [string]$executionLock.task_id)
    }
    if ([string]::IsNullOrWhiteSpace($script:canonicalRequestId)) {
        $script:canonicalRequestId = [string]$executionLock.request_id
    }
}

$taskTitle = Get-StringProperty -InputObject $sharedTruth -Name 'task_title'
if ([string]::IsNullOrWhiteSpace($taskTitle)) {
    $mimView = Get-ObjectProperty -InputObject $sharedTruth -Name 'mim_view'
    $taskTitle = Get-StringProperty -InputObject $mimView -Name 'task_title'
}

$mimView = Get-ObjectProperty -InputObject $sharedTruth -Name 'mim_view'
$canonicalLaneSource = Get-StringProperty -InputObject $sharedTruth -Name 'canonical_lane_source'
$canonicalState = Get-StringProperty -InputObject $mimView -Name 'state'
if ([string]::IsNullOrWhiteSpace($canonicalState)) {
    $canonicalState = 'BLOCKED_WITH_REASON'
}

$canonicalPhase = Get-StringProperty -InputObject $mimView -Name 'phase'
if ([string]::IsNullOrWhiteSpace($canonicalPhase)) {
    $canonicalPhase = 'canonical_recoupling'
}

$sharedTruthDisagreementDetected = [bool](Get-ObjectProperty -InputObject $sharedTruth -Name 'disagreement_detected')
$todView = Get-ObjectProperty -InputObject $sharedTruth -Name 'tod_view'
$todViewObjective = Get-StringProperty -InputObject $todView -Name 'objective_id'
$todViewTask = Get-StringProperty -InputObject $todView -Name 'task_id'
$directCanonicalEvidenceAvailable = (-not $sharedTruthDisagreementDetected) -and ($todViewObjective -eq $script:canonicalObjectiveId) -and ($todViewTask -eq $script:canonicalTaskId)

$canonicalReason = Get-StringProperty -InputObject $mimView -Name 'reason'
if ([string]::IsNullOrWhiteSpace($canonicalReason)) {
    $canonicalReason = Get-StringProperty -InputObject $sharedTruth -Name 'blocker_detail'
}

$summary = if ($directCanonicalEvidenceAvailable) {
    $stateReason = Get-StringProperty -InputObject $sharedTruth -Name 'state_reason'
    if ([string]::IsNullOrWhiteSpace($stateReason)) { $canonicalReason } else { $stateReason }
}
else {
    $detail = if ([string]::IsNullOrWhiteSpace($canonicalReason)) { 'Authoritative canonical evidence exists for the current MIM lane, but direct TOD latest artifacts need a refresh before execution can resume.' } else { $canonicalReason }
    "Recoupled latest artifact onto canonical objective $($script:canonicalObjectiveId)/task $($script:canonicalTaskId). Direct TOD execution evidence is not present yet, so refresh is required. Canonical blocker: $detail"
}

$currentAction = if ($directCanonicalEvidenceAvailable) {
    'Republished direct canonical TOD execution evidence onto the latest-artifact lane.'
}
else {
    'Recoupled TOD latest artifacts onto the canonical lane and blocked execution pending a fresh canonical TOD publication.'
}

$nextStep = if ($directCanonicalEvidenceAvailable) {
    'monitor_canonical_execution_progress'
}
else {
    'refresh_canonical_latest_artifacts'
}

$artifactNames = @(
    'TOD_ACTIVE_OBJECTIVE.latest.json',
    'TOD_ACTIVE_TASK.latest.json',
    'TOD_ACTIVITY_STREAM.latest.json',
    'TOD_VALIDATION_RESULT.latest.json',
    'TOD_EXECUTION_RESULT.latest.json',
    'TOD_EXECUTION_TRUTH.latest.json',
    'TOD_NEXT_TASK_SELECTION.latest.json'
)

$timestamp = Get-UtcNowIso
$recoupled = @()
$skipped = @()

foreach ($artifactName in $artifactNames) {
    $artifactPath = Join-Path $sharedRootPath $artifactName
    $currentPayload = Read-JsonFileIfExists -PathValue $artifactPath
    if ($null -eq $currentPayload) {
        $skipped += [pscustomobject]@{ artifact = $artifactName; reason = 'missing' }
        continue
    }

    if (-not (Test-ArtifactNeedsRecoupling -Payload $currentPayload -CanonicalObjectiveId $script:canonicalObjectiveId -CanonicalTaskId $script:canonicalTaskId)) {
        $skipped += [pscustomobject]@{ artifact = $artifactName; reason = 'already_canonical' }
        continue
    }

    $replacementPayload = New-CanonicalRecouplingPayload -ArtifactName $artifactName -Timestamp $timestamp -Summary $summary -TaskTitle $taskTitle -CurrentAction $currentAction -NextStep $nextStep -CanonicalState $canonicalState -CanonicalPhase $canonicalPhase -CanonicalSource (Get-StringProperty -InputObject $sharedTruth -Name 'source') -CanonicalLaneSource $canonicalLaneSource
    $archiveInfo = Write-SupersededArtifactRecord -ArtifactPath $artifactPath -PreviousPayload $currentPayload -ReplacementPayload $replacementPayload -Timestamp $timestamp
    Write-JsonFile -PathValue $artifactPath -Payload $replacementPayload
    $recoupled += [pscustomobject]@{
        artifact = $artifactName
        archived_to = [string]$archiveInfo.record_path
        latest_superseded = [string]$archiveInfo.latest_superseded_path
        replacement_reason_code = 'canonical_latest_artifact_needs_refresh'
    }
}

$reconcileOutput = $null
if (-not $SkipReconcile) {
    if (-not (Test-Path -Path $reconcileScriptAbs)) {
        throw "Reconcile script not found: $reconcileScriptAbs"
    }

    $pythonCandidates = @(
        (Join-Path $repoRoot '.venv/Scripts/python.exe'),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path $_) }

    if (@($pythonCandidates).Count -eq 0) {
        throw 'No Python executable is available to refresh shared truth reconciliation.'
    }

    $pythonExe = [string]$pythonCandidates[0]
    $reconcileRaw = & $pythonExe $reconcileScriptAbs --shared-root $sharedRootPath --integration-path $integrationStatusAbs --output $reconcileOutputAbs
    if ($LASTEXITCODE -ne 0) {
        throw 'Shared truth reconciliation failed after recoupling.'
    }

    $reconcileJoined = ($reconcileRaw | Out-String).Trim()
    if (-not [string]::IsNullOrWhiteSpace($reconcileJoined)) {
        try {
            $reconcileOutput = $reconcileJoined | ConvertFrom-Json
        }
        catch {
            $reconcileOutput = [pscustomobject]@{ raw = $reconcileJoined }
        }
    }
}

$residual0631 = Get-Residual0631Artifacts -RootPath $sharedRootPath
$refreshedSharedTruth = Read-JsonFileIfExists -PathValue $reconcileOutputAbs

$result = [pscustomobject]@{
    generated_at = $timestamp
    source = 'tod-canonical-latest-artifact-recoupling-v1'
    shared_root = $sharedRootPath
    canonical_objective_id = $script:canonicalObjectiveId
    canonical_task_id = $script:canonicalTaskId
    execution_lock = $executionLock
    recoupled_count = @($recoupled).Count
    recoupled = @($recoupled)
    skipped = @($skipped)
    residual_0631 = @($residual0631)
    reconcile_output = $reconcileOutput
    shared_truth = $refreshedSharedTruth
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 20
}
else {
    $result | ConvertTo-Json -Depth 20
}