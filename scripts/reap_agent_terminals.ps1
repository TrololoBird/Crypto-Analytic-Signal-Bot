# Reap Cursor agent terminal logs stuck without exit_code (no user action required).
param(
    [string]$TerminalsDir = "$env:USERPROFILE\.cursor\projects\c-Users-undea-Documents-bot2\terminals"
)

$resolved = Resolve-Path $TerminalsDir -ErrorAction SilentlyContinue
if (-not $resolved) {
    Write-Error "terminals dir not found: $TerminalsDir"
    exit 1
}

$now = [datetime]::UtcNow
$reaped = 0

Get-ChildItem (Join-Path $resolved "*.txt") | ForEach-Object {
    $raw = Get-Content $_.FullName -Raw
    if ($raw -notmatch 'running_for_ms' -or $raw -match 'exit_code:') {
        return
    }
    $procId = $null
    if ($raw -match 'pid:\s*(\d+)') {
        $procId = [int]$Matches[1]
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host "killed pid=$procId file=$($_.Name)"
        }
    }
    $started = $now
    if ($raw -match 'started_at:\s*(\S+)') {
        $started = [datetime]::Parse($Matches[1])
    }
    $elapsed = [int](($now - $started).TotalMilliseconds)
    $footer = @"

---
exit_code: 130
elapsed_ms: $elapsed
ended_at: $($now.ToString('o'))
reaped_by: scripts/reap_agent_terminals.ps1
---
"@
    Add-Content -Path $_.FullName -Value $footer -Encoding utf8
    Write-Host "reaped $($_.Name)"
    $reaped++
}

Write-Host "done reaped=$reaped"
