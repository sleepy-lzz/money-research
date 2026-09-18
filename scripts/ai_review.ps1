param(
    [ValidateSet('prepare','import','validate','retrospective','doctor')][string]$Action = 'prepare',
    [ValidateSet('all','conservative','balanced','aggressive')][string]$Mode = 'all',
    [string]$BatchId = '',
    [string]$Result = ''
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Project Python environment not found' }
Push-Location $project
try {
    if ($Action -eq 'doctor') {
        & $python -m ashare_agent research-doctor
    } elseif ($Action -eq 'prepare') {
        $argsList = @('-m','ashare_agent','research-export')
        if ($BatchId) { $argsList += @('--batch-id',$BatchId) }
        if ($Mode -ne 'all') { $argsList += @('--mode',$Mode) }
        & $python @argsList
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'Frozen packet and mode prompts exported. Give the prompt to Codex with local project access. This script does not invoke a paid API or claim the model has already run.'
            Start-Process explorer.exe (Join-Path $project 'runtime\research-v2\outbox')
        }
    } else {
        if (-not $BatchId -or -not $Result) { throw 'Specify -BatchId and -Result. Automatic latest-batch guessing is forbidden for imports.' }
        if (-not (Test-Path $Result -PathType Leaf)) { throw 'Result must be a JSON file (single mode or atomic multi-mode array)' }
        $argsList = @('-m','ashare_agent','research-import','--batch-id',$BatchId,'--input',$Result)
        if ($Action -eq 'validate') { $argsList += '--validate-only' }
        if ($Action -eq 'retrospective') { $argsList += '--retrospective' }
        & $python @argsList
    }
    exit $LASTEXITCODE
} finally { Pop-Location }
