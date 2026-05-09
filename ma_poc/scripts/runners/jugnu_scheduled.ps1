# Wraps scripts/runners/jugnu.py to advance --start-index by 100 each run.
# Scheduled via schtasks every 15 minutes. Stops when CSV is exhausted.

$ErrorActionPreference = "Stop"

# This script lives at ma_poc/scripts/runners/. Walk up two levels to land on ma_poc/.
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$maPocDir   = Split-Path -Parent (Split-Path -Parent $scriptDir)
Set-Location $maPocDir

$csvPath     = "config/properties.csv"
$counterFile = "data/state/jugnu_start_index.txt"
$logFile     = "data/runs/scheduler.log"
$pythonExe   = ".venv/Scripts/python.exe"
$batchSize   = 100
$seedIndex   = 101
$taskName    = "JugnuRunner"

New-Item -ItemType Directory -Force -Path (Split-Path $logFile)     | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $counterFile) | Out-Null

function Log {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Tee-Object -FilePath $logFile -Append
}

if (-not (Test-Path $csvPath)) {
    Log "ERROR: CSV not found at $csvPath (cwd=$maPocDir)"
    exit 1
}
if (-not (Test-Path $pythonExe)) {
    Log "ERROR: venv python not found at $pythonExe (cwd=$maPocDir)"
    exit 1
}

if (Test-Path $counterFile) {
    $startIndex = [int]((Get-Content $counterFile -Raw).Trim())
} else {
    $startIndex = $seedIndex
}

$rowCount = @(Import-Csv $csvPath).Count

if ($startIndex -gt $rowCount) {
    Log "CSV exhausted (start-index=$startIndex > rows=$rowCount). Disabling task '$taskName'."
    schtasks /Change /TN $taskName /DISABLE 2>&1 | Out-Null
    exit 0
}

Log "Run start: --start-index $startIndex --limit $batchSize (CSV rows=$rowCount)"
$prevPref = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $output = & $pythonExe scripts/runners/jugnu.py --csv $csvPath --start-index $startIndex --limit $batchSize 2>&1 |
        Tee-Object -FilePath $logFile -Append
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevPref
}

# Trust the "Run complete: X/Y succeeded" marker over $LASTEXITCODE: asyncio/Playwright
# subprocess cleanup on Windows can raise during interpreter shutdown (after the batch has
# fully succeeded), flipping the exit code to 1 and stalling the counter forever.
$runCompleteLine = $output | Select-String -Pattern 'Run complete:\s+\d+/\d+\s+succeeded' | Select-Object -Last 1
$runSucceeded    = $null -ne $runCompleteLine

if ($runSucceeded) {
    $nextIndex = $startIndex + $batchSize
    Set-Content -Path $counterFile -Value $nextIndex
    if ($exitCode -eq 0) {
        Log "Run done (exit=0). Next start-index=$nextIndex"
    } else {
        Log "Run done (exit=$exitCode but 'Run complete:' marker found; treating as success). Next start-index=$nextIndex"
    }
    exit 0
} else {
    Log "Run failed (exit=$exitCode, no 'Run complete:' marker). Counter unchanged; will retry next fire at $startIndex."
    exit $exitCode
}
