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

    $hasExecution = $config.PSObject.Properties.Name -contains "execution"
    if ($hasExecution -and $null -ne $config.execution) {
        if (($config.execution.PSObject.Properties.Name -contains "active_engine") -and $config.execution.active_engine) {
            $activeEngine = $config.execution.active_engine
        }
        if (($config.execution.PSObject.Properties.Name -contains "fallback_engine") -and $config.execution.fallback_engine) {
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

function ConvertTo-StringArray {
    param([object]$Value)

    if ($null -eq $Value) { return @() }
    if ($Value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
        return @($Value)
    }

    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @()
        foreach ($item in $Value) {
            if ($null -ne $item -and -not [string]::IsNullOrWhiteSpace([string]$item)) {
                $items += [string]$item
            }
        }
        return $items
    }

    return @([string]$Value)
}

function Normalize-ExecutionResult {
    param(
        [object]$RawResult,
        [string]$EngineName
    )

    if ($null -eq $RawResult) {
        return New-ExecutionResultEnvelope `
            -Engine $EngineName `
            -Success $false `
            -Summary "Execution engine returned null result" `
            -RawOutput "" `
            -Failures @("null_result") `
            -Recommendations "Ensure engine wrapper returns an execution result object" `
            -NeedsEscalation $true
    }

    $summary = [string]$RawResult.summary
    if ([string]::IsNullOrWhiteSpace($summary)) {
        $summary = "Execution completed with no summary"
    }

    $rawOutput = [string]$RawResult.raw_output
    if ([string]::IsNullOrWhiteSpace($rawOutput)) {
        $rawOutput = ""
    }

    $success = $false
    if ($null -ne $RawResult.success) {
        $success = [bool]$RawResult.success
    }

    $testResults = [string]$RawResult.test_results
    if ([string]::IsNullOrWhiteSpace($testResults)) {
        $testResults = "not-run"
    }

    $recommendations = [string]$RawResult.recommendations
    if ([string]::IsNullOrWhiteSpace($recommendations)) {
        $recommendations = ""
    }

    $engine = [string]$RawResult.execution_engine
    if ([string]::IsNullOrWhiteSpace($engine)) {
        $engine = $EngineName
    }

    $metadata = @{}
    if ($null -ne $RawResult.execution_metadata) {
        $metadata = @{}
        $RawResult.execution_metadata.PSObject.Properties | ForEach-Object {
            $metadata[$_.Name] = $_.Value
        }
    }

    return New-ExecutionResultEnvelope `
        -Engine $engine `
        -Success $success `
        -Summary $summary `
        -RawOutput $rawOutput `
        -FilesChanged (ConvertTo-StringArray -Value $RawResult.files_changed) `
        -TestsRun (ConvertTo-StringArray -Value $RawResult.tests_run) `
        -TestResults $testResults `
        -Failures (ConvertTo-StringArray -Value $RawResult.failures) `
        -Recommendations $recommendations `
        -NeedsEscalation ([bool]$RawResult.needs_escalation) `
        -ExecutionMetadata $metadata
}

function Invoke-ReviewPrecheck {
    param([pscustomobject]$ResultEnvelope)

    $blockingIssues = @()

    if (-not $ResultEnvelope.success) {
        $blockingIssues += "execution_not_successful"
    }

    $failureCount = 0
    if ($null -ne $ResultEnvelope.failures) {
        $failureCount = @($ResultEnvelope.failures | Where-Object { $null -ne $_ -and -not [string]::IsNullOrWhiteSpace([string]$_) }).Count
    }
    if ($failureCount -gt 0) {
        $blockingIssues += "execution_failures_present"
    }

    if ([string]::IsNullOrWhiteSpace($ResultEnvelope.summary)) {
        $blockingIssues += "missing_summary"
    }

    if ($ResultEnvelope.test_results -eq "fail") {
        $blockingIssues += "tests_failed"
    }

    if ($ResultEnvelope.needs_escalation) {
        $blockingIssues += "needs_escalation_flag"
    }

    return [pscustomobject]@{
        ready_for_review = ($blockingIssues.Count -eq 0)
        blocking_issues = $blockingIssues
    }
}

function Finalize-ExecutionResult {
    param(
        [object]$RawResult,
        [string]$EngineName
    )

    $normalized = Normalize-ExecutionResult -RawResult $RawResult -EngineName $EngineName
    $precheck = Invoke-ReviewPrecheck -ResultEnvelope $normalized
    $normalized | Add-Member -NotePropertyName review_precheck -NotePropertyValue $precheck -Force

    if (-not $precheck.ready_for_review) {
        $normalized.needs_escalation = $true
    }

    return $normalized
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

function Resolve-FailureCategory {
    param([pscustomobject]$ResultEnvelope)

    if ($null -eq $ResultEnvelope) { return "execution_error" }

    if ($null -ne $ResultEnvelope.failures -and $ResultEnvelope.failures.Count -gt 0) {
        $first = [string]$ResultEnvelope.failures[0]
        if ($first -match "timeout|timed out") { return "timeout" }
        if ($first -match "contract|schema|incompatible") { return "contract_drift_breaking" }
        if ($first -match "validation|invalid") { return "validation_failure" }
        if ($first -match "unsupported execution engine|no eligible") { return "no_eligible_engine" }
        return "execution_error"
    }

    if ($null -ne $ResultEnvelope.review_precheck -and -not [bool]$ResultEnvelope.review_precheck.ready_for_review) {
        return "review_rejection"
    }

    if (-not [bool]$ResultEnvelope.success) {
        return "execution_error"
    }

    return ""
}

function Resolve-ResultCategory {
    param([pscustomobject]$ResultEnvelope)

    if ($null -eq $ResultEnvelope) { return "failure" }
    if ([bool]$ResultEnvelope.success -and -not [bool]$ResultEnvelope.needs_escalation) { return "success" }
    if ([bool]$ResultEnvelope.success -and [bool]$ResultEnvelope.needs_escalation) { return "success_with_escalation" }
    return "failure"
}

function Resolve-RecoveryQuality {
    param(
        [pscustomobject]$ResultEnvelope,
        [bool]$FallbackUsed,
        [string]$SelectionReason
    )

    if ($null -eq $ResultEnvelope) {
        return "manual_intervention"
    }

    if ($null -ne $ResultEnvelope.review_precheck -and -not [bool]$ResultEnvelope.review_precheck.ready_for_review) {
        return "guardrail_block"
    }

    if (-not [bool]$ResultEnvelope.success) {
        return "unrecovered_failure"
    }

    if ($FallbackUsed) {
        return "recovered_on_fallback"
    }

    $retryCount = 0
    if ($null -ne $ResultEnvelope.execution_metadata -and ($ResultEnvelope.execution_metadata.PSObject.Properties.Name -contains "retry_count")) {
        $retryCount = [int]$ResultEnvelope.execution_metadata.retry_count
    }
    if ($retryCount -gt 0 -or $SelectionReason -match "retry") {
        return "recovered_on_retry"
    }

    if ([bool]$ResultEnvelope.needs_escalation) {
        return "manual_intervention"
    }

    return "clean_success"
}

function Get-RoutingMetricWindow {
    param(
        [string]$ConfigPath,
        [string]$EngineName,
        [int]$Window
    )

    $result = [pscustomobject]@{
        runs = 0
        pass_rate = 0.0
        fallback_rate = 0.0
    }

    try {
        $config = Get-TodConfig -ConfigPath $ConfigPath
        $resp = Invoke-MimGet -Endpoint "/routing/engines?window=$Window" -Config $config
        if ($null -ne $resp.engine_metrics) {
            $metric = $resp.engine_metrics.PSObject.Properties[$EngineName]
            if ($null -ne $metric) {
                $data = $metric.Value
                $result.runs = [int]$data.runs
                $result.pass_rate = [double]$data.pass_rate
                $result.fallback_rate = [double]$data.fallback_rate
            }
        }
    }
    catch {
        Write-TodApiLog -Level "WARN" -Message "Could not load /routing/engines window=${Window}: $($_.Exception.Message)"
    }

    return $result
}

function Get-RoutingMetric7d {
    param(
        [string]$ConfigPath,
        [string]$EngineName
    )

    $result = [pscustomobject]@{
        runs = 0
        pass_rate = 0.0
        fallback_rate = 0.0
    }

    try {
        $config = Get-TodConfig -ConfigPath $ConfigPath
        $resp = Invoke-MimGet -Endpoint "/routing/history?limit=500" -Config $config
        $cutoff = (Get-Date).ToUniversalTime().AddDays(-7)
        $rows = @($resp | Where-Object {
            $_.selected_engine -eq $EngineName -and
            $null -ne $_.timestamp -and
            ([datetime]$_.timestamp).ToUniversalTime() -ge $cutoff
        })

        $runs = $rows.Count
        if ($runs -gt 0) {
            $passes = @($rows | Where-Object { $_.routing_final_outcome -eq "success" }).Count
            $fallbacks = @($rows | Where-Object { [bool]$_.fallback_used }).Count
            $result.runs = $runs
            $result.pass_rate = [double]($passes / $runs)
            $result.fallback_rate = [double]($fallbacks / $runs)
        }
    }
    catch {
        Write-TodApiLog -Level "WARN" -Message "Could not derive 7-day routing metrics: $($_.Exception.Message)"
    }

    return $result
}

function Get-RoutingConfidenceWeights {
    param([string]$ConfigPath)

    $weights = [pscustomobject]@{
        success_rate = 0.5
        failure_penalty = 0.3
        fallback_penalty = 0.1
        sample_size = 0.1
    }

    try {
        $config = Get-TodConfig -ConfigPath $ConfigPath
        if ($null -ne $config.routing_confidence_weights) {
            $source = $config.routing_confidence_weights
            if ($source.PSObject.Properties.Name -contains "success_rate") { $weights.success_rate = [double]$source.success_rate }
            if ($source.PSObject.Properties.Name -contains "failure_penalty") { $weights.failure_penalty = [double]$source.failure_penalty }
            if ($source.PSObject.Properties.Name -contains "fallback_penalty") { $weights.fallback_penalty = [double]$source.fallback_penalty }
            if ($source.PSObject.Properties.Name -contains "sample_size") { $weights.sample_size = [double]$source.sample_size }
        }
    }
    catch {
        Write-TodApiLog -Level "WARN" -Message "Could not parse routing_confidence_weights; using defaults"
    }

    return $weights
}

function Publish-RoutingMetric {
    param(
        [string]$ConfigPath,
        [pscustomobject]$TaskMetadata,
        [pscustomobject]$EngineConfig,
        [pscustomobject]$FinalResult,
        [double]$LatencyMs,
        [string]$SelectionReason
    )

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $mode = Resolve-TodMode -Mode $config.mode
    if ($mode -eq "local") { return }

    $fallbackUsed = $false
    if ($null -ne $FinalResult.execution_metadata) {
        $hasFallbackFrom = $FinalResult.execution_metadata.PSObject.Properties.Name -contains "fallback_from"
        if ($hasFallbackFrom) {
            $fallbackUsed = $true
        }
    }

    $reviewOutcome = "unknown"
    if ($null -ne $FinalResult.review_precheck) {
        if ([bool]$FinalResult.review_precheck.ready_for_review) {
            $reviewOutcome = "pass"
        }
        else {
            $reviewOutcome = "fail"
        }
    }

    $w20 = Get-RoutingMetricWindow -ConfigPath $ConfigPath -EngineName ([string]$FinalResult.execution_engine) -Window 20
    $w50 = Get-RoutingMetricWindow -ConfigPath $ConfigPath -EngineName ([string]$FinalResult.execution_engine) -Window 50
    $w7d = Get-RoutingMetric7d -ConfigPath $ConfigPath -EngineName ([string]$FinalResult.execution_engine)

    $engineSuccessRate = ($w20.pass_rate * 0.5) + ($w50.pass_rate * 0.35) + ($w7d.pass_rate * 0.15)
    $fallbackPenalty = ($w20.fallback_rate * 0.5) + ($w50.fallback_rate * 0.35) + ($w7d.fallback_rate * 0.15)
    $failurePenalty = 1.0 - $engineSuccessRate

    $runs50 = [Math]::Max(0, [int]$w50.runs)
    $sampleSizeWeight = [Math]::Min(1.0, $runs50 / 50.0)

    $weights = Get-RoutingConfidenceWeights -ConfigPath $ConfigPath

    $routingConfidence =
        ($engineSuccessRate * $weights.success_rate) -
        ($failurePenalty * $weights.failure_penalty) -
        ($fallbackPenalty * $weights.fallback_penalty) +
        ($sampleSizeWeight * $weights.sample_size)

    if (-not [bool]$FinalResult.success) {
        $routingConfidence = $routingConfidence - 0.15
    }
    if ($routingConfidence -lt 0.0) { $routingConfidence = 0.0 }
    if ($routingConfidence -gt 1.0) { $routingConfidence = 1.0 }
    $routingConfidence = [Math]::Round($routingConfidence, 4)

    $recoveryQuality = Resolve-RecoveryQuality -ResultEnvelope $FinalResult -FallbackUsed $fallbackUsed -SelectionReason $SelectionReason

    $policyVersion = "routing-policy-v1"
    if ($null -ne $config.routing_policy_version -and -not [string]::IsNullOrWhiteSpace([string]$config.routing_policy_version)) {
        $policyVersion = [string]$config.routing_policy_version
    }

    $engineVersion = "unknown"
    if ($null -ne $FinalResult.execution_metadata -and ($FinalResult.execution_metadata.PSObject.Properties.Name -contains "engine_version")) {
        $engineVersion = [string]$FinalResult.execution_metadata.engine_version
    }

    $payload = @{
        task_id = $TaskMetadata.task_id
        objective_id = $TaskMetadata.objective_id
        selected_engine = [string]$FinalResult.execution_engine
        fallback_engine = [string]$EngineConfig.fallback_engine
        fallback_used = $fallbackUsed
        routing_source = "tod.invoke-engine"
        routing_confidence = $routingConfidence
        policy_version = $policyVersion
        engine_version = $engineVersion
        routing_selection_reason = $SelectionReason
        routing_final_outcome = $(if ([bool]$FinalResult.success) { "success" } else { "fail" })
        latency_ms = [int][Math]::Round($LatencyMs, 0)
        result_category = Resolve-ResultCategory -ResultEnvelope $FinalResult
        failure_category = Resolve-FailureCategory -ResultEnvelope $FinalResult
        review_outcome = $reviewOutcome
        blocked_pre_invocation = $false
        metadata_json = @{
            tests_run = $FinalResult.tests_run
            test_results = $FinalResult.test_results
            needs_escalation = $FinalResult.needs_escalation
            failures = $FinalResult.failures
            recovery_quality = $recoveryQuality
            confidence_inputs = @{
                windows = @{
                    last_20_runs = @{ runs = $w20.runs; pass_rate = [Math]::Round($w20.pass_rate, 4); fallback_rate = [Math]::Round($w20.fallback_rate, 4) }
                    last_50_runs = @{ runs = $w50.runs; pass_rate = [Math]::Round($w50.pass_rate, 4); fallback_rate = [Math]::Round($w50.fallback_rate, 4) }
                    last_7_days = @{ runs = $w7d.runs; pass_rate = [Math]::Round($w7d.pass_rate, 4); fallback_rate = [Math]::Round($w7d.fallback_rate, 4) }
                }
                weights = @{ 
                    success_rate = $weights.success_rate
                    failure_penalty = $weights.failure_penalty
                    fallback_penalty = $weights.fallback_penalty
                    sample_size = $weights.sample_size
                }
                computed = @{
                    success_rate = [Math]::Round($engineSuccessRate, 4)
                    failure_penalty = [Math]::Round($failurePenalty, 4)
                    fallback_penalty = [Math]::Round($fallbackPenalty, 4)
                    sample_size_weight = [Math]::Round($sampleSizeWeight, 4)
                }
            }
        }
    }

    try {
        Invoke-MimPost -Endpoint "/routing/history" -Payload $payload -Config $config | Out-Null
    }
    catch {
        Write-TodApiLog -Level "WARN" -Message "Failed to publish routing metric: $($_.Exception.Message)"
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

    $startedAt = Get-Date
    $selectionReason = "active engine from config"
    $finalResult = $null

    try {
        $activeResult = Invoke-SelectedExecutionEngine -Engine $engineConfig.active_engine -PackagePath $PackagePath -TaskMetadata $TaskMetadata
        $finalResult = Finalize-ExecutionResult -RawResult $activeResult -EngineName $engineConfig.active_engine
    }
    catch {
        $activeError = $_.Exception.Message
        Write-TodApiLog -Level "WARN" -Message "Active engine failed: $activeError"

        if ($engineConfig.fallback_engine -and $engineConfig.fallback_engine -ne $engineConfig.active_engine) {
            try {
                $fallbackResult = Invoke-SelectedExecutionEngine -Engine $engineConfig.fallback_engine -PackagePath $PackagePath -TaskMetadata $TaskMetadata
                $fallbackResult.execution_metadata | Add-Member -NotePropertyName fallback_from -NotePropertyValue $engineConfig.active_engine -Force
                $selectionReason = "fallback selected after active failure: $activeError"
                $finalResult = Finalize-ExecutionResult -RawResult $fallbackResult -EngineName $engineConfig.fallback_engine
            }
            catch {
                $fallbackError = $_.Exception.Message
                $combinedFailure = New-ExecutionResultEnvelope `
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
                $selectionReason = "active and fallback failed"
                $finalResult = Finalize-ExecutionResult -RawResult $combinedFailure -EngineName $engineConfig.active_engine
            }
        }
        else {
            $singleFailure = New-ExecutionResultEnvelope `
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
            $selectionReason = "active engine failed with no fallback"
            $finalResult = Finalize-ExecutionResult -RawResult $singleFailure -EngineName $engineConfig.active_engine
        }
    }

    $latencyMs = ((Get-Date) - $startedAt).TotalMilliseconds
    Publish-RoutingMetric -ConfigPath $ConfigPath -TaskMetadata $TaskMetadata -EngineConfig $engineConfig -FinalResult $finalResult -LatencyMs $latencyMs -SelectionReason $selectionReason

    return $finalResult
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

function Get-TodReliabilityState {
    param([string]$ConfigPath)

    $config = Get-TodConfig -ConfigPath $ConfigPath
    $todRoot = Split-Path -Path $PSScriptRoot -Parent
    $repoRoot = Split-Path -Path $todRoot -Parent

    $summaryPath = Join-Path $repoRoot "tod-tests-summary.json"
    $historyPath = Join-Path $todRoot "history/tod-tests-history.json"
    $dashboardPath = Join-Path $todRoot "history/reliability-dashboard.json"

    $summary = $null
    $history = @()
    $dashboard = $null

    if (Test-Path $summaryPath) {
        try {
            $summary = Get-Content -Raw -Path $summaryPath | ConvertFrom-Json
        }
        catch {
            Write-TodApiLog -Level "WARN" -Message "Could not parse tod-tests-summary.json"
        }
    }

    if (Test-Path $historyPath) {
        try {
            $history = @(Get-Content -Raw -Path $historyPath | ConvertFrom-Json)
        }
        catch {
            Write-TodApiLog -Level "WARN" -Message "Could not parse tod-tests-history.json"
            $history = @()
        }
    }

    if (Test-Path $dashboardPath) {
        try {
            $dashboard = Get-Content -Raw -Path $dashboardPath | ConvertFrom-Json
        }
        catch {
            Write-TodApiLog -Level "WARN" -Message "Could not parse reliability-dashboard.json"
        }
    }

    $latestHistory = $null
    if ($history.Count -gt 0) {
        $latestHistory = $history[-1]
    }

    return [pscustomobject]@{
        contract_version = "tod-reliability-signals-v1"
        schema_version = "2026-03-10-01"
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        mode = (Resolve-TodMode -Mode $config.mode)
        capabilities = @(
            "reliability_state_export",
            "reliability_trend_history",
            "routing_confidence_windowed_weights",
            "recovery_quality_categorization",
            "mim_integration_ready_preview"
        )
        paths = [pscustomobject]@{
            summary = $summaryPath
            history = $historyPath
            dashboard = $dashboardPath
        }
        latest_snapshot = $summary
        latest_history_entry = $latestHistory
        dashboard = $dashboard
        trend_window = [pscustomobject]@{
            history_entries = $history.Count
            last_20_runs_available = ($history.Count -ge 20)
            last_50_runs_available = ($history.Count -ge 50)
            last_7_days_hint = "Derived from routing metric timestamps when available"
        }
        integration_contract_for_mim = [pscustomobject]@{
            planned_endpoint = "tod.reliability-state"
            payload_version = "v1"
            required_fields = @(
                "timestamp",
                "commit_sha",
                "pass_count",
                "fail_count",
                "retry_count",
                "guardrail_blocks",
                "engine_stats"
            )
        }
    }
}
