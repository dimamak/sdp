"""Shared logic for Claude Code JSONL harvesting."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..util import day_of, get_logger

log = get_logger("harvest.claude")


def register_jsonl(jsonl: Path, source_name: str, cfg, store) -> bool:
    """Copy a session transcript into the store and register it. Dedup by session id + size."""
    mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
    day = day_of(mtime, cfg)
    size = jsonl.stat().st_size
    # external_id includes size so a session that grew since last harvest is re-captured
    external_id = f"{jsonl.stem}:{size}"
    dest_dir = store.day_files_dir(day, "claude")
    dest = dest_dir / f"{source_name}--{jsonl.parent.name}--{jsonl.name}"
    inserted = store.add_item(
        source=source_name,
        external_id=external_id,
        day=day,
        ts=mtime.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        kind="claude_jsonl",
        path=str(dest),
        meta={"project": jsonl.parent.name, "session_id": jsonl.stem, "size": size},
    )
    if inserted:
        shutil.copy2(jsonl, dest)
    return inserted


def harvest_dir(projects_dir: Path, since: datetime, source_name: str, cfg, store,
                session_ids: set[str] | None = None) -> int:
    """Register all JSONL files under projects_dir modified since `since`.
    If session_ids is given, only those sessions are taken (glob by file stem —
    no assumptions about Claude's project-path encoding)."""
    if not projects_dir.exists():
        log.warning("%s: projects dir missing: %s", source_name, projects_dir)
        return 0
    count = 0
    since_ts = since.timestamp()
    for jsonl in projects_dir.glob("**/*.jsonl"):
        if jsonl.stat().st_mtime < since_ts:
            continue
        if session_ids is not None and jsonl.stem not in session_ids:
            continue
        if register_jsonl(jsonl, source_name, cfg, store):
            count += 1
    return count
