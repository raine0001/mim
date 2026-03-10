Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot/mim_api_helpers.ps1"

function Get-TodStatePath {
    $todRoot = Split-Path -Path $PSScriptRoot -Parent
    $stateDir = Join-Path $todRoot "state"
    if (-not (Test-Path $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }
    return $stateDir
}

function Read-TodState {
    param([string]$Name)
    $path = Join-Path (Get-TodStatePath) "$Name.json"
    if (-not (Test-Path $path)) {
        return @()
    }
    return Get-Content -Raw -Path $path | ConvertFrom-Json
}

function Write-TodState {
    param(
        [string]$Name,
        [object]$Value
    )
    $path = Join-Path (Get-TodStatePath) "$Name.json"
    $Value | ConvertTo-Json -Depth 10 | Set-Content -Path $path
}

function Add-TodStateItem {
    param(
        [string]$Name,
        [pscustomobject]$Item
    )
    $existing = @(Read-TodState -Name $Name)
    $updated = @($existing + $Item)
    Write-TodState -Name $Name -Value $updated
    return $Item
}

function Resolve-TodMode {
    param([string]$Mode)
    if (-not $Mode) { return "hybrid" }
    return $Mode.ToLowerInvariant()
}

function Get-TodExecutionConfig {
    param([string]$ConfigPath)

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $activeEngine = "local"
    $fallbackEngine = "local"

    if ($null -ne $config.execution) {
        if ($config.execution.active_engine) {
            $activeEngine = $config.execution.active_engine
        }
        if ($config.execution.fallback_engine) {
            $fallbackEngine = $config.execution.fallback_engine
        }
    }

    return [pscustomobject]@{
        active_engine = $activeEngine.ToLowerInvariant()
        fallback_engine = $fallbackEngine.ToLowerInvariant()
    }
}

function New-ExecutionResultEnvelope {
    param(
        [string]$Engine,
        [bool]$Success,
        [string]$Summary,
        [string]$RawOutput,
        [string[]]$FilesChanged = @(),
        [string[]]$TestsRun = @(),
        [string]$TestResults = "",
        [string[]]$Failures = @(),
        [string]$Recommendations = "",
        [bool]$NeedsEscalation = $false,
        [hashtable]$ExecutionMetadata = @{}
    )

    return [pscustomobject]@{
        execution_engine = $Engine
        success = $Success
        summary = $Summary
        raw_output = $RawOutput
        files_changed = $FilesChanged
        tests_run = $TestsRun
        test_results = $TestResults
        failures = $Failures
        recommendations = $Recommendations
        needs_escalation = $NeedsEscalation
        execution_metadata = [pscustomobject]$ExecutionMetadata
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
    }
}

function Invoke-CodexExecutionEngine {
    param(
        [string]$PackagePath,
        [pscustomobject]$TaskMetadata
    )

    throw "CodexExecutionEngine is not yet configured in this TOD environment"
}

function Invoke-LocalExecutionEngine {
    param(
        [string]$PackagePath,
        [pscustomobject]$TaskMetadata
    )

    $contentPreview = (Get-Content -Path $PackagePath -TotalCount 20 -ErrorAction SilentlyContinue) -join "`n"
    if (-not $contentPreview) {
        $contentPreview = "<empty package>"
    }

    return New-ExecutionResultEnvelope `
        -Engine "local" `
        -Success $true `
        -Summary "LocalExecutionEngine placeholder processed package input" `
        -RawOutput $contentPreview `
        -FilesChanged @() `
        -TestsRun @() `
        -TestResults "not-run" `
        -Failures @() `
        -Recommendations "Wire concrete local executor in Task 31/32" `
        -NeedsEscalation $false `
        -ExecutionMetadata @{
            package_path = $PackagePath
            task_id = $TaskMetadata.task_id
            objective_id = $TaskMetadata.objective_id
        }
}

function Invoke-SelectedExecutionEngine {
    param(
        [string]$Engine,
        [string]$PackagePath,
        [pscustomobject]$TaskMetadata
    )

    switch ($Engine.ToLowerInvariant()) {
        "codex" { return Invoke-CodexExecutionEngine -PackagePath $PackagePath -TaskMetadata $TaskMetadata }
        "local" { return Invoke-LocalExecutionEngine -PackagePath $PackagePath -TaskMetadata $TaskMetadata }
        default { throw "Unsupported execution engine: $Engine" }
    }
}

function Invoke-ExecutionEngine {
    param(
        [string]$PackagePath,
        [pscustomobject]$TaskMetadata,
        [string]$ConfigPath
    )

    if (-not (Test-Path $PackagePath)) {
        throw "Package path not found: $PackagePath"
    }

    $engineConfig = Get-TodExecutionConfig -ConfigPath $ConfigPath
    Write-TodApiLog -Message "Invoke-ExecutionEngine active=$($engineConfig.active_engine) fallback=$($engineConfig.fallback_engine) package=$PackagePath"

    try {
        return Invoke-SelectedExecutionEngine -Engine $engineConfig.active_engine -PackagePath $PackagePath -TaskMetadata $TaskMetadata
    }
    catch {
        $activeError = $_.Exception.Message
        Write-TodApiLog -Level "WARN" -Message "Active engine failed: $activeError"

        if ($engineConfig.fallback_engine -and $engineConfig.fallback_engine -ne $engineConfig.active_engine) {
            try {
                $fallbackResult = Invoke-SelectedExecutionEngine -Engine $engineConfig.fallback_engine -PackagePath $PackagePath -TaskMetadata $TaskMetadata
                $fallbackResult.execution_metadata | Add-Member -NotePropertyName fallback_from -NotePropertyValue $engineConfig.active_engine -Force
                return $fallbackResult
            }
            catch {
                $fallbackError = $_.Exception.Message
                return New-ExecutionResultEnvelope `
                    -Engine $engineConfig.active_engine `
                    -Success $false `
                    -Summary "Execution failed on active and fallback engines" `
                    -RawOutput "active_error=$activeError; fallback_error=$fallbackError" `
                    -Failures @($activeError, $fallbackError) `
                    -Recommendations "Check engine configuration and credentials" `
                    -NeedsEscalation $true `
                    -ExecutionMetadata @{
                        active_engine = $engineConfig.active_engine
                        fallback_engine = $engineConfig.fallback_engine
                        package_path = $PackagePath
                        task_id = $TaskMetadata.task_id
                    }
            }
        }

        return New-ExecutionResultEnvelope `
            -Engine $engineConfig.active_engine `
            -Success $false `
            -Summary "Execution failed on active engine and no fallback available" `
            -RawOutput $activeError `
            -Failures @($activeError) `
            -Recommendations "Configure fallback engine or fix active engine" `
            -NeedsEscalation $true `
            -ExecutionMetadata @{
                active_engine = $engineConfig.active_engine
                fallback_engine = $engineConfig.fallback_engine
                package_path = $PackagePath
                task_id = $TaskMetadata.task_id
            }
    }
}

function Get-MimManifest {
    param([string]$ConfigPath)

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    if ($mode -eq "local") {
        return [pscustomobject]@{
            unavailable = $true
            reason = "local mode"
        }
    }

    try {
        return Invoke-MimGet -Endpoint "/manifest" -Config $config
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Manifest fetch failed: $($_.Exception.Message)"
            return [pscustomobject]@{
                unavailable = $true
                reason = "fetch_failed"
                error = $_.Exception.Message
            }
        }
        throw
    }
}

function Sync-Mim {
    param(
        [string]$ConfigPath,
        [string]$ExpectedContractVersion = "tod-mim-shared-contract-v1",
        [string]$ExpectedSchemaVersion = "2026-03-09-01",
        [string[]]$ExpectedCapabilities = @("health", "status", "manifest", "objectives", "tasks", "results", "reviews", "journal")
    )

    $manifest = Get-MimManifest -ConfigPath $ConfigPath
    if ($manifest.unavailable) {
        return [pscustomobject]@{
            ok = $false
            decision = "warn"
            message = "Manifest unavailable"
            reason = $manifest.reason
            error = $manifest.error
        }
    }

    $missingCapabilities = @()
    foreach ($cap in $ExpectedCapabilities) {
        if ($manifest.capabilities -notcontains $cap) {
            $missingCapabilities += $cap
        }
    }

    $contractCompatible = ($manifest.contract_version -eq $ExpectedContractVersion)
    $schemaMatch = ($manifest.schema_version -eq $ExpectedSchemaVersion)
    $capabilitiesMatch = ($missingCapabilities.Count -eq 0)

    $decision = "ok"
    if (-not $contractCompatible) {
        $decision = "escalate"
    }
    elseif (-not $schemaMatch -or -not $capabilitiesMatch) {
        $decision = "warn"
    }

    return [pscustomobject]@{
        ok = ($decision -eq "ok")
        decision = $decision
        expected_contract_version = $ExpectedContractVersion
        live_contract_version = $manifest.contract_version
        expected_schema_version = $ExpectedSchemaVersion
        live_schema_version = $manifest.schema_version
        repo_signature = $manifest.repo_signature
        last_updated_at = $manifest.last_updated_at
        capabilities_match = $capabilitiesMatch
        missing_capabilities = $missingCapabilities
        recommended_action = $(
            if ($decision -eq "escalate") { "Stop sync and escalate: incompatible contract version" }
            elseif ($decision -eq "warn") { "Proceed carefully and re-index/cache refresh recommended" }
            else { "Sync safe" }
        )
        manifest = $manifest
    }
}

function Ping-Mim {
    param([string]$ConfigPath)
    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    if ($mode -eq "local") {
        return [pscustomobject]@{
            mode = "local"
            available = $false
            message = "MIM disabled in local mode"
        }
    }

    $start = Get-Date
    $probe = Test-MimAvailable -Config $config
    $latencyMs = [Math]::Round(((Get-Date) - $start).TotalMilliseconds, 2)

    if ($probe.available) {
        return [pscustomobject]@{
            mode = $mode
            available = $true
            message = "MIM reachable"
            status = "running"
            latency_ms = $latencyMs
            health = $probe.health
            server = $probe.status
        }
    }

    return [pscustomobject]@{
        mode = $mode
        available = $false
        message = "MIM unavailable"
        status = "down"
        latency_ms = $latencyMs
        error = $probe.error
    }
}

function New-MimObjective {
    param(
        [string]$Title,
        [string]$Description = "",
        [string]$Priority = "normal",
        [string[]]$Constraints = @(),
        [string]$SuccessCriteria = "",
        [string]$Status = "new",
        [string]$ConfigPath
    )

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    $payload = @{
        title = $Title
        description = $Description
        priority = $Priority
        constraints = $Constraints
        success_criteria = $SuccessCriteria
        status = $Status
    }

    if ($mode -eq "remote") {
        return Invoke-MimPost -Endpoint "/objectives" -Payload $payload -Config $config
    }

    if ($mode -eq "local") {
        $local = [pscustomobject]@{
            objective_id = [int](Get-Date -UFormat %s)
            title = $Title
            description = $Description
            priority = $Priority
            constraints = $Constraints
            success_criteria = $SuccessCriteria
            status = $Status
            created_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        return Add-TodStateItem -Name "objectives" -Item $local
    }

    try {
        $remote = Invoke-MimPost -Endpoint "/objectives" -Payload $payload -Config $config
        Add-TodStateItem -Name "objectives" -Item $remote | Out-Null
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote objective create failed; using local fallback"
            $local = [pscustomobject]@{
                objective_id = [int](Get-Date -UFormat %s)
                title = $Title
                description = $Description
                priority = $Priority
                constraints = $Constraints
                success_criteria = $SuccessCriteria
                status = $Status
                created_at = (Get-Date).ToUniversalTime().ToString("o")
                source = "local-fallback"
            }
            return Add-TodStateItem -Name "objectives" -Item $local
        }
        throw
    }
}

function Get-MimObjectives {
    param([string]$ConfigPath)
    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    if ($mode -eq "local") {
        return @(Read-TodState -Name "objectives")
    }

    if ($mode -eq "remote") {
        return @(Invoke-MimGet -Endpoint "/objectives" -Config $config)
    }

    try {
        $remote = @(Invoke-MimGet -Endpoint "/objectives" -Config $config)
        Write-TodState -Name "objectives" -Value $remote
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote objectives read failed; using local fallback"
            return @(Read-TodState -Name "objectives")
        }
        throw
    }
}

function New-MimTask {
    param(
        [int]$ObjectiveId,
        [string]$Title,
        [string]$Scope = "",
        [int[]]$Dependencies = @(),
        [string]$AcceptanceCriteria = "",
        [string]$Status = "queued",
        [string]$AssignedTo = "tod",
        [string]$ConfigPath
    )

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    $payload = @{
        objective_id = $ObjectiveId
        title = $Title
        scope = $Scope
        dependencies = $Dependencies
        acceptance_criteria = $AcceptanceCriteria
        status = $Status
        assigned_to = $AssignedTo
    }

    if ($mode -eq "remote") {
        return Invoke-MimPost -Endpoint "/tasks" -Payload $payload -Config $config
    }

    if ($mode -eq "local") {
        $local = [pscustomobject]@{
            task_id = [int](Get-Date -UFormat %s)
            objective_id = $ObjectiveId
            title = $Title
            scope = $Scope
            dependencies = $Dependencies
            acceptance_criteria = $AcceptanceCriteria
            status = $Status
            assigned_to = $AssignedTo
            created_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        return Add-TodStateItem -Name "tasks" -Item $local
    }

    try {
        $remote = Invoke-MimPost -Endpoint "/tasks" -Payload $payload -Config $config
        Add-TodStateItem -Name "tasks" -Item $remote | Out-Null
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote task create failed; using local fallback"
            $local = [pscustomobject]@{
                task_id = [int](Get-Date -UFormat %s)
                objective_id = $ObjectiveId
                title = $Title
                scope = $Scope
                dependencies = $Dependencies
                acceptance_criteria = $AcceptanceCriteria
                status = $Status
                assigned_to = $AssignedTo
                created_at = (Get-Date).ToUniversalTime().ToString("o")
                source = "local-fallback"
            }
            return Add-TodStateItem -Name "tasks" -Item $local
        }
        throw
    }
}

function Get-MimTasks {
    param([string]$ConfigPath)
    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    if ($mode -eq "local") {
        return @(Read-TodState -Name "tasks")
    }

    if ($mode -eq "remote") {
        return @(Invoke-MimGet -Endpoint "/tasks" -Config $config)
    }

    try {
        $remote = @(Invoke-MimGet -Endpoint "/tasks" -Config $config)
        Write-TodState -Name "tasks" -Value $remote
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote tasks read failed; using local fallback"
            return @(Read-TodState -Name "tasks")
        }
        throw
    }
}

function New-MimResult {
    param(
        [int]$TaskId,
        [string]$Summary,
        [string[]]$FilesChanged = @(),
        [string[]]$TestsRun = @(),
        [string]$TestResults = "",
        [string[]]$Failures = @(),
        [string]$Recommendations = "",
        [string]$ConfigPath
    )

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    $payload = @{
        task_id = $TaskId
        summary = $Summary
        files_changed = $FilesChanged
        tests_run = $TestsRun
        test_results = $TestResults
        failures = $Failures
        recommendations = $Recommendations
    }

    if ($mode -eq "remote") {
        return Invoke-MimPost -Endpoint "/results" -Payload $payload -Config $config
    }

    if ($mode -eq "local") {
        $local = [pscustomobject]@{
            result_id = [int](Get-Date -UFormat %s)
            task_id = $TaskId
            summary = $Summary
            files_changed = $FilesChanged
            tests_run = $TestsRun
            test_results = $TestResults
            failures = $Failures
            recommendations = $Recommendations
            created_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        return Add-TodStateItem -Name "results" -Item $local
    }

    try {
        $remote = Invoke-MimPost -Endpoint "/results" -Payload $payload -Config $config
        Add-TodStateItem -Name "results" -Item $remote | Out-Null
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote result create failed; using local fallback"
            $local = [pscustomobject]@{
                result_id = [int](Get-Date -UFormat %s)
                task_id = $TaskId
                summary = $Summary
                files_changed = $FilesChanged
                tests_run = $TestsRun
                test_results = $TestResults
                failures = $Failures
                recommendations = $Recommendations
                created_at = (Get-Date).ToUniversalTime().ToString("o")
                source = "local-fallback"
            }
            return Add-TodStateItem -Name "results" -Item $local
        }
        throw
    }
}

function New-MimReview {
    param(
        [int]$TaskId,
        [string]$Decision,
        [string]$Rationale = "",
        [bool]$ContinueAllowed = $false,
        [bool]$EscalateToUser = $false,
        [string]$ConfigPath
    )

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    $payload = @{
        task_id = $TaskId
        decision = $Decision
        rationale = $Rationale
        continue_allowed = $ContinueAllowed
        escalate_to_user = $EscalateToUser
    }

    if ($mode -eq "remote") {
        return Invoke-MimPost -Endpoint "/reviews" -Payload $payload -Config $config
    }

    if ($mode -eq "local") {
        $local = [pscustomobject]@{
            review_id = [int](Get-Date -UFormat %s)
            task_id = $TaskId
            decision = $Decision
            rationale = $Rationale
            continue_allowed = $ContinueAllowed
            escalate_to_user = $EscalateToUser
            created_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        return Add-TodStateItem -Name "reviews" -Item $local
    }

    try {
        $remote = Invoke-MimPost -Endpoint "/reviews" -Payload $payload -Config $config
        Add-TodStateItem -Name "reviews" -Item $remote | Out-Null
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote review create failed; using local fallback"
            $local = [pscustomobject]@{
                review_id = [int](Get-Date -UFormat %s)
                task_id = $TaskId
                decision = $Decision
                rationale = $Rationale
                continue_allowed = $ContinueAllowed
                escalate_to_user = $EscalateToUser
                created_at = (Get-Date).ToUniversalTime().ToString("o")
                source = "local-fallback"
            }
            return Add-TodStateItem -Name "reviews" -Item $local
        }
        throw
    }
}

function Get-MimJournal {
    param([string]$ConfigPath)
    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode

    if ($mode -eq "local") {
        return @(Read-TodState -Name "journal")
    }

    if ($mode -eq "remote") {
        return @(Invoke-MimGet -Endpoint "/journal" -Config $config)
    }

    try {
        $remote = @(Invoke-MimGet -Endpoint "/journal" -Config $config)
        Write-TodState -Name "journal" -Value $remote
        return $remote
    }
    catch {
        if ($config.fallback_to_local) {
            Write-TodApiLog -Level "WARN" -Message "Remote journal read failed; using local fallback"
            return @(Read-TodState -Name "journal")
        }
        throw
    }
}
