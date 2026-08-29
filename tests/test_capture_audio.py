"""Covers server/capture/audio.py's platform-dependent and parsing logic.

Everything here is pure: no ffmpeg, no microphone, no OS-specific imports — the
CI matrix runs the same assertions on ubuntu, macOS and Windows. The recorder is
structured so the only per-OS piece is `capture_input_args`, which is exactly
what these tests pin down.
"""
import time

import pytest

from server.capture.audio import (VETTED_SUFFIX, capture_args, capture_input_args,
                                  parse_devices, parse_speech_seconds, preferred_device,
                                  sweep)


# ---------------------------------------------------------------------------
# capture_args
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform,device,expected", [
    ("win32", "Microphone Array (Realtek)", ["-f", "dshow", "-i", "audio=Microphone Array (Realtek)"]),
    ("darwin", "1", ["-f", "avfoundation", "-i", ":1"]),
    ("linux", "alsa_input.pci-0000_00_1f.3", ["-f", "pulse", "-i", "alsa_input.pci-0000_00_1f.3"]),
    ("linux", "", ["-f", "pulse", "-i", "default"]),
    ("linux", "alsa:hw:1", ["-f", "alsa", "-i", "hw:1"]),
])
def test_capture_input_args_per_platform(platform, device, expected):
    assert capture_input_args(platform, device) == expected


def test_capture_args_share_everything_after_the_input():
    win = capture_args("win32", "Mic", "/tmp/out-%Y.opus")
    lin = capture_args("linux", "default", "/tmp/out-%Y.opus")
    # the encoder half must be identical, or the server would need per-OS decoding
    assert win[win.index("-ac"):] == lin[lin.index("-ac"):]


def test_capture_args_segment_the_stream_and_name_by_wall_clock():
    args = capture_args("linux", "default", "/tmp/out-%Y%m%d.opus")
    assert args[-1] == "/tmp/out-%Y%m%d.opus"
    for flag in ("-f", "segment"), ("-strftime", "1"), ("-c:a", "libopus"):
        i = args.index(flag[0])
        assert flag[1] in args[i:]


def test_capture_args_read_the_config(tmp_path):
    from server.config import Config
    cfg = Config({"audio": {"segment_seconds": 30, "bitrate_kbps": 48}}, {},
                 tmp_path / "config.yaml")
    args = capture_args("linux", "default", "out.opus", cfg)
    assert args[args.index("-segment_time") + 1] == "30"
    assert "48k" in args


# ---------------------------------------------------------------------------
# device listing and choice
# ---------------------------------------------------------------------------

WIN_LISTING = """[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000]  "Integrated Webcam"
[dshow @ 000] DirectShow audio devices
[dshow @ 000]  "Microphone Array (Realtek(R) Audio)"
[dshow @ 000]  "Microphone (NVIDIA Broadcast)"
"""

