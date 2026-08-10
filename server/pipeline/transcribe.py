"""Transcribe harvested audio locally, then discard the audio.

Always-on capture means most of the day is silence or background noise, so each
file is silence-stripped first and dropped entirely if little speech remains —
that is what keeps overnight CPU time reasonable.

Hebrew uses the ivrit.ai fine-tune with an explicit language token: that model's
own card notes language detection and the translate task were degraded during
fine-tuning, so we always transcribe (never translate) and let the drafting LLM
handle Hebrew→English.

Raw audio is deleted once a transcript is stored (configurable) — it contains
other people's voices and has no further use.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ..util import get_logger

log = get_logger("pipeline.transcribe")

_MODEL = None


def _get_model(cfg):
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        name = str(cfg.get("transcription.model", "ivrit-ai/whisper-large-v3-turbo-ct2"))
        log.info("loading %s", name)
        _MODEL = WhisperModel(
            name,
            device=str(cfg.get("transcription.device", "cpu")),
            compute_type=str(cfg.get("transcription.compute_type", "int8")),
            cpu_threads=int(cfg.get("transcription.cpu_threads", 4)),
        )
    return _MODEL


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _strip_silence(src: Path, dst: Path, threshold_db: int, min_silence: float) -> float:
    """Remove silent stretches; returns the remaining duration in seconds."""
    flt = (f"silenceremove=start_periods=1:start_duration={min_silence}:"
           f"start_threshold={threshold_db}dB:stop_periods=-1:"
           f"stop_duration={min_silence}:stop_threshold={threshold_db}dB")
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(src), "-af", flt, "-ac", "1", "-ar", "16000", str(dst)],
        capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("silence strip failed for %s: %s", src.name, r.stderr[:200])
        return -1.0
    return _duration(dst)


def transcribe_pending(cfg, store) -> int:
    """Transcribe every stored audio item that has no transcript yet."""
    if not cfg.get("transcription.enabled", False):
        return 0

    rows = store.db.execute(
        "SELECT * FROM items WHERE kind='audio' AND (summary IS NULL OR summary='')"
        " ORDER BY ts"
    ).fetchall()
    if not rows:
        return 0

    threshold = int(cfg.get("transcription.silence_threshold_db", -35))
    min_silence = float(cfg.get("transcription.min_silence_seconds", 1.5))
    min_speech = float(cfg.get("transcription.min_speech_seconds", 4))
    language = str(cfg.get("transcription.language", "he"))
    delete_after = bool(cfg.get("transcription.delete_audio_after", True))
    max_files = int(cfg.get("transcription.max_files_per_run", 200))

    done = 0
    dropped = 0
    model = None
    for row in rows[:max_files]:
        src = Path(row["path"] or "")
        if not src.exists():
            store.set_item_summary(row["id"], "[audio missing]")
            continue

        with tempfile.TemporaryDirectory() as td:
            stripped = Path(td) / "speech.wav"
            speech = _strip_silence(src, stripped, threshold, min_silence)
            if speech < 0:
                speech, stripped = _duration(src), src  # fall back to the raw file
            if speech < min_speech:
                # nothing but room noise — record that and move on
                store.set_item_summary(row["id"], "[no speech]")
                if delete_after:
                    src.unlink(missing_ok=True)
                dropped += 1
                continue

            if model is None:
                model = _get_model(cfg)
            segments, info = model.transcribe(
                str(stripped), language=language, task="transcribe",
                vad_filter=True, beam_size=int(cfg.get("transcription.beam_size", 1)),
            )
            text = " ".join(s.text.strip() for s in segments).strip()

        if not text:
            store.set_item_summary(row["id"], "[no speech]")
            dropped += 1
        else:
            meta = json.loads(row["meta_json"] or "{}")
            meta.update({"speech_seconds": round(speech, 1), "language": language,
                         "transcribed": True})
            store.db.execute(
                "UPDATE items SET summary=?, kind='transcript', meta_json=? WHERE id=?",
                (text, json.dumps(meta, ensure_ascii=False), row["id"]))
            store.db.commit()
            done += 1
            log.info("transcribed %s (%.0fs speech, %d chars)", src.name, speech, len(text))

        if delete_after:
            src.unlink(missing_ok=True)

    log.info("transcription: %d transcript(s), %d silent file(s) dropped", done, dropped)
    return done
