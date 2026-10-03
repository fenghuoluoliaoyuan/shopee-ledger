# Daily data check for shopee-ledger.
#
# Registered by tools\register-daily-task.ps1 and run by Windows Task Scheduler,
# which uses Windows PowerShell 5.1. That host reads .ps1 files using the ANSI code
# page, so this file is kept ASCII-only on purpose: Chinese here would be mojibake
# and a parse error. Chinese log text comes from the Python side.
#
# Run manually:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\daily-check.ps1
#
# Design (see .cursor/rules/agent-autonomy.mdc): do not wait to be triggered.
# A failing step must not stop the later ones - a half-done check beats no check.

$ErrorActionPreference = 'Continue'

# Child processes (python) emit UTF-8; decode it as UTF-8 so the log stays readable.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'

$python = 'D:\python\python312\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$logDir = Join-Path $root 'data'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'daily.log'

function Write-Log($text) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$stamp] $text" -Encoding utf8
}

function Invoke-Step($label, $arguments) {
    Write-Log "--- $label ---"
    & $python @arguments 2>&1 | ForEach-Object { Write-Log "  $_" }
    Write-Log "$label exit code: $LASTEXITCODE"
}

Write-Log '=== begin ==='

# 1) Headless browser checks the watched listing pages for new documents.
#    New notices always appear at the top of page 1, so page 1 is enough.
Invoke-Step 'watch --fetch' @('-m', 'shopee_ledger', 'watch', '--fetch')

# 2) Fetch the body text of documents not read yet, so values can be extracted.
Invoke-Step 'harvest' @('-m', 'shopee_ledger', 'harvest', '--limit', '12')

# 3) Fetch public parameter sources defined in spec/sources.json.
Invoke-Step 'fetch' @('-m', 'shopee_ledger', 'fetch')

# 4) Pull the SLS freight rate table and report any change.
#    Rates do change, so the point is detection: which channel, which tier, from what to what.
Invoke-Step 'freight --check' @('-m', 'shopee_ledger', 'freight', '--check')

# 5) Link new documents to the parameters they may answer (locates, never values).
Invoke-Step 'associate' @('-m', 'shopee_ledger', 'associate')

# 6) Alerts. Exit code 1 means a P1 alert is open.
Invoke-Step 'alert' @('-m', 'shopee_ledger', 'alert')

# 7) Calibration: compares measured values against the D-level guesses.
#    Mostly says "not enough samples" until orders start flowing - that output is the
#    reminder of what data to collect.
Invoke-Step 'calibrate' @('-m', 'shopee_ledger', 'calibrate')

# 8) Evidence health: every A-level parameter's snapshot must exist AND be tracked by git.
#    A reference string alone is not evidence - if the file is not in the repo the chain
#    is broken for anyone who clones it.
Invoke-Step 'check-snapshots' @('-m', 'shopee_ledger', 'check-snapshots')

# 9) Static check: no local name is read before its assignment. Two real bugs
#    (rounds 11 and 12) lived here because the failing branch was never executed
#    by any test; this catches that shape without running it.
Invoke-Step 'check-lint' @('-m', 'shopee_ledger', 'check-lint')

Write-Log '=== end ==='
