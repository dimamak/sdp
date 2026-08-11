# Always-on office audio capture.
#
# Records the room mic continuously into short Opus segments (~24 kbps mono, so a
# full working day is well under 100 MB). Silence handling and speech detection
# happen on the SERVER, where there is CPU to spare and the logic can be tuned
# without touching every laptop.
#
# PAUSE: create a file named PAUSED in the output folder (or run stop_recording.cmd)
# and capture stops within a segment; delete it to resume. Use it for private
# conversations - the mic hears everything in the room, and transcripts outlive
# the audio.
#
# Run:  powershell -ExecutionPolicy Bypass -File laptop\record_audio.ps1
# Normally started automatically at logon by install_recorders.ps1.
param(
    [string]$Device = "",                       # dshow audio device; empty = auto-pick
    [string]$OutDir = "$env:USERPROFILE\dailypost\audio",
    [int]$SegmentSeconds = 120,                 # one file per 2 minutes: the Ogg
                                                # muxer only lands data as pages
                                                # complete, so shorter segments
                                                # bound what a crash can lose
    [int]$Bitrate = 24,                         # kbps, mono - speech only
    [int]$SilenceThresholdDb = -35,             # below this counts as silence
    [double]$MinSpeechSeconds = 4,              # segments with less speech are deleted
    [switch]$KeepSilent                         # keep every segment (debugging)
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Get-DefaultAudioDevice {
    # ffmpeg lists devices on stderr and exits non-zero by design. In PowerShell 5.1
    # `2>&1` on a native command raises NativeCommandError, which is fatal under
    # ErrorActionPreference=Stop - so redirect to a file instead.
    $tmp = Join-Path $env:TEMP "dp-devices-$PID.txt"
    $p = Start-Process -FilePath "ffmpeg" `
        -ArgumentList '-hide_banner -list_devices true -f dshow -i dummy' `
        -WindowStyle Hidden -PassThru -Wait -RedirectStandardError $tmp
    $out = Get-Content $tmp -ErrorAction SilentlyContinue
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $audio = $out | Select-String '"([^"]+)" \(audio\)' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    if (-not $audio) { throw "no dshow audio devices found" }
    # Prefer a built-in microphone array: it is designed for far-field pickup, so
    # it hears the room. Noise-suppressed virtual mics (e.g. NVIDIA Broadcast) are
    # tuned for a single speaker and actively remove other voices.
    $preferred = $audio | Where-Object { $_ -match "Microphone Array" } | Select-Object -First 1
    if (-not $preferred) { $preferred = $audio | Where-Object { $_ -notmatch "NVIDIA|Broadcast" } | Select-Object -First 1 }
    if (-not $preferred) { $preferred = $audio[0] }
    return $preferred
}

if (-not $Device) { $Device = Get-DefaultAudioDevice }
Write-Host "recording device: $Device"
Write-Host "output:           $OutDir"
Write-Host "pause:            create $OutDir\PAUSED"

$pauseFile = Join-Path $OutDir "PAUSED"

# The recorder runs hidden, so console output goes nowhere. Keep a small log:
# without it, a failing silence sweep is invisible and silence piles up.
$logFile = Join-Path $OutDir "recorder.log"
function Log([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    try { [System.IO.File]::AppendAllText($logFile, $line + "`r`n") } catch {}
    Write-Host $line
}

function Stop-OrphanedCapture {
    # ffmpeg is started detached, so killing this script (or re-registering the
    # scheduled task) leaves it recording with nobody to sweep silence or honour
    # the pause flag - and it holds the microphone, so a fresh recorder cannot
    # open the device. Clear ours out before starting.
    $mine = @(Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -like "*$OutDir*" })
    foreach ($p in $mine) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Log "killed orphaned ffmpeg pid $($p.ProcessId)"
        } catch {}
    }
    if ($mine.Count) { Start-Sleep -Seconds 2 }
}

