# Registers the always-on recorders as logon scheduled tasks, adds their folders
# to push.conf, and creates pause/resume shortcuts.
#
# This is the SERVER-mode path: a Windows laptop records locally and pushes the
# output to a separate server over SSH. In laptop mode the recorders run inside
# the bot process instead and are configured by:
#     python -m setup.wizard --source audio
#     python -m setup.wizard --source activity
#
# The recorders themselves are Python (server/capture/), not PowerShell — the
# same code runs on Windows, macOS and Linux. This script only registers them.
#
# Run:  powershell -ExecutionPolicy Bypass -File laptop\install_recorders.ps1
param(
    [string]$Root = "$env:USERPROFILE\dailypost",
    [switch]$NoAudio,
    [switch]$NoActivity
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$audioDir = Join-Path $Root "audio"
$activityDir = Join-Path $Root "activity"
New-Item -ItemType Directory -Force -Path $audioDir, $activityDir | Out-Null

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "no venv at $python - run 'python -m setup.wizard' in $repoRoot first"
}
if (-not $NoActivity) {
    # screenshots + foreground-window reading need Pillow/mss, which are not in
    # requirements.txt (nobody downloads them unless they turn this on)
    & (Join-Path $repoRoot ".venv\Scripts\pip.exe") install -q -r (Join-Path $repoRoot "requirements-capture.txt")
}

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
interface IMMDeviceEnumerator{int EnumAudioEndpoints(int d,int m,out IMMDeviceCollection c);int GetDefaultAudioEndpoint(int d,int r,out IMMDevice e);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
// method order must match the COM vtable exactly
interface IMMDevice{int Activate(ref Guid id,int ctx,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);
 int OpenPropertyStore(int a,out IPropertyStore s);int GetId([MarshalAs(UnmanagedType.LPWStr)]out string id);int GetState(out int st);}
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume{
 int RegisterControlChangeNotify(IntPtr n);int UnregisterControlChangeNotify(IntPtr n);
 int GetChannelCount(out uint c);int SetMasterVolumeLevel(float l,ref Guid g);
 int SetMasterVolumeLevelScalar(float l,ref Guid g);int GetMasterVolumeLevel(out float l);
 int GetMasterVolumeLevelScalar(out float l);int SetChannelVolumeLevel(uint i,float l,ref Guid g);
 int SetChannelVolumeLevelScalar(uint i,float l,ref Guid g);int GetChannelVolumeLevel(uint i,out float l);
 int GetChannelVolumeLevelScalar(uint i,out float l);int SetMute([MarshalAs(UnmanagedType.Bool)]bool m,ref Guid g);
 int GetMute([MarshalAs(UnmanagedType.Bool)]out bool m);}
[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCollection{int GetCount(out uint c);int Item(uint i,out IMMDevice d);}
[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore{int GetCount(out int c);int GetAt(int i,out PROPERTYKEY k);int GetValue(ref PROPERTYKEY k,out PROPVARIANT v);}
[StructLayout(LayoutKind.Sequential)] public struct PROPERTYKEY{public Guid fmtid;public int pid;}
[StructLayout(LayoutKind.Explicit)] public struct PROPVARIANT{[FieldOffset(0)]public short vt;[FieldOffset(8)]public IntPtr p;}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject{}
public class DailypostMic{
 // Target the device the RECORDER will use, not the system default: the default
 // capture endpoint is often a webcam or headset, so checking it reports a
 // healthy mic while the one being recorded from stays muted.
 static PROPERTYKEY NameKey(){var k=new PROPERTYKEY();
  k.fmtid=new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");k.pid=14;return k;}
 static IAudioEndpointVolume Vol(string match,out string found){
  var e=(IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
  IMMDeviceCollection col; e.EnumAudioEndpoints(1,1,out col); uint n; col.GetCount(out n);
  var K=NameKey(); IAudioEndpointVolume first=null; string firstName=null; found=null;
  for(uint i=0;i<n;i++){IMMDevice d; col.Item(i,out d);
   IPropertyStore ps; d.OpenPropertyStore(0,out ps); PROPVARIANT pv; ps.GetValue(ref K,out pv);
   string name=Marshal.PtrToStringUni(pv.p); if(name==null) continue;
   Guid iid=typeof(IAudioEndpointVolume).GUID; object o; d.Activate(ref iid,1,IntPtr.Zero,out o);
   var v=(IAudioEndpointVolume)o;
   if(first==null && name.IndexOf("NVIDIA",StringComparison.OrdinalIgnoreCase)<0){first=v;firstName=name;}
   if(name.IndexOf(match,StringComparison.OrdinalIgnoreCase)>=0){found=name;return v;}}
  found=firstName; return first;}
 public static string Device(){string n; Vol("Microphone Array",out n); return n??"(none)";}
 public static bool IsMuted(){string n; var v=Vol("Microphone Array",out n);
  if(v==null) return false; bool m; v.GetMute(out m); return m;}
 public static int Volume(){string n; var v=Vol("Microphone Array",out n);
  if(v==null) return 0; float l; v.GetMasterVolumeLevelScalar(out l); return (int)(l*100);}
 public static void Unmute(){string n; var v=Vol("Microphone Array",out n); if(v==null) return;
  Guid g=Guid.Empty; v.SetMute(false,ref g);
  float l; v.GetMasterVolumeLevelScalar(out l); if(l<0.6f) v.SetMasterVolumeLevelScalar(0.85f,ref g);}}
"@
}

if (-not $NoAudio) {
    # A muted mic records perfectly valid, perfectly empty files - capture looks
    # healthy while nothing is heard. Check it here rather than days later.
    try {
        Initialize-MicInterop
        if ([DailypostMic]::IsMuted()) {
            Write-Host ""
            Write-Host "The recording microphone '$([DailypostMic]::Device())' is MUTED (volume $([DailypostMic]::Volume())%)." -ForegroundColor Yellow
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
            Write-Host "microphone '$([DailypostMic]::Device())' is live (volume $([DailypostMic]::Volume())%)" -ForegroundColor Green
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

function Register-LogonTask([string]$name, [string]$module, [string]$outDir) {
    # Launching through wscript with window style 0 is the reliable way to get a
    # truly hidden INTERACTIVE process - and it must stay interactive, because
    # screen capture needs a real desktop. (python.exe under Task Scheduler
    # otherwise flashes or keeps a console window.)
    #
    # The command is assembled with Chr(34) rather than escaped quotes: paths
    # contain spaces, and nested quote-doubling across PowerShell -> VBScript is
    # where this silently breaks.
    $vbs = Join-Path $Root "$name.vbs"
    $vbsLines = @(
        'Dim q, cmd, sh',
        'q = Chr(34)',
        "cmd = q & ""$python"" & q & "" -m $module --out-dir "" & q & ""$outDir"" & q",
        'Set sh = CreateObject("WScript.Shell")',
        # `python -m` resolves the package from the working directory
        "sh.CurrentDirectory = ""$repoRoot""",
        'sh.Run cmd, 0, False'
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
    Register-LogonTask "dailypost-record-audio" "server.capture.audio" $audioDir
}
if (-not $NoActivity) {
    Register-LogonTask "dailypost-record-activity" "server.capture.activity" $activityDir
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
    # only *.speech.opus: those are segments the local sweep has closed AND found
    # speech in. A bare *.opus also matches the segment ffmpeg is still writing,
    # which arrives truncated and fails to decode on the server.
    if (-not $NoAudio -and -not $hasAudio) { $add += "PUSH_PATH=audio|$(ConvertTo-BashPath $audioDir)|*.speech.opus" }
    if (-not $NoActivity -and -not $hasAct) { $add += "PUSH_PATH=activity|$(ConvertTo-BashPath $activityDir)|*" }
    if ($add) {
        [System.IO.File]::WriteAllText($conf, (($lines + $add) -join "`n") + "`n",
                                       (New-Object System.Text.UTF8Encoding $false))
        Write-Host "added to push.conf:`n  $($add -join "`n  ")"
    } else {
        Write-Host "push.conf already ships audio + activity"
    }
} else {
    # No push.conf means there is no separate server to push to. That is the normal
    # laptop-mode shape, not an error: the nightly runs on this machine and reads
    # the recorder folders through an ingest_dir source instead.
    Write-Host ""
    Write-Host "No laptop\push.conf - nothing will be pushed anywhere." -ForegroundColor Yellow
    Write-Host "  laptop mode (everything on this machine): register the recorder folders with"
    Write-Host "    .venv\Scripts\python.exe -m setup.wizard --source audio"
    Write-Host "    .venv\Scripts\python.exe -m setup.wizard --source activity"
    Write-Host "  server mode (push to a separate box): run setup\wizard_laptop.ps1, then re-run this."
}

Write-Host "`nStart now without logging out:"
Write-Host "  schtasks /Run /TN dailypost-record-audio"
Write-Host "  schtasks /Run /TN dailypost-record-activity"
