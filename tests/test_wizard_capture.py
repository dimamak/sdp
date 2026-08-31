"""The wizard's audio and activity steps are the only supported way to turn
capture on.

What matters here is not the prompts but the config left behind: a drainer narrow
enough that it can never eat the PAUSED flag out of a live recorder's directory,
and wide enough that the doctor's audio check agrees audio will actually arrive.
Those two used to disagree silently — transcription on, nothing able to produce a
kind='audio' item, no error, every night.

Both steps default to OFF: a microphone or a screen recorder that someone enabled
by pressing enter is a bug, not a convenience.
"""
from pathlib import Path

import pytest

import setup.wizard as wizard
from server.harvest.ingest_dir import _matches, admits_audio


@pytest.fixture
def answers(monkeypatch):
    """Scripted replies so the step runs unattended. An empty queue means every
    prompt takes its own default — which is how the consent default is tested."""
    state = {"ask": [], "yes": []}
    monkeypatch.setattr(wizard, "ask",
                        lambda prompt, default="": state["ask"].pop(0) if state["ask"] else default)
    monkeypatch.setattr(wizard, "yes",
                        lambda prompt, default=True: (
                            state["yes"].pop(0) if state["yes"] else default))
    monkeypatch.setattr(wizard, "have_module", lambda name: True)
    monkeypatch.setattr(wizard, "install_extra",
                        lambda req: pytest.fail("faster-whisper is already installed"))
    monkeypatch.setattr("server.capture.ensure_ffmpeg", lambda *a: (True, "ffmpeg (stub)"))
    monkeypatch.setattr("server.capture.audio.list_devices",
                        lambda *a: ["Microphone Array (Realtek)"])
    return state


def _laptop(tmp_path) -> dict:
    return {"mode": "laptop", "ingest_dir": str(tmp_path)}


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------

def test_pressing_enter_at_every_prompt_leaves_the_mic_off(answers, tmp_path):
    # nobody gets a room microphone switched on by not reading the question
    data = _laptop(tmp_path)
    wizard.step_audio(data)
    assert data["audio"]["enabled"] is False
    assert data["transcription"]["enabled"] is False
    assert data.get("sources", []) == []


def test_declining_also_turns_transcription_back_off(answers, tmp_path):
    data = _laptop(tmp_path)
    data["transcription"] = {"enabled": True}
    answers["yes"] = [False]
    wizard.step_audio(data)
    assert data["transcription"]["enabled"] is False


# ---------------------------------------------------------------------------
# what enabling actually writes
# ---------------------------------------------------------------------------

def _enable(data, answers, yeses=(True,)):
    answers["yes"] = list(yeses)
    wizard.step_audio(data)
    return next(s for s in data["sources"] if s.get("name") == "audio")


def test_enabling_registers_a_drainer_the_doctor_accepts(answers, tmp_path):
    data = _laptop(tmp_path)
    src = _enable(data, answers)
    assert src["type"] == "ingest_dir" and src["enabled"] is True
    assert admits_audio(src), "the doctor would report 'no source can produce audio'"
    assert data["audio"]["enabled"] is True
    assert data["transcription"]["enabled"] is True


def test_the_drainer_cannot_eat_the_control_files(answers, tmp_path):
    from server.capture.audio import MUTED_FLAG

    data = _laptop(tmp_path)
    src = _enable(data, answers)
    for control in ("PAUSED", MUTED_FLAG, "ffmpeg.log"):
        assert _matches(src["exclude"], control, control), f"{control} would be ingested"


def test_the_drainer_only_takes_segments_the_sweep_has_vetted(answers, tmp_path):
    from server.capture.audio import VETTED_SUFFIX

    data = _laptop(tmp_path)
    src = _enable(data, answers)
    assert src["include"] == [f"*{VETTED_SUFFIX}"]
    # ffmpeg still has the current segment open; 90s comfortably outlives the
    # 120s default only once the file stops being written
    assert src["min_age_seconds"] >= 60


def test_the_spool_dir_is_created(answers, tmp_path):
    data = _laptop(tmp_path)
    src = _enable(data, answers)
    assert Path(src["path"]).is_dir()
    assert data["audio"]["out_dir"] == src["path"]


# ---------------------------------------------------------------------------
# device choice
# ---------------------------------------------------------------------------

