"""Always-on office audio capture, on Windows, macOS and Linux.

Records the room mic continuously into short Opus segments (~24 kbps mono, so a
full working day is well under 100 MB), then sweeps them locally: a segment with
almost no speech in it is deleted where it was recorded and never reaches the
store, the network, or a transcription model. Surviving segments are renamed to
`*.speech.opus`, which is what the `ingest_dir` source is configured to pick up.

PAUSE: create a file named PAUSED in the output folder and capture stops within
a few seconds; delete it to resume. Use it for private conversations — the mic
hears everyone in the room, and transcripts outlive the audio. Nothing here or
in the ingest step ever deletes that file.

Run standalone (server mode, a Windows laptop's logon task):
    python -m server.capture.audio --out-dir <dir>
In laptop mode the bot process starts `start(cfg)` in a thread instead.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..util import get_logger
from . import PAUSE_FILE, ensure_ffmpeg

log = get_logger("capture.audio")

SEGMENT_PATTERN = "office-%Y%m%d-%H%M%S.opus"
VETTED_SUFFIX = ".speech.opus"
MUTED_FLAG = "MIC-PROBABLY-MUTED.txt"
# a segment is only swept once the muxer has certainly moved on from it
FINISHED_AFTER_SECONDS = 60
MUTED_STREAK = 10


# ---------------------------------------------------------------------------
# platform-specific pieces — kept pure so they can be tested without a mic
# ---------------------------------------------------------------------------

def capture_input_args(platform: str, device: str) -> list[str]:
    """The `-f <backend> -i <device>` pair for this OS.

    Linux has two live backends: PulseAudio/PipeWire present a named source,
    bare ALSA does not. `device` selects between them — an explicit "alsa:foo"
    forces ALSA, anything else goes through pulse, which is what a desktop
    install has.
    """
    if platform == "win32":
        return ["-f", "dshow", "-i", f"audio={device}"]
    if platform == "darwin":
        # avfoundation addresses devices as "<video>:<audio>"; video is left empty
        return ["-f", "avfoundation", "-i", f":{device}"]
    if device.startswith("alsa:"):
        return ["-f", "alsa", "-i", device[len("alsa:"):] or "default"]
    return ["-f", "pulse", "-i", device or "default"]


def capture_args(platform: str, device: str, out_pattern: str, cfg=None) -> list[str]:
    """The full ffmpeg command line for continuous segmented capture."""
    def opt(key, default):
        return default if cfg is None else cfg.get(f"audio.{key}", default)

    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *capture_input_args(platform, device),
        "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", f"{int(opt('bitrate_kbps', 24))}k",
        "-application", "voip",
        # land Ogg pages promptly: the newest segment is the only thing a crash
        # can lose, and flushing keeps that loss to seconds rather than minutes
        "-flush_packets", "1",
        "-f", "segment", "-segment_time", str(int(opt("segment_seconds", 120))),
        "-reset_timestamps", "1", "-strftime", "1",
        out_pattern,
    ]


def _device_list_args(platform: str) -> list[str] | None:
    if platform == "win32":
        return ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    if platform == "darwin":
        return ["ffmpeg", "-hide_banner", "-list_devices", "true",
                "-f", "avfoundation", "-i", ""]
    return None


def parse_devices(platform: str, text: str) -> list[str]:
    """Device identifiers out of ffmpeg's `-list_devices` output (which it writes
    to stderr and then exits non-zero, by design)."""
    if platform == "win32":
        # only the audio half of the listing; dshow names are quoted
        tail = text.split("DirectShow audio devices", 1)
        return re.findall(r'"([^"]+)"\s*$', tail[-1], re.MULTILINE) if len(tail) > 1 else []
    if platform == "darwin":
        tail = text.split("AVFoundation audio devices", 1)
        # "[0] MacBook Pro Microphone" -> the index is what -i takes
        return (re.findall(r"^\[[^\]]+\]\s*\[(\d+)\]", tail[-1], re.MULTILINE)
                if len(tail) > 1 else [])
    return []


def list_devices(platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    args = _device_list_args(platform)
    if args is None:
        # pactl is the only thing that can answer this on Linux; without it the
        # wizard falls back to asking for a name, and "default" always works.
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            return []
        return [ln.split("\t")[1] for ln in out.stdout.splitlines() if "\t" in ln]
    out = subprocess.run(args, capture_output=True, text=True)
    return parse_devices(platform, out.stderr)


def preferred_device(devices: list[str], platform: str | None = None) -> str:
    """Pick a room mic, not a headset.

    A built-in microphone array is designed for far-field pickup, so it hears the
    room. Noise-suppressed virtual mics (NVIDIA Broadcast, Krisp) are tuned for a
    single speaker and actively remove every other voice — exactly the ones an
    office recording exists to capture.
    """
    if not devices:
        return "default"
    for d in devices:
        if "microphone array" in d.lower():
            return d
    for d in devices:
        if not re.search(r"nvidia|broadcast|krisp|virtual", d, re.I):
            return d
    return devices[0]


# ---------------------------------------------------------------------------
# the local silence sweep
# ---------------------------------------------------------------------------

def parse_speech_seconds(stderr: str) -> float:
    """Speech seconds in one segment = its duration minus every detected silence.

    Returns -1.0 when ffmpeg's output can't be read as a completed pass, which
    the caller treats as "keep it" — losing a real conversation is much worse
    than storing one silent segment.
    """
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", stderr)
    if not times:
        return -1.0
    h, m, s = times[-1]
    total = int(h) * 3600 + int(m) * 60 + float(s)
    if total <= 0:
        return -1.0
    silence = sum(float(v) for v in re.findall(r"silence_duration:\s*([\d.]+)", stderr))
    return max(0.0, total - silence)


def _speech_seconds(path: Path, threshold_db: int) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
         "-af", f"silencedetect=noise={threshold_db}dB:d=1.5", "-f", "null", "-"],
        capture_output=True, text=True)
    return parse_speech_seconds(out.stderr)


def _is_ffmpeg(pid: int) -> bool:
    """Guard against a recycled pid: only kill the leftover if it really is ffmpeg."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return "ffmpeg" in out.stdout.lower()
        if sys.platform == "linux":
            return "ffmpeg" in Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                "utf-8", "replace").lower()
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10)
        return "ffmpeg" in out.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def reap_orphan(pid_file: Path) -> None:
    """Kill an ffmpeg left recording by a previous run.

    ffmpeg outlives a hard-killed parent, and while it lives it holds the
    microphone open — so a fresh recorder can never acquire the device, and it
    keeps writing segments that nobody sweeps or pauses.
    """
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    if _is_ffmpeg(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("killed orphaned ffmpeg pid %d from a previous run", pid)
            time.sleep(2)
        except OSError:
            pass
    pid_file.unlink(missing_ok=True)


class _MicWarning:
    """A muted mic produces perfectly valid, perfectly empty files: capture looks
    healthy while recording nothing. Surface that rather than failing silently."""

    def __init__(self, out_dir: Path, segment_seconds: int):
        self.flag = out_dir / MUTED_FLAG
        self.segment_seconds = segment_seconds
        self.streak = 0

    def update(self, was_silent: bool) -> None:
        if not was_silent:
            self.streak = 0
            self.flag.unlink(missing_ok=True)
            return
        self.streak += 1
        if self.streak < MUTED_STREAK or self.flag.exists():
            return
        minutes = int(self.streak * self.segment_seconds / 60)
        self.flag.write_text(
            f"No speech detected in the last {self.streak} segments (~{minutes} minutes).\n"
            "If the room was not silent, the microphone is probably muted:\n"
            "  - press the mic-mute key (often F9), or\n"
            "  - unmute the microphone in your OS sound input settings\n"
            "This file is removed automatically once speech is detected again.\n",
            encoding="utf-8")
        log.warning("no speech for %d segments — mic may be muted", self.streak)


def sweep(out_dir: Path, cfg=None, warning: _MicWarning | None = None) -> int:
    """Vet every finished segment; returns how many were kept.

    Silent segments are deleted here, on the machine that recorded them, so a
    quiet day never reaches the network at all.
    """
    def opt(key, default):
        return default if cfg is None else cfg.get(f"audio.{key}", default)

    threshold = int(opt("silence_threshold_db", -35))
    min_speech = float(opt("min_speech_seconds", 4))
    cutoff = time.time() - FINISHED_AFTER_SECONDS
    kept = 0
    for f in sorted(out_dir.glob("*.opus")):
        if f.name.endswith(VETTED_SUFFIX):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue          # the muxer is probably still writing this one
        if st.st_size == 0:
            f.unlink(missing_ok=True)
            continue
        speech = _speech_seconds(f, threshold)
        if 0 <= speech < min_speech:
            f.unlink(missing_ok=True)
            log.info("dropped %s (%.1fs speech)", f.name, speech)
            if warning:
                warning.update(True)
            continue
        try:
            f.rename(f.with_name(f.name[: -len(".opus")] + VETTED_SUFFIX))
        except OSError as e:
            log.warning("could not mark %s as vetted: %s", f.name, e)
            continue
        log.info("kept %s (%.1fs speech)", f.name, speech)
        kept += 1
        if warning:
            warning.update(False)
    return kept


# ---------------------------------------------------------------------------
# the supervision loop
# ---------------------------------------------------------------------------

def status(cfg) -> str:
    """One line for the bot's /status command. A mic whose state you can't see
    is a mic you shouldn't trust."""
    if not cfg.get("audio.enabled", False):
        return "audio capture: off"
    out_dir = Path(str(cfg.get("audio.out_dir", "~/.dailypost/audio"))).expanduser()
    if (out_dir / PAUSE_FILE).exists():
        return f"audio capture: PAUSED (delete {out_dir / PAUSE_FILE} to resume)"
    segments = sorted(out_dir.glob(f"*{VETTED_SUFFIX}")) if out_dir.exists() else []
    if not segments:
        return "audio capture: on, no segments waiting"
    newest = max(s.stat().st_mtime for s in segments)
    mins = int((time.time() - newest) / 60)
    extra = "  " + MUTED_FLAG if (out_dir / MUTED_FLAG).exists() else ""
    return f"audio capture: on, {len(segments)} segment(s) waiting, newest {mins}m ago{extra}"


def record_loop(cfg, stop_event: threading.Event | None = None,
                out_dir: Path | None = None, device: str | None = None) -> None:
    stop_event = stop_event or threading.Event()
    out_dir = out_dir or Path(str(cfg.get("audio.out_dir", "~/.dailypost/audio"))).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    present, msg = ensure_ffmpeg()
    if not present:
        log.error("audio capture not started: %s", msg)
        return
    if device is None:
        device = str(cfg.get("audio.device", "") or "") or preferred_device(list_devices())
    pause_file = out_dir / PAUSE_FILE
    pid_file = out_dir / ".ffmpeg.pid"     # dotfile: ingest_dir never looks at it
    segment_seconds = int(cfg.get("audio.segment_seconds", 120))
    warning = _MicWarning(out_dir, segment_seconds)
    reap_orphan(pid_file)
    log.info("audio capture started (device %r, %ds segments) -> %s; pause with %s",
             device, segment_seconds, out_dir, pause_file)

    while not stop_event.is_set():
        if pause_file.exists():
            reap_orphan(pid_file)   # pausing must also stop an orphan from a crash
            stop_event.wait(20)
            continue
        args = capture_args(sys.platform, device, str(out_dir / SEGMENT_PATTERN), cfg)
        err_log = (out_dir / "ffmpeg.log").open("a", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=err_log)
        except OSError as e:
            log.error("could not start ffmpeg: %s — retrying in 60s", e)
            err_log.close()
            stop_event.wait(60)
            continue
        pid_file.write_text(str(proc.pid))
        last_sweep = time.time()
        while proc.poll() is None:
            if stop_event.is_set() or pause_file.exists():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if time.time() - last_sweep >= 60:
                try:
                    sweep(out_dir, cfg, warning)
                except Exception:
                    log.exception("silence sweep failed — continuing to record")
                last_sweep = time.time()
            stop_event.wait(3)
        err_log.close()
        pid_file.unlink(missing_ok=True)
        try:
            sweep(out_dir, cfg, warning)
        except Exception:
            log.exception("final silence sweep failed")
        if proc.poll() is not None and not stop_event.is_set() and not pause_file.exists():
            # device unplugged, sleep/resume, or an ffmpeg error
            log.warning("capture ended (exit %s) — retrying in 15s", proc.returncode)
            stop_event.wait(15)
    log.info("audio capture stopped")


def start(cfg) -> threading.Event:
    """Start the recorder in a daemon thread; returns its stop event."""
    stop = threading.Event()
    threading.Thread(target=record_loop, args=(cfg, stop),
                     daemon=True, name="audio-recorder").start()
    return stop


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--out-dir", help="override audio.out_dir (server-mode laptops)")
    ap.add_argument("--device", help="override audio.device")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args(argv)

    if args.list_devices:
        for d in list_devices():
            print(d)
        return 0
    from ..config import Config
    cfg = Config.load(args.config)
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
    try:
        record_loop(cfg, out_dir=out_dir, device=args.device)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
