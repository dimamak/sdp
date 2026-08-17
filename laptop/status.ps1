# Laptop-side health check - the counterpart to `setup.wizard --doctor` on the server.
# Answers "is this machine actually feeding the pipeline?" in one command.
#
#   powershell -ExecutionPolicy Bypass -File laptop\status.ps1
param(
    [string]$Root = "$env:USERPROFILE\dailypost"
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = 0; $bad = 0
function Pass($m) { Write-Host "  [ok]   $m" -ForegroundColor Green; $script:ok++ }
function Fail($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:bad++ }
function Info($m) { Write-Host "         $m" -ForegroundColor DarkGray }

Write-Host "`n=== dailypost laptop status ===" -ForegroundColor Cyan

# --- push configuration and connectivity ------------------------------------
$conf = Join-Path $scriptDir "push.conf"
if (-not (Test-Path $conf)) {
    Fail "push.conf missing - run setup\wizard_laptop.ps1"
} else {
    $remote = (Select-String -Path $conf -Pattern '^REMOTE=(.+)$').Matches.Groups[1].Value.Trim()
    $rdir = (Select-String -Path $conf -Pattern '^REMOTE_DIR=(.+)$').Matches.Groups[1].Value.Trim()
    Pass "push.conf -> $remote : $rdir"
    # A moved server is the classic breakage: the alias resolves but the host changed.
    $probe = & ssh -o BatchMode=yes -o ConnectTimeout=10 $remote "echo ok; hostname" 2>$null
    if ($probe -match "ok") { Pass "ssh to '$remote' works ($($probe[-1]))" }
    else { Fail "ssh to '$remote' FAILED - check ~/.ssh/config if the server moved" }
}

# --- scheduled tasks ---------------------------------------------------------
foreach ($t in @("dailypost-push", "dailypost-record-activity", "dailypost-record-audio")) {
    $q = schtasks /Query /TN $t /FO LIST /V 2>$null
    if (-not $q) { Fail "$t not registered"; continue }
    $status = (($q | Select-String '^Status:').Line -split ':')[1].Trim()
    $result = (($q | Select-String 'Last Result:').Line -split ':')[1].Trim()
    $last = (($q | Select-String 'Last Run Time:').Line -split ':', 2)[1].Trim()
    if ($t -eq "dailypost-push") {
        # 0 = last run succeeded; 267011 = never run
        if ($result -eq "0") { Pass "$t last run $last (ok)" }
        else { Fail "$t last run $last returned $result" }
    } else {
        # Recorders are launched through wscript, which returns immediately, so
        # the TASK completes while the recorder keeps running. Task state says
        # nothing about capture — the process check below is what matters here.
        Info "$t registered (last run $last)"
    }
}

# --- processes: exactly one of each, no orphans ------------------------------
$recs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match 'record_(audio|activity)' })
foreach ($kind in @('activity', 'audio')) {
    $n = @($recs | Where-Object { $_.CommandLine -match "record_$kind" }).Count
    if ($n -eq 1) { Pass "record_$kind is capturing" }
    elseif ($n -eq 0) { Fail "record_$kind NOT running - start: schtasks /Run /TN dailypost-record-$kind" }
    else { Fail "$n record_$kind processes - duplicates double-log and fight over the mic" }
}
$ff = @(Get-Process ffmpeg -ErrorAction SilentlyContinue).Count
if ($ff -le 1) { Pass "ffmpeg processes: $ff" }
else { Fail "$ff ffmpeg processes - an orphan survives when a recorder is killed" }

# --- captured data freshness -------------------------------------------------
foreach ($d in @(@{n = "audio"; p = "$Root\audio"; g = "*.speech.opus" },
                 @{n = "activity"; p = "$Root\activity"; g = "*" })) {
    $files = @(Get-ChildItem (Join-Path $d.p $d.g) -File -ErrorAction SilentlyContinue)
    if (-not $files) { Info "$($d.n): no files yet in $($d.p)"; continue }
    $newest = ($files | Sort-Object LastWriteTime -Desc)[0]
    $ageMin = [int]((Get-Date) - $newest.LastWriteTime).TotalMinutes
    if ($ageMin -le 60) { Pass "$($d.n): $($files.Count) files, newest ${ageMin}m ago" }
    else { Info "$($d.n): $($files.Count) files, newest ${ageMin}m ago (idle or paused?)" }
}
if (Test-Path (Join-Path $Root "audio\MIC-PROBABLY-MUTED.txt")) {
    Fail "MIC-PROBABLY-MUTED.txt present - no speech detected for a long stretch"
}
foreach ($p in @("$Root\audio\PAUSED", "$Root\activity\PAUSED")) {
    if (Test-Path $p) { Fail "recording PAUSED ($p) - run RESUME-recording.cmd" }
}

