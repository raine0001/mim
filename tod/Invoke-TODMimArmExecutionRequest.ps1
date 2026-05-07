param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$SharedRoot = "runtime/shared",
    [Parameter(Mandatory = $true)][string]$Command,
    [double]$X = 0,
    [double]$Y = 0,
    [double]$Z = 0
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonCandidates = @(
    (Join-Path $RepoRoot ".venv/bin/python"),
    (Join-Path $RepoRoot ".venv/Scripts/python.exe"),
    "python3",
    "python"
)

$Python = $null
foreach ($Candidate in $PythonCandidates) {
    if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
        $Python = $Candidate
        break
    }
}

if (-not $Python) {
    throw "No Python runtime found for Invoke-TODMimArmExecutionRequest.ps1"
}

& $Python \
    (Join-Path $RepoRoot "scripts/submit_tod_mim_arm_execution_request.py") \
    --base-url $BaseUrl \
    --shared-root $SharedRoot \
    --command $Command \
    --x $X \
    --y $Y \
    --z $Z