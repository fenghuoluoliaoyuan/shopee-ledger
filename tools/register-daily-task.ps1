# Register the daily check as a Windows scheduled task. Run once.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\register-daily-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\register-daily-task.ps1 -RunNow
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\register-daily-task.ps1 -Remove
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 using the ANSI code page,
# so non-ASCII here would be a parse error. See daily-check.ps1 for details.

param(
    [string]$Time = '10:00',
    [switch]$Remove,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$taskName = 'shopee-ledger daily check'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script = Join-Path $root 'tools\daily-check.ps1'

if ($Remove) {
    schtasks /Delete /TN $taskName /F 2>$null | Out-Null
    Write-Output "deleted scheduled task: $taskName"
    exit 0
}

if (-not (Test-Path $script)) { throw "missing $script" }

$action = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`""
schtasks /Create /TN $taskName /TR $action /SC DAILY /ST $Time /F | Out-Null
Write-Output "registered: $taskName (daily at $Time)"
schtasks /Query /TN $taskName /FO LIST | Select-String -Pattern 'TaskName|Next Run|Status'

if ($RunNow) {
    schtasks /Run /TN $taskName | Out-Null
    Write-Output 'triggered once; check data\daily.log in a minute'
}