# --- microphone --------------------------------------------------------------
# NOTE: the closing "@ of a here-string must start at column 0, so this block is
# deliberately not indented inside the try below.
$micSrc = @"
using System;using System.Runtime.InteropServices;
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IE3{int EnumAudioEndpoints(int d,int m,out IC3 c);int GetDefaultAudioEndpoint(int d,int r,out ID3 e);}
[Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IC3{int GetCount(out uint c);int Item(uint i,out ID3 d);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface ID3{int Activate(ref Guid id,int ctx,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);
 int OpenPropertyStore(int a,out IP3 s);int GetId([MarshalAs(UnmanagedType.LPWStr)]out string id);int GetState(out int st);}
[Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IP3{int GetCount(out int c);int GetAt(int i,out PK3 k);int GetValue(ref PK3 k,out PV3 v);}
[StructLayout(LayoutKind.Sequential)] public struct PK3{public Guid fmtid;public int pid;}
[StructLayout(LayoutKind.Explicit)] public struct PV3{[FieldOffset(0)]public short vt;[FieldOffset(8)]public IntPtr p;}
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IV3{int R(IntPtr n);int U(IntPtr n);int GetChannelCount(out uint c);
 int SetMasterVolumeLevel(float l,ref Guid g);int SetMasterVolumeLevelScalar(float l,ref Guid g);
 int GetMasterVolumeLevel(out float l);int GetMasterVolumeLevelScalar(out float l);
 int SetChannelVolumeLevel(uint i,float l,ref Guid g);int SetChannelVolumeLevelScalar(uint i,float l,ref Guid g);
 int GetChannelVolumeLevel(uint i,out float l);int GetChannelVolumeLevelScalar(uint i,out float l);
 int SetMute([MarshalAs(UnmanagedType.Bool)]bool m,ref Guid g);int GetMute([MarshalAs(UnmanagedType.Bool)]out bool m);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class EC3{}
public class DPStatusMic{
 public static string Check(){
  var e=(IE3)(new EC3()); IC3 col; e.EnumAudioEndpoints(1,1,out col); uint n; col.GetCount(out n);
  var K=new PK3(); K.fmtid=new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"); K.pid=14; string best=null;
  for(uint i=0;i<n;i++){ID3 d; col.Item(i,out d); IP3 ps; d.OpenPropertyStore(0,out ps);
   PV3 pv; ps.GetValue(ref K,out pv); string name=Marshal.PtrToStringUni(pv.p); if(name==null) continue;
   if(name.IndexOf("Microphone Array",StringComparison.OrdinalIgnoreCase)<0) continue;
   Guid iid=typeof(IV3).GUID; object o; d.Activate(ref iid,1,IntPtr.Zero,out o); var v=(IV3)o;
   bool m; float lv; v.GetMute(out m); v.GetMasterVolumeLevelScalar(out lv);
   best=name+"|"+m+"|"+(int)(lv*100);}
  return best;}}
"@

try {
    if (-not ("DPStatusMic" -as [type])) { Add-Type -TypeDefinition $micSrc }
    $mic = [DPStatusMic]::Check()
    if (-not $mic) { Info "no 'Microphone Array' device found" }
    else {
        $parts = $mic -split '\|'
        if ($parts[1] -eq "True") { Fail "mic '$($parts[0])' is MUTED - it records silence" }
        else { Pass "mic '$($parts[0])' live (volume $($parts[2])%)" }
    }
} catch { Info "could not read microphone state: $($_.Exception.Message)" }

Write-Host "`n$ok ok, $bad problem(s)`n" -ForegroundColor $(if ($bad) { "Red" } else { "Green" })
