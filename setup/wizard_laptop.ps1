# SERVER MODE ONLY. This is the laptop half of the two-machine setup: it
# generates laptop/push.conf, verifies ssh, and registers the push task that
# ships this laptop's files to an always-on server over SSH.
#
# If everything runs on this one machine (mode: laptop) you do not want this
# script at all — there is no remote to push to. Run instead:
#     python -m setup.wizard
#
# Run:  powershell -ExecutionPolicy Bypass -File setup\wizard_laptop.ps1
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$confPath = Join-Path $repoRoot "laptop\push.conf"

function ConvertTo-BashPath([string]$p) {
    $p = $p -replace "\\", "/"
    if ($p -match "^([A-Za-z]):(.*)$") { return "/" + $Matches[1].ToLower() + $Matches[2] }
    return $p
}

Write-Host "=== dailypost laptop setup ===" -ForegroundColor Cyan

$remote = Read-Host "ssh host alias of your server (from ~/.ssh/config)"
Write-Host "verifying ssh connectivity to '$remote'..."
$check = ssh -o BatchMode=yes -o ConnectTimeout=10 $remote "echo ok" 2>$null
if ($check -ne "ok") { throw "ssh to '$remote' failed non-interactively - set up key auth first" }
Write-Host "ssh OK" -ForegroundColor Green

# Ask the server which instances exist rather than guessing a path: on a shared
# server the default ingest dir belongs to whoever was set up FIRST, and pushing
# there would file this laptop's files into someone else's store.
$appDir = Read-Host "dailypost app dir on the server [default /opt/dailypost/app]"
if (-not $appDir) { $appDir = "/opt/dailypost/app" }

$remoteDir = ""
$runUser = ""
$rows = @(ssh -o BatchMode=yes $remote "cd '$appDir' && ./.venv/bin/python -m setup.wizard --list-instances" 2>$null |
          Where-Object { $_ -match "`t" })
if ($rows.Count -ge 1) {
    Write-Host "instances on $remote :"
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $f = $rows[$i] -split "`t"
        Write-Host ("  [{0}] {1}  ->  {2}  (runs as {3})" -f ($i + 1), $f[0], $f[1], $f[2])
    }
    $pick = Read-Host "which instance is this laptop for? [1-$($rows.Count), default 1]"
    if (-not $pick) { $pick = 1 }
    $sel = $rows[[int]$pick - 1] -split "`t"
    $remoteDir = $sel[1]
    $runUser = $sel[2]
    Write-Host "using $remoteDir (owner $runUser)" -ForegroundColor Green
} else {
    Write-Host "could not list instances - falling back to manual entry." -ForegroundColor Yellow
}

if (-not $remoteDir) {
    $remoteDir = Read-Host "remote ingest dir [default /opt/dailypost/ingest/laptop]"
    if (-not $remoteDir) { $remoteDir = "/opt/dailypost/ingest/laptop" }
}

# Files arrive owned by whoever ssh connects as. If the pipeline runs as a
# different user it won't be able to drain the spool, so hand ownership over.
$sshUser = (ssh -o BatchMode=yes $remote "whoami").Trim()
if (-not $runUser) {
    $runUser = Read-Host "user that runs dailypost on the server [default $sshUser]"
    if (-not $runUser) { $runUser = $sshUser }
}
if ($runUser -ne $sshUser) {
    $postCmd = "chown -R ${runUser}: '$remoteDir'"
    Write-Host "ssh connects as '$sshUser' but the pipeline runs as '$runUser' - pushes will chown automatically."
} else {
    $postCmd = ""
}

$lines = @("REMOTE=$remote", "REMOTE_DIR=$remoteDir", "REMOTE_POST_CMD=$postCmd", "")

# Claude Code projects
$claudeDir = Join-Path $env:USERPROFILE ".claude\projects"
if (Test-Path $claudeDir) {
    $ans = Read-Host "push Claude Code sessions from $claudeDir ? [Y/n]"
    if ($ans -ne "n") {
        $bashDir = ConvertTo-BashPath $claudeDir
        $lines += "PUSH_PATH=claude|$bashDir|*.jsonl"
    }
}

# Screenshots
$shotDir = Read-Host "screenshots folder to push (empty = skip)"
if ($shotDir) {
    if (-not (Test-Path $shotDir)) { throw "folder not found: $shotDir" }
    $bashShot = ConvertTo-BashPath $shotDir
    $lines += "PUSH_PATH=screenshots|$bashShot|*.png"
}

# Extra folders (audio etc.)
while ($true) {
    $extra = Read-Host "extra folder to push as name|dir|glob (empty = done)"
    if (-not $extra) { break }
    $lines += "PUSH_PATH=$extra"
}

# UTF-8 WITHOUT BOM - PowerShell 5.1's -Encoding utf8 adds a BOM, which breaks
# the bash parser that reads this file
[System.IO.File]::WriteAllText($confPath, (($lines -join "`n") + "`n"),
                               (New-Object System.Text.UTF8Encoding $false))
Write-Host "wrote $confPath" -ForegroundColor Green

$time = Read-Host "daily push time [default 23:00]"
if (-not $time) { $time = "23:00" }
& (Join-Path $repoRoot "laptop\install_task.ps1") -Time $time

# --- optional always-on recorders -------------------------------------------
Write-Host ""
Write-Host "Optional: capture office conversations and non-coding screen activity."
Write-Host "  audio    - always-on room mic, silence stripped and transcribed on the server,"
Write-Host "             raw audio deleted afterwards. It hears everyone in the room."
Write-Host "  activity - foreground window log + occasional screenshots (skips IDEs/terminals)."
Write-Host "  Both pause instantly via a PAUSE-recording.cmd shortcut."
$recAudio = Read-Host "install the audio recorder? [y/N]"
$recAct = Read-Host "install the activity recorder? [y/N]"

if ($recAudio -eq "y" -or $recAct -eq "y") {
    # install_recorders installs ffmpeg itself when the audio recorder is wanted
    $recArgs = @{}
    if ($recAudio -ne "y") { $recArgs["NoAudio"] = $true }
    if ($recAct -ne "y") { $recArgs["NoActivity"] = $true }
    & (Join-Path $repoRoot "laptop\install_recorders.ps1") @recArgs
    Write-Host "Recorders start automatically at next logon." -ForegroundColor Green
}

$test = Read-Host "run a test push now? [Y/n]"
if ($test -ne "n") {
    $bash = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    & $bash -lc ("'" + (ConvertTo-BashPath $repoRoot) + "/laptop/push_daily.sh'")
}
Write-Host "laptop setup complete." -ForegroundColor Cyan
