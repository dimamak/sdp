# SERVER MODE ONLY, and only if you run the X reply radar's browser extension
# from this laptop against a server-mode instance (server/bot/localapi.py
# binds to the server's 127.0.0.1 only, so this laptop needs a standing SSH
# tunnel to reach it while you browse x.com).
#
# Unlike install_task.ps1's nightly push, this task must never exit on its
# own: radar_tunnel.sh loops and reconnects internally, so the scheduled task
# just needs to start it once at logon and leave it running, hidden, for the
# whole session. Run:
#   powershell -ExecutionPolicy Bypass -File laptop\install_radar_tunnel_task.ps1
param(
    [string]$TaskName = "dailypost-radar-tunnel"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$tunnelScript = Join-Path $scriptDir "radar_tunnel.sh"
if (-not (Test-Path $tunnelScript)) { throw "radar_tunnel.sh not found next to this script" }
$conf = Join-Path $scriptDir "push.conf"
if (-not (Test-Path $conf)) { throw "push.conf not found - copy push.conf.example and set REMOTE / RADAR_PORT first" }
if (-not (Select-String -Path $conf -Pattern '^RADAR_PORT=\S')) {
    throw "RADAR_PORT not set in push.conf - add it (see push.conf.example) before installing this task"
}

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
$bashPath = ConvertTo-BashPath $tunnelScript

# Hidden launcher: schtasks has no "run minimized and detached" of its own, and
# running bash.exe directly leaves a visible console open for the whole
# session. wscript with window style 0 gives a genuinely hidden but still
# interactive-desktop process (ssh itself needs no desktop, but this mirrors
# setup/autostart.py's vbs_launcher() so laptop and server autostart behave
# the same way). Quotes are built with Chr(34) rather than escaped inline,
# same reason as autostart.py: paths under Program Files contain spaces.
$vbsPath = Join-Path $scriptDir "$TaskName.vbs"
$vbs = @"
Dim q, cmd, sh
q = Chr(34)
cmd = q & "$bash" & q & " -lc " & q & "'$bashPath'" & q
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$scriptDir"
sh.Run cmd, 0, False
"@
Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII

$wscript = "$env:SystemRoot\System32\wscript.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$userEsc = [System.Security.SecurityElement]::Escape($user)
$cmdEsc = [System.Security.SecurityElement]::Escape($wscript)
$argEsc = [System.Security.SecurityElement]::Escape("`"$vbsPath`"")

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$userEsc</UserId>
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
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$cmdEsc</Command>
      <Arguments>$argEsc</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$tmp = Join-Path $env:TEMP "$TaskName.xml"
Set-Content -Path $tmp -Value $xml -Encoding Unicode
schtasks /Create /F /TN $TaskName /XML $tmp | Out-Null
$xmlOk = ($LASTEXITCODE -eq 0)
Remove-Item $tmp -ErrorAction SilentlyContinue
if (-not $xmlOk) { throw "schtasks /Create failed for $TaskName" }

schtasks /Run /TN $TaskName | Out-Null
Write-Host "Scheduled task '$TaskName' registered and started: runs at every logon, stays open, reconnects on drop."
Write-Host "Stop it:   schtasks /End /TN $TaskName"
Write-Host "Remove it: schtasks /End /TN $TaskName ; schtasks /Delete /TN $TaskName /F ; Remove-Item '$vbsPath'"
