# Registers the Windows Scheduled Task that runs push_daily.sh nightly via Git Bash.
# Run from an elevated or normal PowerShell:  .\install_task.ps1 [-Time 23:00]
param(
    [string]$Time = "23:00",
    [string]$TaskName = "dailypost-push"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pushScript = Join-Path $scriptDir "push_daily.sh"

# locate Git Bash
$bash = @(
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LocalAppData\Programs\Git\bin\bash.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $bash) { throw "Git Bash not found - install Git for Windows" }

# bash-style path (C:\x\y -> /c/x/y) — PS 5.1 compatible
function ConvertTo-BashPath([string]$p) {
    $p = $p -replace "\\", "/"
    if ($p -match "^([A-Za-z]):(.*)$") { return "/" + $Matches[1].ToLower() + $Matches[2] }
    return $p
}
$bashPath = ConvertTo-BashPath $pushScript

$action = New-ScheduledTaskAction -Execute $bash -Argument "-lc `"'$bashPath'`""
$triggerDaily = New-ScheduledTaskTrigger -Daily -At $Time
# catch-up: also run at logon (script itself is incremental, so this is cheap)
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($triggerDaily, $triggerLogon) -Settings $settings -Force | Out-Null

Write-Host "Scheduled task '$TaskName' registered: daily at $Time + at logon (catch-up)."
Write-Host "Test now with:  Start-ScheduledTask -TaskName $TaskName"
