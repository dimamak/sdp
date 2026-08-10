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
    [int]$SegmentSeconds = 600,                 # one file per 10 minutes
    [int]$Bitrate = 24                          # kbps, mono - speech only
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

while ($true) {
    if (Test-Path $pauseFile) {
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
              '-f segment -segment_time ' + $SegmentSeconds + ' -reset_timestamps 1 -strftime 1 ' +
              '"' + $pattern + '"'

    # Hidden window (not -NoNewWindow): under a scheduled task there is no console
    # to inherit, and -NoNewWindow then fails to launch the child at all.
    $errLog = Join-Path $OutDir "ffmpeg.log"
    $proc = Start-Process -FilePath "ffmpeg" -ArgumentList $ffArgs `
        -WindowStyle Hidden -PassThru -RedirectStandardError $errLog
    while (-not $proc.HasExited) {
        if (Test-Path $pauseFile) {
            Write-Host "paused - stopping capture"
            try { $proc.CloseMainWindow() | Out-Null; Start-Sleep 1 } catch {}
            try { if (-not $proc.HasExited) { $proc.Kill() } } catch {}
            break
        }
        Start-Sleep -Seconds 3
    }
    if ($proc.HasExited -and -not (Test-Path $pauseFile)) {
        # device unplugged, sleep/resume, or an ffmpeg error - back off and retry
        Write-Host "capture ended (exit $($proc.ExitCode)) - retrying in 15s"
        Start-Sleep -Seconds 15
    }
}
