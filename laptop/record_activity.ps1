# Captures what you do OUTSIDE the coding agent: browsing, dashboards, docs,
# design, admin. Two complementary streams, both cheap:
#
#   1. activity log  - the foreground window title every $IntervalSeconds,
#                      written deduplicated as NDJSON. A few KB per day, and it
#                      tells the model exactly where non-coding time went.
#   2. screenshots   - taken only when the foreground APP changes (plus a
#                      periodic floor), downscaled JPEG. Tens of images per day
#                      instead of hundreds, so the vision pass stays cheap.
#
# Coding apps are still logged by title but skipped for screenshots - that work
# is already captured in full by the Claude Code transcripts.
#
# PAUSE: create a file named PAUSED in the output folder; delete it to resume.
param(
    [string]$OutDir = "$env:USERPROFILE\dailypost\activity",
    [int]$IntervalSeconds = 30,             # how often the foreground is sampled
    [int]$MinShotIntervalSeconds = 120,     # never screenshot more often than this
    [int]$MaxShotIntervalSeconds = 900,     # screenshot at least this often while active
    [int]$MaxWidth = 1600,                  # downscale target
    [int]$JpegQuality = 55,
    [int]$IdleSkipSeconds = 300,            # stop capturing after this much inactivity
    [int]$DedupDistance = 3,                # perceptual-hash distance below which a
                                            # screen counts as unchanged and is skipped
    [string[]]$SkipShotApps = @("WindowsTerminal", "Code", "devenv", "cmd",
                                "powershell", "pwsh", "conhost")
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Fg {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    // seconds since the last keyboard or mouse input - nobody at the machine
    public static double IdleSeconds() {
        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(lii);
        if (!GetLastInputInfo(ref lii)) return 0;
        return (Environment.TickCount - (long)lii.dwTime) / 1000.0;
    }
    public static string Title() {
        IntPtr h = GetForegroundWindow();
        if (h == IntPtr.Zero) return "";
        StringBuilder sb = new StringBuilder(512);
        GetWindowText(h, sb, 512);
        return sb.ToString();
    }
    public static uint Pid() {
        IntPtr h = GetForegroundWindow();
        uint pid = 0;
        if (h != IntPtr.Zero) GetWindowThreadProcessId(h, out pid);
        return pid;
    }
}
"@

function Get-JpegEncoder {
    [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
        Where-Object { $_.MimeType -eq "image/jpeg" } | Select-Object -First 1
}
$jpegEncoder = Get-JpegEncoder
$encParams = New-Object System.Drawing.Imaging.EncoderParameters 1
$encParams.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter(
    [System.Drawing.Imaging.Encoder]::Quality, [int]$JpegQuality)

function Get-AHash([System.Drawing.Bitmap]$bmp) {
    # 64-bit average hash: robust to a ticking clock or a blinking cursor, but
    # different as soon as the actual content changes
    $t = New-Object System.Drawing.Bitmap $bmp, 8, 8
    try {
        $vals = New-Object 'System.Collections.Generic.List[double]'
        for ($y = 0; $y -lt 8; $y++) {
            for ($x = 0; $x -lt 8; $x++) {
                $p = $t.GetPixel($x, $y)
                $vals.Add($p.R * 0.299 + $p.G * 0.587 + $p.B * 0.114)
            }
        }
        $avg = ($vals | Measure-Object -Average).Average
        return -join ($vals | ForEach-Object { if ($_ -ge $avg) { '1' } else { '0' } })
    } finally { $t.Dispose() }
}

function Get-HashDistance([string]$a, [string]$b) {
    if (-not $a -or -not $b -or $a.Length -ne $b.Length) { return 999 }
    $d = 0
    for ($i = 0; $i -lt $a.Length; $i++) { if ($a[$i] -ne $b[$i]) { $d++ } }
    return $d
}

# Returns the perceptual hash of what was captured, or $null if the screen was
# unchanged (nothing written).
function Save-Screenshot([string]$path, [string]$prevHash) {
    # whole virtual desktop, so multi-monitor setups are captured too
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    try {
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
        $g.Dispose()
        $hash = Get-AHash $bmp
        if ((Get-HashDistance $hash $prevHash) -le $DedupDistance) { return $null }
        $scale = [Math]::Min(1.0, $MaxWidth / $b.Width)
        $w = [int]($b.Width * $scale); $h = [int]($b.Height * $scale)
        $small = New-Object System.Drawing.Bitmap $bmp, $w, $h
        try { $small.Save($path, $jpegEncoder, $encParams) } finally { $small.Dispose() }
        return $hash
    } finally { $bmp.Dispose() }
}

$pauseFile = Join-Path $OutDir "PAUSED"
$lastTitle = ""
$lastApp = ""
$lastShot = [DateTime]::MinValue
$lastHash = ""

Write-Host "activity log: $OutDir  (interval ${IntervalSeconds}s, pause: $pauseFile)"

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    if (Test-Path $pauseFile) { continue }

    try {
        # away from the machine: the screen is not changing and nothing is being
        # done, so neither a log line nor a screenshot carries information
        if ([Fg]::IdleSeconds() -ge $IdleSkipSeconds) { continue }
        $title = [Fg]::Title()
        if (-not $title) { continue }
        $pid_ = [Fg]::Pid()
        $app = try { (Get-Process -Id $pid_ -ErrorAction Stop).ProcessName } catch { "?" }
        $now = Get-Date

        # log only when the window actually changed - a day of unchanged screens
        # should cost nothing
        if ($title -ne $lastTitle) {
            $rec = [ordered]@{ ts = $now.ToUniversalTime().ToString("o"); app = $app; title = $title }
            $line = ($rec | ConvertTo-Json -Compress)
            $logFile = Join-Path $OutDir ("activity-" + $now.ToString("yyyyMMdd") + ".ndjson")
            # UTF-8 without BOM: Add-Content -Encoding utf8 prefixes a BOM on
            # creation, which breaks JSON parsing of the first line server-side
            [System.IO.File]::AppendAllText($logFile, $line + "`n",
                                            (New-Object System.Text.UTF8Encoding $false))
            $lastTitle = $title
        }

        $appChanged = ($app -ne $lastApp)
        $sinceShot = ($now - $lastShot).TotalSeconds
        $wantShot = ($appChanged -and $sinceShot -ge $MinShotIntervalSeconds) -or
                    ($sinceShot -ge $MaxShotIntervalSeconds)
        if ($wantShot -and ($SkipShotApps -notcontains $app)) {
            $name = "screen-" + $now.ToString("yyyyMMdd-HHmmss") + "-" + $app + ".jpg"
            $h = Save-Screenshot (Join-Path $OutDir $name) $lastHash
            # $null means the screen was visually identical to the last one saved
            if ($h) { $lastHash = $h }
            $lastShot = $now
        }
        $lastApp = $app
    } catch {
        Write-Host "activity sample failed: $($_.Exception.Message)"
    }
}
