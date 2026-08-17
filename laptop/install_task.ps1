# Registers the Windows Scheduled Task that runs push_daily.sh nightly via Git Bash.
# Uses an XML task definition so we get, without needing admin rights:
#   - StartWhenAvailable: a run missed because the laptop was off/asleep fires
#     as soon as the machine is back
#   - a logon trigger as extra catch-up (the push script is incremental, so
#     redundant runs are cheap no-ops)
# Run:  powershell -ExecutionPolicy Bypass -File laptop\install_task.ps1 [-Time 23:00]
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

# bash-style path (C:\x\y -> /c/x/y) - PS 5.1 compatible
function ConvertTo-BashPath([string]$p) {
    $p = $p -replace "\\", "/"
    if ($p -match "^([A-Za-z]):(.*)$") { return "/" + $Matches[1].ToLower() + $Matches[2] }
    return $p
}
$bashPath = ConvertTo-BashPath $pushScript

$user = "$env:USERDOMAIN\$env:USERNAME"
$start = (Get-Date -Format "yyyy-MM-dd") + "T" + $Time + ":00"
$esc = [System.Security.SecurityElement]::Escape("-lc `"'$bashPath'`"")
$cmdEsc = [System.Security.SecurityElement]::Escape($bash)
$userEsc = [System.Security.SecurityElement]::Escape($user)

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$userEsc</UserId>
      <Delay>PT2M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$userEsc</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$cmdEsc</Command>
      <Arguments>$esc</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$tmp = Join-Path $env:TEMP "dailypost-task.xml"
Set-Content -Path $tmp -Value $xml -Encoding Unicode
schtasks /Create /F /TN $TaskName /XML $tmp | Out-Null
$xmlOk = ($LASTEXITCODE -eq 0)
Remove-Item $tmp -ErrorAction SilentlyContinue

if ($xmlOk) {
    Write-Host "Scheduled task '$TaskName' registered: daily at $Time + at logon; missed runs fire when the laptop wakes."
} else {
    # last-resort fallback: plain daily task (no catch-up)
    Write-Host "XML task registration failed - falling back to a plain daily task (no missed-run catch-up)."
    $tr = "\`"$bash\`" -lc $bashPath"
    schtasks /Create /F /TN $TaskName /SC DAILY /ST $Time /TR $tr | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks fallback failed too" }
    Write-Host "Scheduled task '$TaskName' registered: daily at $Time."
}
Write-Host "Test now with:  schtasks /Run /TN $TaskName"
