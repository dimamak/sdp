"""Covers the `note` kind in server/pipeline/digest.py.

`ingest_dir` stores dropped .txt/.md files with a path and no summary. The
digest's fallback branch renders `item["summary"]` and skips anything blank, so
before the dedicated branch existed every note ever dropped was stored and then
silently dropped from the digest — the documented "drop a note in the ingest
folder" path did nothing at all.
"""
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.pipeline.digest import build_digest
from server.store import Store


def _cfg(tmp_path):
    return Config({"store_dir": str(tmp_path / "store"), "sources": []},
                  {}, tmp_path / "config.yaml")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _store_with_note(tmp_path, text: str, name: str = "standup.md"):
    cfg = _cfg(tmp_path)
    store = Store(cfg.path_of("store_dir"))
    note = store.day_files_dir("2026-08-29", "notes") / name
    note.write_text(text, encoding="utf-8")
    store.add_item(source="drop:notes", external_id=name, day="2026-08-29",
                   ts=_now_iso(), kind="note", path=str(note))
    return cfg, store


def test_note_body_is_read_from_the_file(tmp_path):
    cfg, store = _store_with_note(tmp_path, "shipped the retry backoff")
    digest, ids = build_digest(cfg, store, "2026-08-29")
    assert "shipped the retry backoff" in digest
    assert "### Note (standup.md)" in digest
    assert ids


def test_note_body_is_capped(tmp_path):
    cfg, store = _store_with_note(tmp_path, "x" * 5000)
    cfg.data["pipeline"] = {"per_item_max_chars": 100}
    digest, _ = build_digest(cfg, store, "2026-08-29")
    assert "x" * 100 in digest
    assert "x" * 101 not in digest


def test_missing_note_file_is_skipped_not_crashed(tmp_path):
    cfg, store = _store_with_note(tmp_path, "gone")
    store.db.execute("UPDATE items SET path=?", (str(tmp_path / "nope.md"),))
    store.db.commit()
    digest, _ = build_digest(cfg, store, "2026-08-29")
    assert "### Note" not in digest


def test_note_outside_the_window_is_not_included(tmp_path):
    cfg, store = _store_with_note(tmp_path, "old news")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    store.db.execute("UPDATE items SET ts=?, created_at=?", (old, old.replace("T", " ")[:19]))
    store.db.commit()
    assert build_digest(cfg, store, "2026-08-29") == ("", [])