def test_the_suggested_device_is_the_room_mic_not_the_noise_suppressed_one(
        answers, tmp_path, monkeypatch):
    monkeypatch.setattr("server.capture.audio.list_devices",
                        lambda *a: ["Microphone (NVIDIA Broadcast)", "Microphone Array (Realtek)"])
    data = _laptop(tmp_path)
    _enable(data, answers)
    assert data["audio"]["device"] == "Microphone Array (Realtek)"


def test_an_explicit_number_overrides_the_suggestion(answers, tmp_path, monkeypatch):
    monkeypatch.setattr("server.capture.audio.list_devices",
                        lambda *a: ["Microphone (NVIDIA Broadcast)", "Microphone Array (Realtek)"])
    data = _laptop(tmp_path)
    answers["ask"] = ["", "1"]  # default spool dir, then device 1
    _enable(data, answers)
    assert data["audio"]["device"] == "Microphone (NVIDIA Broadcast)"


def test_an_out_of_range_number_falls_back_instead_of_crashing(answers, tmp_path):
    data = _laptop(tmp_path)
    answers["ask"] = ["", "99"]
    _enable(data, answers)
    assert data["audio"]["device"] == "Microphone Array (Realtek)"


def test_missing_ffmpeg_still_writes_the_config(answers, tmp_path, monkeypatch):
    # the recorder waits for ffmpeg rather than the setup being lost
    monkeypatch.setattr("server.capture.ensure_ffmpeg", lambda *a: (False, "ffmpeg not on PATH"))
    monkeypatch.setattr("server.capture.audio.list_devices",
                        lambda *a: pytest.fail("must not probe devices without ffmpeg"))
    data = _laptop(tmp_path)
    # enable; then decline the offer to install ffmpeg (only asked where it can
    # be done without sudo, so the second answer goes unused on Linux)
    src = _enable(data, answers, (True, False))
    assert data["audio"]["enabled"] is True
    assert admits_audio(src)


# ---------------------------------------------------------------------------
# server mode configures no recorder, but must still verify audio can arrive
# ---------------------------------------------------------------------------

def test_server_mode_does_not_start_a_local_recorder(answers, tmp_path):
    data = {"mode": "server", "ingest_dir": str(tmp_path),
            "sources": [{"type": "ingest_dir", "enabled": True, "name": "laptop",
                         "path": str(tmp_path / "laptop")}]}
    answers["yes"] = [True]
    wizard.step_audio(data)
    assert "audio" not in data                      # no recorder on the server
    assert data["transcription"]["enabled"] is True  # but it does the transcribing


# ---------------------------------------------------------------------------
# the activity step, which has the same shape and the same consent rules
# ---------------------------------------------------------------------------

@pytest.fixture
def activity_answers(answers, monkeypatch):
    class _Backend:
        name = "stub"
        titles_available = True

        def foreground(self):
            return "Chrome", "docs"

    monkeypatch.setattr("server.capture.activity.make_backend", lambda *a: _Backend())
    return answers


def test_activity_is_off_unless_asked_for(activity_answers, tmp_path):
    data = _laptop(tmp_path)
    wizard.step_activity(data)
    assert data["activity"]["enabled"] is False
    assert data.get("sources", []) == []


def test_enabling_activity_registers_a_drainer_that_spares_the_open_hour(
        activity_answers, tmp_path):
    data = _laptop(tmp_path)
    activity_answers["yes"] = [True]
    wizard.step_activity(data)
    src = next(s for s in data["sources"] if s.get("name") == "activity")
    assert _matches(src["exclude"], "PAUSED", "PAUSED")
    # the NDJSON rotates hourly and the current hour is still being appended to
    assert src["min_age_seconds"] >= 300
    assert Path(src["path"]).is_dir()
    assert data["activity"]["enabled"] is True


def test_an_unusable_display_does_not_silently_enable_activity(
        activity_answers, tmp_path, monkeypatch):
    # Wayland, or a headless box: better to say so than to log "?" all day
    monkeypatch.setattr("server.capture.activity.make_backend",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("Wayland")))
    data = _laptop(tmp_path)
    activity_answers["yes"] = [True, False]  # enable; decline "save anyway?"
    wizard.step_activity(data)
    assert data["activity"]["enabled"] is False
    assert data.get("sources", []) == []
