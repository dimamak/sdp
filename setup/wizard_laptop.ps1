# Laptop-side setup companion: generates laptop/push.conf, verifies ssh, registers the task.
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

$remoteDir = Read-Host "remote ingest dir [default /opt/dailypost/ingest/laptop]"
if (-not $remoteDir) { $remoteDir = "/opt/dailypost/ingest/laptop" }
$postCmd = Read-Host "remote post-extract command (optional, e.g. chown -R appuser: $remoteDir; empty = none)"

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

Set-Content -Path $confPath -Value ($lines -join "`n") -Encoding utf8
Write-Host "wrote $confPath" -ForegroundColor Green

$time = Read-Host "daily push time [default 23:00]"
if (-not $time) { $time = "23:00" }
& (Join-Path $repoRoot "laptop\install_task.ps1") -Time $time

$test = Read-Host "run a test push now? [Y/n]"
if ($test -ne "n") {
    $bash = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    & $bash -lc ("'" + (ConvertTo-BashPath $repoRoot) + "/laptop/push_daily.sh'")
}
Write-Host "laptop setup complete." -ForegroundColor Cyan
