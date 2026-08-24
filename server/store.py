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
    -- pending|editing|imaging|skipped|posted|failed
    -- ('approved'/'edited' are legacy values nothing writes any more)
    -- imaging: you tapped Approve, an image is rendering or waiting for your
    --          confirmation — nothing has been sent to LinkedIn yet
    status TEXT NOT NULL DEFAULT 'pending',
    tg_message_id TEXT,
    posted_urn TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- One row per image render attempt; the newest row for a draft is the live
-- candidate. Earlier takes are kept: the prompt plus the feedback that produced
-- each one is what you want to look at when the images start feeling samey.
CREATE TABLE IF NOT EXISTS draft_images (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,           -- take number within the draft, 1-based
    prompt TEXT,                            -- what the model was asked for, verbatim
    alt_text TEXT,                          -- LinkedIn content.media.altText
    feedback TEXT,                          -- your steer that produced this take
    path TEXT,                              -- local file; NULL once pruned
    mime TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'ready',   -- ready|pending_review|attached|discarded|failed|pruned
    li_image_urn TEXT,
    tg_message_id TEXT,                     -- the confirm message (carries the buttons)
    tg_photo_message_id TEXT,               -- the photo message itself
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_images_draft ON draft_images(draft_id);

-- One row per X (Twitter) rewrite attempt of an already-posted LinkedIn draft;
-- same one-row-per-take shape as draft_images. Never written to until the
-- LinkedIn post for the same draft has actually gone out.
CREATE TABLE IF NOT EXISTS draft_x (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,           -- take number within the draft, 1-based
    text TEXT,                              -- the X-native rewrite
    feedback TEXT,                          -- your steer that produced this take
    source TEXT NOT NULL DEFAULT 'claude',  -- claude|manual (manual = verbatim "Replace text")
    -- ready|pending_review|editing|posted|discarded|failed
    status TEXT NOT NULL DEFAULT 'ready',
    tweet_id TEXT,
    tg_message_id TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_x_draft ON draft_x(draft_id);

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


def _ensure_columns(db, table: str, cols: dict[str, str]) -> None:
    """Additive, idempotent migration for tables that already exist in the wild.

    CREATE TABLE IF NOT EXISTS covers new tables but never new columns, and there
    is no migration mechanism here. SQLite's ADD COLUMN only accepts constant
    defaults and no UNIQUE, so keep decls to 'TEXT' / 'INTEGER DEFAULT 0'.
    """
    have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


class Store:
    def __init__(self, store_dir: Path | str):
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.dir / "files"
        # deliberately a sibling of files/, not a child: prune_old_files() rmtree's
        # every dated directory under files/, and a posted image is the only local
        # copy of published creative — LinkedIn won't give it back.
        self.images_dir = self.dir / "images"
        self.db_path = self.dir / "dailypost.db"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        _ensure_columns(self.db, "drafts", {
            "reddit_status": "TEXT",          # NULL|pending|editing|posted|skipped
            "reddit_title": "TEXT",
            "reddit_permalink": "TEXT",       # optional, if you paste the URL back
            "reddit_tg_message_id": "TEXT",
        })
        _ensure_columns(self.db, "draft_x", {})
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

    def image_path_for(self, day: str, draft_id: int, n: int, ext: str = "jpg") -> Path:
        """Where take `n` of draft `draft_id` is written. Pure paths — safe anywhere."""
        d = self.images_dir / day
        d.mkdir(parents=True, exist_ok=True)
        return d / f"d{draft_id}-{n}.{ext.lstrip('.')}"

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

    def recent_hooks(self, before_day: str, limit: int = 10) -> list[str]:
        """First line of one draft per day, most recent days first.

        Prefers the day's posted draft, and keeps one line per day so a
        prolific night can't fill the whole list with its own siblings.
        """
        rows = self.db.execute(
            "SELECT day, text FROM drafts WHERE day < ? AND text <> ''"
            " ORDER BY day DESC, (status='posted') DESC, id ASC",
            (before_day,),
        ).fetchall()
        hooks, seen = [], set()
        for r in rows:
            if r["day"] in seen:
                continue
            lines = r["text"].strip().splitlines()
            if not lines:
                continue
            seen.add(r["day"])
            hooks.append(lines[0].strip())
            if len(hooks) >= limit:
                break
        return hooks

    def latest_editing_draft(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM drafts WHERE status='editing' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def draft_by_tg_message(self, tg_message_id: str | int) -> sqlite3.Row | None:
        """Resolve which draft a Telegram reply refers to.

        Also matches the photo/confirm messages of the image flow, the X
        candidate messages, and the Reddit delivery message, so replying to any
        of those, or the original draft, works.
        """
        mid = str(tg_message_id)
        return self.db.execute(
            "SELECT d.* FROM drafts d WHERE d.tg_message_id=? OR d.reddit_tg_message_id=?"
            " UNION ALL"
            " SELECT d.* FROM drafts d JOIN draft_images i ON i.draft_id=d.id"
            "  WHERE i.tg_message_id=? OR i.tg_photo_message_id=?"
            " UNION ALL"
            " SELECT d.* FROM drafts d JOIN draft_x x ON x.draft_id=d.id"
            "  WHERE x.tg_message_id=?"
            " LIMIT 1",
            (mid, mid, mid, mid, mid),
        ).fetchone()

    def has_drafts_for_day(self, day: str) -> bool:
        """Used to keep repeated runs idempotent: once a day has been drafted,
        later attempts do nothing rather than delivering a second set."""
        return self.db.execute(
            "SELECT 1 FROM drafts WHERE day=? LIMIT 1", (day,)
        ).fetchone() is not None

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

    def session_for_day(self, day: str) -> str | None:
        row = self.db.execute(
            "SELECT session_id FROM day_sessions WHERE day=?", (day,)).fetchone()
        return row["session_id"] if row else None

    # ---- draft images ---------------------------------------------------------
    def add_image(self, draft_id: int, n: int, *, prompt: str | None = None,
                  alt_text: str | None = None, feedback: str | None = None,
                  path: str | None = None, mime: str | None = None,
                  model: str | None = None, status: str = "ready",
                  error: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO draft_images(draft_id, n, prompt, alt_text, feedback, path,"
            " mime, model, status, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (draft_id, n, prompt, alt_text, feedback, path, mime, model, status, error),
        )
        self.db.commit()
        return cur.lastrowid

    def update_image(self, image_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE draft_images SET {cols} WHERE id=?",
                        (*fields.values(), image_id))
        self.db.commit()

    def latest_image(self, draft_id: int, status: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM draft_images WHERE draft_id=?"
        args: tuple = (draft_id,)
        if status:
            sql += " AND status=?"
            args += (status,)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def images_for_draft(self, draft_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM draft_images WHERE draft_id=? ORDER BY n", (draft_id,)).fetchall()

    def pending_image(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """The image currently awaiting your yes/no, if it is still recent.

        The age bound matters: without it a forgotten candidate would swallow every
        later free-text message forever (the bug latest_editing_draft() still has).
        """
        return self.db.execute(
            "SELECT * FROM draft_images WHERE status='pending_review'"
            " AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def stale_unposted_images(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Old takes that were never published — safe to delete the bytes of."""
        return self.db.execute(
            "SELECT * FROM draft_images WHERE path IS NOT NULL"
            " AND status != 'attached' AND created_at < ?",
            (cutoff_iso.replace("T", " ").split("+")[0],),
        ).fetchall()

    # ---- draft X (Twitter) posts -----------------------------------------------
    def add_x(self, draft_id: int, n: int, *, text: str | None = None,
             feedback: str | None = None, source: str = "claude",
             status: str = "ready", error: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO draft_x(draft_id, n, text, feedback, source, status, error)"
            " VALUES(?,?,?,?,?,?,?)",
            (draft_id, n, text, feedback, source, status, error),
        )
        self.db.commit()
        return cur.lastrowid

    def update_x(self, x_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE draft_x SET {cols} WHERE id=?", (*fields.values(), x_id))
        self.db.commit()

    def latest_x(self, draft_id: int, status: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM draft_x WHERE draft_id=?"
        args: tuple = (draft_id,)
        if status:
            sql += " AND status=?"
            args += (status,)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def x_for_draft(self, draft_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM draft_x WHERE draft_id=? ORDER BY n", (draft_id,)).fetchall()

    def pending_x(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """The X candidate currently awaiting your yes/no, if it is still recent.

        Age-bounded from the start — see pending_image()'s comment on why an
        unbounded version would swallow every later free-text message forever.
        """
        return self.db.execute(
            "SELECT * FROM draft_x WHERE status='pending_review'"
            " AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def editing_x(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """The X candidate whose text you're about to replace verbatim, if recent."""
        return self.db.execute(
            "SELECT * FROM draft_x WHERE status='editing'"
            " AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def draft_awaiting_x(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """A LinkedIn draft that was posted but never got an X candidate started —
        e.g. the bot restarted between publish() and start_x(). Used by /x."""
        return self.db.execute(
            "SELECT * FROM drafts WHERE status='posted'"
            " AND updated_at >= datetime('now', ?)"
            " AND id NOT IN (SELECT draft_id FROM draft_x)"
            " ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    # ---- Reddit (draft assist — reuses the LinkedIn text, see draft.reddit_body) ----
    def set_reddit(self, draft_id: int, **fields) -> None:
        """Thin wrapper over update_draft; fields are the reddit_* column names,
        e.g. set_reddit(id, reddit_status='posted', reddit_permalink=url)."""
        self.update_draft(draft_id, **fields)

    def pending_reddit(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """The draft currently awaiting your Reddit Submit tap, if still recent.

        Age-bounded from the start — see pending_image()'s comment on why an
        unbounded version would swallow every later free-text message forever.
        """
        return self.db.execute(
            "SELECT * FROM drafts WHERE reddit_status='pending'"
            " AND updated_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def editing_reddit(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """The draft whose Reddit title you're about to replace verbatim, if recent."""
        return self.db.execute(
            "SELECT * FROM drafts WHERE reddit_status='editing'"
            " AND updated_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def draft_awaiting_reddit(self, max_age_hours: int = 12) -> sqlite3.Row | None:
        """A LinkedIn draft that was posted but never got a Reddit step started —
        e.g. the bot restarted between the X step and this one. Used by /reddit."""
        return self.db.execute(
            "SELECT * FROM drafts WHERE status='posted' AND reddit_status IS NULL"
            " AND updated_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(max_age_hours)} hours",),
        ).fetchone()

    def last_posted_reddit(self) -> sqlite3.Row | None:
        """Most recent draft actually marked posted to Reddit — the cadence nudge."""
        return self.db.execute(
            "SELECT * FROM drafts WHERE reddit_status='posted'"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
