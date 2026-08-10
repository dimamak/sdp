"""SQLite store: harvested items, drafts, sync cursors."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    day TEXT NOT NULL,
    ts TEXT,
    kind TEXT NOT NULL DEFAULT 'text',
    path TEXT,
    summary TEXT,
    meta_json TEXT,
    used_in_draft TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_day ON items(day);
CREATE INDEX IF NOT EXISTS idx_items_used ON items(used_in_draft);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    rationale TEXT,
    alternates_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|editing|approved|edited|skipped|posted|failed
    tg_message_id TEXT,
    posted_urn TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    cursor TEXT
);

-- one Claude Code session per day, so the bot can hold a real conversation
-- about that day's material (resumed with `claude -p --resume <id>`)
CREATE TABLE IF NOT EXISTS day_sessions (
    day TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Store:
    def __init__(self, store_dir: Path | str):
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.dir / "files"
        self.db_path = self.dir / "dailypost.db"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- items -------------------------------------------------------------
    def add_item(self, source: str, external_id: str, day: str, *, ts: str | None = None,
                 kind: str = "text", path: str | None = None, summary: str | None = None,
                 meta: dict | None = None) -> bool:
        """Insert an item; returns True if newly inserted (dedup on source+external_id)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO items(source, external_id, day, ts, kind, path, summary, meta_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (source, external_id, day, ts, kind, path, summary,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        self.db.commit()
        return cur.rowcount > 0

    def unused_items_since(self, since_iso: str) -> list[sqlite3.Row]:
        # ts OR created_at inside the window: items pushed late (laptop was off)
        # have an old ts but a fresh created_at and must still enter the next digest.
        # created_at is sqlite datetime('now') format (space-separated, UTC).
        since_sqlite = since_iso.replace("T", " ").split("+")[0]
        return self.db.execute(
            "SELECT * FROM items WHERE used_in_draft IS NULL AND (ts >= ? OR created_at >= ?)"
            " ORDER BY source, ts",
            (since_iso, since_sqlite),
        ).fetchall()

    def mark_used(self, item_ids: list[int], day: str) -> None:
        self.db.executemany("UPDATE items SET used_in_draft=? WHERE id=?", [(day, i) for i in item_ids])
        self.db.commit()

    def set_item_summary(self, item_id: int, summary: str) -> None:
        self.db.execute("UPDATE items SET summary=? WHERE id=?", (summary, item_id))
        self.db.commit()

    # ---- files -------------------------------------------------------------
    def day_files_dir(self, day: str, sub: str) -> Path:
        d = self.files_dir / day / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- sync cursors --------------------------------------------------------
    def get_cursor(self, source: str) -> str | None:
        row = self.db.execute("SELECT cursor FROM sync_state WHERE source=?", (source,)).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, source: str, cursor: str) -> None:
        self.db.execute(
            "INSERT INTO sync_state(source, cursor) VALUES(?,?)"
            " ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor",
            (source, cursor),
        )
        self.db.commit()

    # ---- drafts --------------------------------------------------------------
    def add_draft(self, day: str, text: str, rationale: str | None = None,
                  alternates: list | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO drafts(day, text, rationale, alternates_json) VALUES(?,?,?,?)",
            (day, text, rationale, json.dumps(alternates, ensure_ascii=False) if alternates else None),
        )
        self.db.commit()
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def update_draft(self, draft_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE drafts SET {cols}, updated_at=datetime('now') WHERE id=?",
            (*fields.values(), draft_id),
        )
        self.db.commit()

    def latest_editing_draft(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM drafts WHERE status='editing' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def draft_by_tg_message(self, tg_message_id: str | int) -> sqlite3.Row | None:
        """Resolve which draft a Telegram reply refers to."""
        return self.db.execute(
            "SELECT * FROM drafts WHERE tg_message_id=? ORDER BY id DESC LIMIT 1",
            (str(tg_message_id),),
        ).fetchone()

    def latest_draft_for_day(self, day: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM drafts WHERE day=? ORDER BY id DESC LIMIT 1", (day,)
        ).fetchone()

    # ---- day sessions (conversation continuity) -------------------------------
    def set_day_session(self, day: str, session_id: str) -> None:
        self.db.execute(
            "INSERT INTO day_sessions(day, session_id) VALUES(?,?)"
            " ON CONFLICT(day) DO UPDATE SET session_id=excluded.session_id",
            (day, session_id),
        )
        self.db.commit()

    def latest_day_session(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM day_sessions ORDER BY day DESC LIMIT 1"
        ).fetchone()
