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

function Save-Screenshot([string]$path) {
    # whole virtual desktop, so multi-monitor setups are captured too
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    try {
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
        $g.Dispose()
        $scale = [Math]::Min(1.0, $MaxWidth / $b.Width)
        $w = [int]($b.Width * $scale); $h = [int]($b.Height * $scale)
        $small = New-Object System.Drawing.Bitmap $bmp, $w, $h
        try { $small.Save($path, $jpegEncoder, $encParams) } finally { $small.Dispose() }
    } finally { $bmp.Dispose() }
}

$pauseFile = Join-Path $OutDir "PAUSED"
$lastTitle = ""
$lastApp = ""
$lastShot = [DateTime]::MinValue

Write-Host "activity log: $OutDir  (interval ${IntervalSeconds}s, pause: $pauseFile)"

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    if (Test-Path $pauseFile) { continue }

    try {
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
            Add-Content -Path $logFile -Value $line -Encoding utf8
            $lastTitle = $title
        }

        $appChanged = ($app -ne $lastApp)
        $sinceShot = ($now - $lastShot).TotalSeconds
        $wantShot = ($appChanged -and $sinceShot -ge $MinShotIntervalSeconds) -or
                    ($sinceShot -ge $MaxShotIntervalSeconds)
        if ($wantShot -and ($SkipShotApps -notcontains $app)) {
            $name = "screen-" + $now.ToString("yyyyMMdd-HHmmss") + "-" + $app + ".jpg"
            Save-Screenshot (Join-Path $OutDir $name)
            $lastShot = $now
        }
        $lastApp = $app
    } catch {
        Write-Host "activity sample failed: $($_.Exception.Message)"
    }
}
