# Full debug live run after refactor — verbose console + session log file.
# Usage:
#   .\scripts\run_debug_live.ps1              # smoke 15 min (no Telegram)
#   .\scripts\run_debug_live.ps1 -Mode bot    # full bot via main.py
#   .\scripts\run_debug_live.ps1 -Minutes 60  # longer smoke window

param(
    [ValidateSet("smoke", "bot")]
    [string]$Mode = "smoke",
    [int]$Minutes = 15
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:DEBUG_BOT = "1"
$env:PYTHONFAULTHANDLER = "1"

$runtimeSec = [Math]::Max(60, $Minutes * 60)

if ($Mode -eq "bot") {
    Write-Host "Starting full bot (DEBUG) | log under data/bot/logs/"
    python main.py
    exit $LASTEXITCODE
}

Write-Host "Starting live_smoke_bot (DEBUG) | runtime=${runtimeSec}s | log under data/bot/logs/"
python scripts/live_smoke_bot.py `
    --debug `
    --runtime-seconds $runtimeSec `
    --warmup-seconds 90 `
    --skip-final-emergency-cycle
exit $LASTEXITCODE
