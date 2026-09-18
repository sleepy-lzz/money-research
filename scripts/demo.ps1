param([string]$Output = 'runtime/demo-report')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Push-Location $taskRoot
try {
    & $taskPython -m ashare_agent init
    if ($LASTEXITCODE -ne 0) { throw 'Initialization failed' }
    & $taskPython -m ashare_agent demo-data --sessions 320
    if ($LASTEXITCODE -ne 0) { throw 'Demo data generation failed' }
    & $taskPython -m ashare_agent backtest --snapshot runtime/snapshots/demo-v2-320-42 --start 2025-09-25 --end 2025-12-31 --output $Output
    if ($LASTEXITCODE -ne 0) { throw 'Backtest failed' }
    Write-Output "Report: $(Join-Path $taskRoot $Output)\report.html"
} finally {
    Pop-Location
}