MAC_LISTING = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x1] [1] External Mic
"""


def test_parse_devices_windows_ignores_the_video_half():
    assert parse_devices("win32", WIN_LISTING) == [
        "Microphone Array (Realtek(R) Audio)", "Microphone (NVIDIA Broadcast)"]


def test_parse_devices_macos_returns_indices():
    assert parse_devices("darwin", MAC_LISTING) == ["0", "1"]


def test_preferred_device_picks_the_room_mic_over_a_noise_suppressed_one():
    # a noise-suppressed virtual mic strips every voice but one — the opposite of
    # what an office recording is for
    assert preferred_device(["Microphone (NVIDIA Broadcast)",
                             "Microphone Array (Realtek(R) Audio)"]) == \
        "Microphone Array (Realtek(R) Audio)"


def test_preferred_device_avoids_virtual_mics_when_there_is_no_array():
    assert preferred_device(["Microphone (NVIDIA Broadcast)", "Headset Mic"]) == "Headset Mic"


def test_preferred_device_falls_back_rather_than_failing():
    assert preferred_device(["Microphone (NVIDIA Broadcast)"]) == "Microphone (NVIDIA Broadcast)"
    assert preferred_device([]) == "default"


# ---------------------------------------------------------------------------
# silencedetect parsing — the local sweep's only judgement call
# ---------------------------------------------------------------------------

SILENCEDETECT = """[silencedetect @ 0x55] silence_start: 0
[silencedetect @ 0x55] silence_end: 42.5 | silence_duration: 42.5
[silencedetect @ 0x55] silence_start: 55.1
[silencedetect @ 0x55] silence_end: 110 | silence_duration: 54.9
size=N/A time=00:02:00.00 bitrate=N/A speed= 120x
"""


def test_speech_seconds_is_duration_minus_every_silence():
    assert parse_speech_seconds(SILENCEDETECT) == pytest.approx(120 - 42.5 - 54.9)


def test_speech_seconds_of_a_wholly_silent_segment_is_zero():
    text = ("[silencedetect] silence_end: 120 | silence_duration: 120\n"
            "time=00:02:00.00\n")
    assert parse_speech_seconds(text) == 0.0


def test_unreadable_output_returns_minus_one_so_the_segment_is_kept():
    # losing a real conversation is much worse than storing one silent segment
    assert parse_speech_seconds("") == -1.0
    assert parse_speech_seconds("time=00:00:00.00") == -1.0


# ---------------------------------------------------------------------------
# sweep — with _speech_seconds stubbed, no ffmpeg needed
# ---------------------------------------------------------------------------

def _aged(path, seconds=120):
    import os
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_sweep_keeps_speech_and_drops_silence(tmp_path, monkeypatch):
    talk, quiet = tmp_path / "a.opus", tmp_path / "b.opus"
    talk.write_bytes(b"x"), quiet.write_bytes(b"x")
    _aged(talk), _aged(quiet)
    monkeypatch.setattr("server.capture.audio._speech_seconds",
                        lambda p, t: 30.0 if p.name == "a.opus" else 0.5)
    assert sweep(tmp_path) == 1
    assert (tmp_path / f"a{VETTED_SUFFIX}").exists()
    assert not quiet.exists()


def test_sweep_leaves_the_segment_still_being_written(tmp_path, monkeypatch):
    fresh = tmp_path / "now.opus"
    fresh.write_bytes(b"x")
    monkeypatch.setattr("server.capture.audio._speech_seconds",
                        lambda p, t: pytest.fail("must not analyse an open segment"))
    assert sweep(tmp_path) == 0
    assert fresh.exists()


def test_sweep_does_not_reanalyse_vetted_segments(tmp_path, monkeypatch):
    vetted = tmp_path / f"a{VETTED_SUFFIX}"
    vetted.write_bytes(b"x")
    _aged(vetted)
    monkeypatch.setattr("server.capture.audio._speech_seconds",
                        lambda p, t: pytest.fail("already vetted"))
    assert sweep(tmp_path) == 0
    assert vetted.exists()


def test_sweep_keeps_a_segment_it_cannot_measure(tmp_path, monkeypatch):
    seg = tmp_path / "a.opus"
    seg.write_bytes(b"x")
    _aged(seg)
    monkeypatch.setattr("server.capture.audio._speech_seconds", lambda p, t: -1.0)
    assert sweep(tmp_path) == 1


def test_sweep_never_touches_the_pause_flag(tmp_path, monkeypatch):
    (tmp_path / "PAUSED").write_text("")
    monkeypatch.setattr("server.capture.audio._speech_seconds", lambda p, t: 0.0)
    sweep(tmp_path)
    assert (tmp_path / "PAUSED").exists()


def test_mic_warning_appears_after_a_silent_streak_and_clears(tmp_path):
    from server.capture.audio import MUTED_FLAG, MUTED_STREAK, _MicWarning
    w = _MicWarning(tmp_path, 120)
    for _ in range(MUTED_STREAK - 1):
        w.update(True)
    assert not (tmp_path / MUTED_FLAG).exists()
    w.update(True)
    assert (tmp_path / MUTED_FLAG).exists()
    w.update(False)
    assert not (tmp_path / MUTED_FLAG).exists()


# ---------------------------------------------------------------------------
# the pause flag short-circuits the loop without ever launching ffmpeg
# ---------------------------------------------------------------------------

def test_record_loop_never_starts_ffmpeg_while_paused(tmp_path, monkeypatch):
    import threading

    from server.config import Config
    from server.capture.audio import record_loop

    (tmp_path / "PAUSED").write_text("")
    monkeypatch.setattr("server.capture.audio.ensure_ffmpeg", lambda *a: (True, "stub"))
    monkeypatch.setattr("server.capture.audio.subprocess.Popen",
                        lambda *a, **k: pytest.fail("paused: ffmpeg must not start"))
    stop = threading.Event()

    real_wait = stop.wait

    def wait_then_stop(timeout=None):
        stop.set()          # let the loop make exactly one pass
        return real_wait(0)

    monkeypatch.setattr(stop, "wait", wait_then_stop)
    cfg = Config({"audio": {"device": "default"}}, {}, tmp_path / "config.yaml")
    record_loop(cfg, stop, out_dir=tmp_path, device="default")
