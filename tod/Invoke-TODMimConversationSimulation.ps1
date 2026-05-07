param(
    [string]$Scenario = "all",
    [string]$SyntheticRoot = "",
    [string]$OutputPath = ""
)

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonCandidates = @(
    (Join-Path $RepoRoot ".venv/bin/python"),
    (Join-Path $RepoRoot ".venv/Scripts/python.exe"),
    "python3",
    "python"
)

$PythonExe = $null
foreach ($candidate in $PythonCandidates) {
    if ($candidate -in @("python3", "python")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            $PythonExe = $command.Source
            break
        }
        continue
    }
    if (Test-Path $candidate) {
        $PythonExe = $candidate
        break
    }
}

if (-not $PythonExe) {
    throw "No Python runtime found for Invoke-TODMimConversationSimulation.ps1"
}

$Arguments = @(
    (Join-Path $RepoRoot "scripts/run_tod_mim_conversation_simulation.py"),
    "--scenario",
    $Scenario
)

if ($SyntheticRoot) {
    $Arguments += @("--synthetic-root", $SyntheticRoot)
}
if ($OutputPath) {
    $Arguments += @("--output", $OutputPath)
}

Push-Location $RepoRoot
try {
    & $PythonExe @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}