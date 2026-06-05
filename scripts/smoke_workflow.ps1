# Smoke test workflow — export BEFORE reset, run bot, export AFTER.
param(
    [int]$RuntimeSeconds = 480,
    [string]$Notes = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    $Py = "python"
}

Write-Host "=== SMOKE TEST WORKFLOW ===" -ForegroundColor Cyan

Write-Host "`n[1/5] Exporting outcomes to forensic archive..."
& $Py scripts/sl_forensic/export_to_archive.py --run-notes $(if ($Notes) { $Notes } else { "pre-smoke export" })
if ($LASTEXITCODE -ne 0) {
    Write-Host "EXPORT FAILED — aborting to preserve data" -ForegroundColor Red
    exit 1
}

Write-Host "`n[2/5] Analyzing forensic archive..."
& $Py scripts/sl_forensic/analyze_archive.py

Write-Host "`n[3/5] Review TIER 1 findings above." -ForegroundColor Yellow
Write-Host "       Apply any df[-2] fixes now if needed."
Write-Host "       Press Enter to continue to live run..."
Read-Host

Write-Host "`n[4/5] Starting live run ($RuntimeSeconds seconds)..."
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logfile = "logs/live_run_$ts.log"
New-Item -Force -ItemType Directory -Path logs | Out-Null

& $Py scripts/clean_session_data.py --mode smoke --config config.toml 2>$null

$proc = Start-Process -FilePath $Py -ArgumentList "main.py", "run" `
    -RedirectStandardOutput $logfile `
    -RedirectStandardError $logfile `
    -PassThru -NoNewWindow

Start-Sleep -Seconds $RuntimeSeconds
if (-not $proc.HasExited) {
    $proc.Kill()
}
Write-Host "Bot stopped."

Write-Host "`n[5/5] Exporting new outcomes to forensic archive..."
& $Py scripts/sl_forensic/export_to_archive.py --run-notes "post-run export $ts"

Write-Host "`n=== WORKFLOW COMPLETE ===" -ForegroundColor Green
Write-Host "Log: $logfile"
Write-Host "Archive report: REPORT_FORENSIC_ARCHIVE.md"
