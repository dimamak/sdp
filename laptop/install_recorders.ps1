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

function Install-Ffmpeg {
    # Only the audio recorder needs ffmpeg; the activity recorder uses .NET and Win32.
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "ffmpeg missing and winget unavailable - install from https://ffmpeg.org/download.html" -ForegroundColor Yellow
        return $false
    }
    Write-Host "installing ffmpeg via winget (a minute or two)..."
    winget install --id Gyan.FFmpeg -e --silent `
        --accept-source-agreements --accept-package-agreements | Out-Null
    # winget writes the new PATH to the registry; this session still has the old one
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Write-Host "ffmpeg installed" -ForegroundColor Green
        return $true
    }
    Write-Host "ffmpeg installed but not visible yet - open a new terminal and re-run" -ForegroundColor Yellow
    return $false
}

if (-not $NoAudio -and -not (Install-Ffmpeg)) {
    Write-Host "skipping the audio recorder (activity recorder is unaffected)" -ForegroundColor Yellow
    $NoAudio = $true
}

function Initialize-MicInterop {
    if ("DailypostMic" -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator{int EnumAudioEndpoints(int d,int m,out IntPtr c);int GetDefaultAudioEndpoint(int d,int r,out IMMDevice e);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice{int Activate(ref Guid id,int ctx,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume{
 int RegisterControlChangeNotify(IntPtr n);int UnregisterControlChangeNotify(IntPtr n);
 int GetChannelCount(out uint c);int SetMasterVolumeLevel(float l,ref Guid g);
 int SetMasterVolumeLevelScalar(float l,ref Guid g);int GetMasterVolumeLevel(out float l);
 int GetMasterVolumeLevelScalar(out float l);int SetChannelVolumeLevel(uint i,float l,ref Guid g);
 int SetChannelVolumeLevelScalar(uint i,float l,ref Guid g);int GetChannelVolumeLevel(uint i,out float l);
 int GetChannelVolumeLevelScalar(uint i,out float l);int SetMute([MarshalAs(UnmanagedType.Bool)]bool m,ref Guid g);
 int GetMute([MarshalAs(UnmanagedType.Bool)]out bool m);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject{}
public class DailypostMic{
 static IAudioEndpointVolume Vol(){
  var e=(IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
  IMMDevice dev; e.GetDefaultAudioEndpoint(1,1,out dev);   // eCapture, eMultimedia
  Guid iid=typeof(IAudioEndpointVolume).GUID; object o;
  dev.Activate(ref iid,1,IntPtr.Zero,out o); return (IAudioEndpointVolume)o;}
 public static bool IsMuted(){bool m; Vol().GetMute(out m); return m;}
 public static int Volume(){float l; Vol().GetMasterVolumeLevelScalar(out l); return (int)(l*100);}
 public static void Unmute(){Guid g=Guid.Empty; var v=Vol(); v.SetMute(false,ref g);
  float l; v.GetMasterVolumeLevelScalar(out l); if(l<0.5f) v.SetMasterVolumeLevelScalar(0.8f,ref g);}}
"@
}

if (-not $NoAudio) {
    # A muted mic records perfectly valid, perfectly empty files - capture looks
    # healthy while nothing is heard. Check it here rather than days later.
    try {
        Initialize-MicInterop
        if ([DailypostMic]::IsMuted()) {
            Write-Host ""
            Write-Host "Your default microphone is currently MUTED (volume $([DailypostMic]::Volume())%)." -ForegroundColor Yellow
            Write-Host "The recorder would capture silence and delete every segment."
            $unmute = Read-Host "unmute the microphone now? [y/N]"
            if ($unmute -eq "y") {
                [DailypostMic]::Unmute()
                if ([DailypostMic]::IsMuted()) {
                    Write-Host "still muted - there may be a hardware mic-mute key (often F9)" -ForegroundColor Yellow
                } else {
                    Write-Host "microphone unmuted (volume $([DailypostMic]::Volume())%)" -ForegroundColor Green
                }
            } else {
                Write-Host "left muted - unmute later with the mic key or Sound settings."
            }
        } else {
            Write-Host "microphone is live (volume $([DailypostMic]::Volume())%)" -ForegroundColor Green
        }
    } catch {
        Write-Host "could not read microphone state: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function ConvertTo-BashPath([string]$p) {
    $p = $p -replace "\\", "/"
    if ($p -match "^([A-Za-z]):(.*)$") { return "/" + $Matches[1].ToLower() + $Matches[2] }
    return $p
}

function Register-LogonTask([string]$name, [string]$script, [string]$outDir) {
    $ps = (Get-Command powershell).Source
    # PowerShell's -WindowStyle Hidden still leaves a console window when the
    # process is started by Task Scheduler. Launching through wscript with window
    # style 0 is the reliable way to get a truly hidden INTERACTIVE process -
    # and it must stay interactive, because screen capture needs a real desktop.
    #
    # The command is assembled with Chr(34) rather than escaped quotes: paths
    # contain spaces, and nested quote-doubling across PowerShell -> VBScript is
    # where this silently breaks.
    $vbs = Join-Path $Root "$name.vbs"
    $vbsLines = @(
        'Dim q, cmd',
        'q = Chr(34)',
        "cmd = q & ""$ps"" & q & "" -ExecutionPolicy Bypass -NoProfile -File "" & q & ""$script"" & q & "" -OutDir "" & q & ""$outDir"" & q",
        'CreateObject("WScript.Shell").Run cmd, 0, False'
    )
    Set-Content -Path $vbs -Value $vbsLines -Encoding ascii

    $exe = "$env:SystemRoot\System32\wscript.exe"
    $taskArgs = """$vbs"""
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
      <Command>$([System.Security.SecurityElement]::Escape($exe))</Command>
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
    Register-LogonTask "dailypost-record-audio" (Join-Path $scriptDir "record_audio.ps1") $audioDir
}
if (-not $NoActivity) {
    Register-LogonTask "dailypost-record-activity" (Join-Path $scriptDir "record_activity.ps1") $activityDir
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
    # NOTE: `$array -notmatch "x"` returns the non-matching ELEMENTS (truthy when
    # any other line exists), so it can't be used as an existence test - that
    # silently appended a duplicate PUSH_PATH on every run.
    $hasAudio = @($lines | Where-Object { $_ -like "PUSH_PATH=audio|*" }).Count -gt 0
    $hasAct = @($lines | Where-Object { $_ -like "PUSH_PATH=activity|*" }).Count -gt 0
    if (-not $NoAudio -and -not $hasAudio) { $add += "PUSH_PATH=audio|$(ConvertTo-BashPath $audioDir)|*.opus" }
    if (-not $NoActivity -and -not $hasAct) { $add += "PUSH_PATH=activity|$(ConvertTo-BashPath $activityDir)|*" }
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
