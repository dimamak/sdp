# Registers the always-on recorders as logon scheduled tasks, adds their folders
# to push.conf, and creates pause/resume shortcuts.
#
# Run:  powershell -ExecutionPolicy Bypass -File laptop\install_recorders.ps1
param(
    [string]$Root = "$env:USERPROFILE\dailypost",
    [switch]$NoAudio,
    [switch]$NoActivity
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$audioDir = Join-Path $Root "audio"
$activityDir = Join-Path $Root "activity"
New-Item -ItemType Directory -Force -Path $audioDir, $activityDir | Out-Null

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg not found in PATH - install it first (winget install Gyan.FFmpeg)"
}

function ConvertTo-BashPath([string]$p) {
    $p = $p -replace "\\", "/"
    if ($p -match "^([A-Za-z]):(.*)$") { return "/" + $Matches[1].ToLower() + $Matches[2] }
    return $p
}

function Register-LogonTask([string]$name, [string]$script, [string]$argLine) {
    $ps = (Get-Command powershell).Source
    $taskArgs = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" $argLine"
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>$([System.Security.SecurityElement]::Escape($user))</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$([System.Security.SecurityElement]::Escape($user))</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$([System.Security.SecurityElement]::Escape($ps))</Command>
      <Arguments>$([System.Security.SecurityElement]::Escape($taskArgs))</Arguments>
    </Exec>
  </Actions>
</Task>
"@
    $tmp = Join-Path $env:TEMP "$name.xml"
    Set-Content -Path $tmp -Value $xml -Encoding Unicode
    schtasks /Create /F /TN $name /XML $tmp | Out-Null
    $rc = $LASTEXITCODE
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if ($rc -ne 0) { throw "failed to register task $name" }
    Write-Host "registered task: $name"
}

if (-not $NoAudio) {
    Register-LogonTask "dailypost-record-audio" (Join-Path $scriptDir "record_audio.ps1") "-OutDir `"$audioDir`""
}
if (-not $NoActivity) {
    Register-LogonTask "dailypost-record-activity" (Join-Path $scriptDir "record_activity.ps1") "-OutDir `"$activityDir`""
}

# pause / resume shortcuts - the mic hears the whole room, so stopping it must be
# a single obvious action
$pauseCmd = Join-Path $Root "PAUSE-recording.cmd"
$resumeCmd = Join-Path $Root "RESUME-recording.cmd"
Set-Content -Path $pauseCmd -Encoding ascii -Value @"
@echo off
echo paused > "$audioDir\PAUSED"
echo paused > "$activityDir\PAUSED"
echo Recording PAUSED. Run RESUME-recording.cmd to start again.
pause
"@
Set-Content -Path $resumeCmd -Encoding ascii -Value @"
@echo off
del "$audioDir\PAUSED" 2>nul
del "$activityDir\PAUSED" 2>nul
echo Recording RESUMED.
pause
"@
Write-Host "pause/resume: $pauseCmd  /  $resumeCmd"

# make sure the push script ships both folders
$conf = Join-Path $scriptDir "push.conf"
if (Test-Path $conf) {
    $lines = [System.IO.File]::ReadAllLines($conf)
    $add = @()
    if ($lines -notmatch "PUSH_PATH=audio\|") { $add += "PUSH_PATH=audio|$(ConvertTo-BashPath $audioDir)|*.opus" }
    if ($lines -notmatch "PUSH_PATH=activity\|") { $add += "PUSH_PATH=activity|$(ConvertTo-BashPath $activityDir)|*" }
    if ($add) {
        [System.IO.File]::WriteAllText($conf, (($lines + $add) -join "`n") + "`n",
                                       (New-Object System.Text.UTF8Encoding $false))
        Write-Host "added to push.conf:`n  $($add -join "`n  ")"
    } else {
        Write-Host "push.conf already ships audio + activity"
    }
} else {
    Write-Host "NOTE: laptop\push.conf not found - run setup\wizard_laptop.ps1 first, then re-run this."
}

Write-Host "`nStart now without logging out:"
Write-Host "  schtasks /Run /TN dailypost-record-audio"
Write-Host "  schtasks /Run /TN dailypost-record-activity"
