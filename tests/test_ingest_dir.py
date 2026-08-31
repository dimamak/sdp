"""Covers server/harvest/ingest_dir.py's spool semantics.

The load-bearing case is the PAUSED regression: the recorders stop the
microphone by writing an extensionless PAUSED file into their output folder, and
before `exclude` existed the nightly ingested that file and deleted it — silently
resuming a mic the person had deliberately paused. Anything a rule skips must
still be on disk when the run ends.
"""
import os
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.harvest import can_produce_audio
from server.harvest.ingest_dir import admits_audio, collect
from server.store import Store


def _cfg(tmp_path, sources=None):
    return Config({"store_dir": str(tmp_path / "store"),
                   "sources": sources or []}, {}, tmp_path / "config.yaml")


def _setup(tmp_path, files: dict[str, str]):
    spool = tmp_path / "spool"
    spool.mkdir()
    for rel, text in files.items():
        p = spool / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = _cfg(tmp_path)
    return spool, cfg, Store(cfg.path_of("store_dir"))


def _kinds(store):
    return {r["kind"] for r in store.db.execute("SELECT kind FROM items")}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_audio_extension_wins_regardless_of_folder(tmp_path):
    # classification is by extension, NOT by the first path segment — an .opus
    # under screenshots/ is still kind='audio'
    spool, cfg, store = _setup(tmp_path, {"screenshots/talk.opus": "x"})
    assert collect({"path": str(spool), "name": "drop"}, cfg, store, None) == 1
    assert _kinds(store) == {"audio"}


def test_files_are_drained_from_the_spool(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"note.txt": "hello"})
    collect({"path": str(spool), "name": "drop"}, cfg, store, None)
    assert not (spool / "note.txt").exists()


# ---------------------------------------------------------------------------
# include / exclude — a skipped file must survive
# ---------------------------------------------------------------------------

def test_excluded_pause_flag_is_neither_ingested_nor_deleted(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"PAUSED": "", "talk.speech.opus": "x"})
    src = {"path": str(spool), "name": "audio",
           "exclude": ["PAUSED", "*.log"], "include": ["*.speech.opus"]}
    assert collect(src, cfg, store, None) == 1
    assert (spool / "PAUSED").exists(), "pausing the mic must survive the nightly"
    assert _kinds(store) == {"audio"}


def test_include_skips_unfinished_segments_without_deleting_them(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"office-1.opus": "partial",
                                          "office-0.speech.opus": "vetted"})
    src = {"path": str(spool), "name": "audio", "include": ["*.speech.opus"]}
    assert collect(src, cfg, store, None) == 1
    assert (spool / "office-1.opus").exists()
    assert not (spool / "office-0.speech.opus").exists()


def test_pattern_may_address_the_relative_path(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"audio/a.opus": "x", "other/b.opus": "y"})
    src = {"path": str(spool), "name": "drop", "include": ["audio/*.opus"]}
    assert collect(src, cfg, store, None) == 1
    assert (spool / "other/b.opus").exists()


def test_no_rules_means_everything_as_before(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"a.opus": "x", "b.txt": "y", "PAUSED": ""})
    assert collect({"path": str(spool), "name": "drop"}, cfg, store, None) == 3
    assert not (spool / "PAUSED").exists()


# ---------------------------------------------------------------------------
# min_age_seconds — a file still being written is left alone, then picked up
# ---------------------------------------------------------------------------

def test_too_young_file_is_skipped_then_ingested_once_it_ages(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"fresh.opus": "x"})
    src = {"path": str(spool), "name": "audio", "min_age_seconds": 3600}
    assert collect(src, cfg, store, None) == 0
    assert (spool / "fresh.opus").exists()

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    os.utime(spool / "fresh.opus", (old, old))
    assert collect(src, cfg, store, None) == 1
    assert not (spool / "fresh.opus").exists()


def test_empty_subdirs_are_removed_but_the_spool_root_survives(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"deep/nested/a.opus": "x"})
    collect({"path": str(spool), "name": "drop"}, cfg, store, None)
    assert spool.exists()
    assert not (spool / "deep").exists()


def test_a_dir_holding_a_skipped_file_is_kept(tmp_path):
    spool, cfg, store = _setup(tmp_path, {"keep/PAUSED": "", "keep/a.opus": "x"})
    collect({"path": str(spool), "name": "drop", "exclude": ["PAUSED"]}, cfg, store, None)
    assert (spool / "keep" / "PAUSED").exists()


# ---------------------------------------------------------------------------
# can_produce_audio — the predicate behind the doctor's silent-no-op check
# ---------------------------------------------------------------------------

def test_admits_audio_true_for_a_plain_ingest_dir():
    assert admits_audio({"type": "ingest_dir", "path": "/x"}) is True


def test_admits_audio_false_when_include_rules_audio_out():
    assert admits_audio({"type": "ingest_dir", "path": "/x", "include": ["*.png"]}) is False


def test_admits_audio_false_when_disabled_or_wrong_type():
    assert admits_audio({"type": "ingest_dir", "path": "/x", "enabled": False}) is False
    assert admits_audio({"type": "telegram"}) is False


def test_admits_audio_true_for_the_recorder_source_shape():
    assert admits_audio({"type": "ingest_dir", "path": "/x",
                         "include": ["*.speech.opus"],
                         "exclude": ["PAUSED", "*.log"]}) is True


def test_can_produce_audio_over_a_config(tmp_path):
    def src(**kw):
        return [{"type": "ingest_dir", "enabled": True, "path": "/x", **kw}]

    assert can_produce_audio(_cfg(tmp_path, [])) is False
    assert can_produce_audio(_cfg(tmp_path, src(include=["*.png"]))) is False
    assert can_produce_audio(_cfg(tmp_path, src())) is True
    # disabled sources never reach the harvester, so they can't feed transcription
    assert can_produce_audio(_cfg(tmp_path, [dict(src()[0], enabled=False)])) is False
