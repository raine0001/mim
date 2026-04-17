Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-TodConfig {
    param(
        [string]$ConfigPath
    )

    $todRoot = Split-Path -Path $PSScriptRoot -Parent
    if (-not $ConfigPath) {
        $ConfigPath = Join-Path $todRoot "config/tod.config.json"
    }

    if (-not (Test-Path $ConfigPath)) {
        $defaultConfig = [pscustomobject]@{
            mode = "hybrid"
            mim_base_url = "http://192.168.1.120:8000"
            timeout_seconds = 15
            fallback_to_local = $true
            default_actor = "tod"
        }

        if ($env:TOD_MODE_OVERRIDE) {
            $defaultConfig.mode = $env:TOD_MODE_OVERRIDE.ToLowerInvariant()
        }

        return $defaultConfig
    }

    $config = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    if ($env:TOD_MODE_OVERRIDE) {
        $config.mode = $env:TOD_MODE_OVERRIDE.ToLowerInvariant()
    }

    return $config
}

function Write-TodApiLog {
    param(
        [string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level = "INFO"
    )

    $todRoot = Split-Path -Path $PSScriptRoot -Parent
    $logDir = Join-Path $todRoot "logs"
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }

    $logPath = Join-Path $logDir "tod_api.log"
    $entry = [pscustomobject]@{
        timestamp = (Get-Date).ToUniversalTime().ToString("o")
        level = $Level
        message = $Message
    }

    $entry | ConvertTo-Json -Compress | Add-Content -Path $logPath
}

function ConvertTo-MimPayload {
    param(
        [hashtable]$Payload
    )

    $clean = @{}
    foreach ($key in $Payload.Keys) {
        $value = $Payload[$key]
        if ($null -ne $value) {
            $clean[$key] = $value
        }
    }

    return ($clean | ConvertTo-Json -Depth 10)
}

function Invoke-MimGet {
    param(
        [string]$Endpoint,
        [pscustomobject]$Config,
        [hashtable]$Headers = @{}
    )

    $uri = "{0}{1}" -f $Config.mim_base_url.TrimEnd('/'), $Endpoint
    Write-TodApiLog -Message "GET $uri"
    if ($Headers.Count -gt 0) {
        return Invoke-RestMethod -Method Get -Uri $uri -Headers $Headers -TimeoutSec ([int]$Config.timeout_seconds)
    }
    return Invoke-RestMethod -Method Get -Uri $uri -TimeoutSec ([int]$Config.timeout_seconds)
}

function Invoke-MimPost {
    param(
        [string]$Endpoint,
        [hashtable]$Payload,
        [pscustomobject]$Config,
        [hashtable]$Headers = @{}
    )

    $uri = "{0}{1}" -f $Config.mim_base_url.TrimEnd('/'), $Endpoint
    $body = ConvertTo-MimPayload -Payload $Payload
    Write-TodApiLog -Message "POST $uri"
    if ($Headers.Count -gt 0) {
        return Invoke-RestMethod -Method Post -Uri $uri -Body $body -Headers $Headers -ContentType "application/json" -TimeoutSec ([int]$Config.timeout_seconds)
    }
    return Invoke-RestMethod -Method Post -Uri $uri -Body $body -ContentType "application/json" -TimeoutSec ([int]$Config.timeout_seconds)
}

function Test-MimAvailable {
    param(
        [pscustomobject]$Config
    )

    try {
        $health = Invoke-MimGet -Endpoint "/health" -Config $Config
        $status = Invoke-MimGet -Endpoint "/status" -Config $Config
        return [pscustomobject]@{
            available = $true
            health = $health
            status = $status
        }
    }
    catch {
        Write-TodApiLog -Level "WARN" -Message "MIM unavailable: $($_.Exception.Message)"
        return [pscustomobject]@{
            available = $false
            health = $null
            status = $null
            error = $_.Exception.Message
        }
    }
}
