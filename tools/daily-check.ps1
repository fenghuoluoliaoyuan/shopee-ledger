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

# 4) Alerts. Exit code 1 means a P1 alert is open.
Invoke-Step 'alert' @('-m', 'shopee_ledger', 'alert')

Write-Log '=== end ==='