function Get-SpeechSeconds([string]$file) {
    # One decode pass with silencedetect: total duration minus detected silence.
    # Cheaper than re-encoding, and it runs once per finished segment.
    $tmp = Join-Path $env:TEMP ("dp-sd-" + [System.IO.Path]::GetFileNameWithoutExtension($file) + ".txt")
    $a = '-hide_banner -nostdin -i "' + $file + '" -af silencedetect=noise=' +
         $SilenceThresholdDb + 'dB:d=1.5 -f null -'
    $p = Start-Process -FilePath "ffmpeg" -ArgumentList $a -WindowStyle Hidden `
        -PassThru -Wait -RedirectStandardError $tmp
    $out = Get-Content $tmp -Raw -ErrorAction SilentlyContinue
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if (-not $out) { return -1 }
    $total = 0.0
    if ($out -match 'time=(\d+):(\d+):([\d.]+)') {
        $total = [double]$Matches[1]*3600 + [double]$Matches[2]*60 + [double]$Matches[3]
    }
    $silence = 0.0
    foreach ($m in [regex]::Matches($out, 'silence_duration:\s*([\d.]+)')) {
        $silence += [double]$m.Groups[1].Value
    }
    if ($total -le 0) { return -1 }
    return [Math]::Max(0.0, $total - $silence)
}

$script:SilentStreak = 0

function Update-MicWarning([bool]$wasSilent) {
    # A muted mic produces perfectly valid, perfectly empty files: capture looks
    # healthy while recording nothing. Surface that instead of failing silently.
    $flag = Join-Path $OutDir "MIC-PROBABLY-MUTED.txt"
    if ($wasSilent) {
        $script:SilentStreak++
        if ($script:SilentStreak -ge 10 -and -not (Test-Path $flag)) {
            Set-Content -Path $flag -Encoding ascii -Value @"
No speech detected in the last $($script:SilentStreak) segments (~$([int]($script:SilentStreak * $SegmentSeconds / 60)) minutes).
If the office was not silent, the microphone is probably muted:
  - press the mic-mute key (often F9), or
  - Settings > System > Sound > Input > unmute the microphone
This file is removed automatically once speech is detected again.
"@
            Write-Host "WARNING: no speech for $($script:SilentStreak) segments - mic may be muted"
        }
    } else {
        $script:SilentStreak = 0
        Remove-Item $flag -Force -ErrorAction SilentlyContinue
    }
}

function Remove-SilentSegments {
    # Only look at finished files: the segment muxer is still writing the newest one.
    $cutoff = (Get-Date).AddSeconds(-60)
    Get-ChildItem (Join-Path $OutDir "*.opus") -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff -and $_.Name -notlike "*.speech.opus" } |
        ForEach-Object {
            if ($_.Length -eq 0) { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; return }
            $speech = Get-SpeechSeconds $_.FullName
            if ($speech -ge 0 -and $speech -lt $MinSpeechSeconds) {
                Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                Log ("dropped {0} ({1:N1}s speech)" -f $_.Name, $speech)
                Update-MicWarning $true
            } else {
                Log ("kept {0} ({1:N1}s speech)" -f $_.Name, $speech)
                Update-MicWarning $false
                # mark as checked so it is not re-analysed every loop
                Rename-Item $_.FullName ($_.BaseName + ".speech.opus") -ErrorAction SilentlyContinue
            }
        }
}

Stop-OrphanedCapture
Log "recorder started (device: $Device, segments ${SegmentSeconds}s)"

while ($true) {
    if (Test-Path $pauseFile) {
        Stop-OrphanedCapture     # pause must also stop an orphan from a crash
        Start-Sleep -Seconds 20
        continue
    }

    # -f segment writes one file per SegmentSeconds; -strftime names it by wall
    # clock so the server knows when each chunk happened.
    $pattern = Join-Path $OutDir "office-%Y%m%d-%H%M%S.opus"
    # One quoted argument string: the device name contains spaces and parentheses,
    # and -ArgumentList with an array does not quote elements individually.
    $ffArgs = '-hide_banner -loglevel error -nostdin ' +
              '-f dshow -i "audio=' + $Device + '" ' +
              '-ac 1 -ar 16000 -c:a libopus -b:a ' + $Bitrate + 'k -application voip ' +
              '-flush_packets 1 ' +
              '-f segment -segment_time ' + $SegmentSeconds + ' -reset_timestamps 1 -strftime 1 ' +
              '"' + $pattern + '"'

    # Hidden window (not -NoNewWindow): under a scheduled task there is no console
    # to inherit, and -NoNewWindow then fails to launch the child at all.
    $errLog = Join-Path $OutDir "ffmpeg.log"
    $proc = Start-Process -FilePath "ffmpeg" -ArgumentList $ffArgs `
        -WindowStyle Hidden -PassThru -RedirectStandardError $errLog
    $lastSweep = Get-Date
    while (-not $proc.HasExited) {
        if (Test-Path $pauseFile) {
            Write-Host "paused - stopping capture"
            try { $proc.CloseMainWindow() | Out-Null; Start-Sleep 1 } catch {}
            try { if (-not $proc.HasExited) { $proc.Kill() } } catch {}
            break
        }
        # discard silent segments locally, so a quiet day never reaches the
        # network or the server at all
        if (-not $KeepSilent -and ((Get-Date) - $lastSweep).TotalSeconds -ge 60) {
            try { Remove-SilentSegments } catch { Log "sweep failed: $($_.Exception.Message)" }
            $lastSweep = Get-Date
        }
        Start-Sleep -Seconds 3
    }
    if (-not $KeepSilent) { try { Remove-SilentSegments } catch {} }
    if ($proc.HasExited -and -not (Test-Path $pauseFile)) {
        # device unplugged, sleep/resume, or an ffmpeg error - back off and retry
        Write-Host "capture ended (exit $($proc.ExitCode)) - retrying in 15s"
        Start-Sleep -Seconds 15
    }
}
