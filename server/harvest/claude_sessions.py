"""Claude Code sessions on a shared host, filtered to the owner's sessions.

The harvester needs one thing: the set of session IDs that belong to the user.
How that set is produced is a pluggable `filter` strategy from config:

  all      — every session (equivalent to claude_projects_dir)
  sql      — a user-supplied SQL query against a SQLite DB returning session-id rows;
             `$SINCE` in params is replaced with the harvest window start (ISO).
  command  — a shell command printing one session id per line
  id_file  — a text file with one session id per line

Nothing platform-specific is hardcoded: the query/command IS the adapter config.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from . import register
from .claude_common import harvest_dir
from ..util import get_logger

log = get_logger("harvest.claude_sessions")


def resolve_session_ids(fcfg: dict, since_iso: str) -> set[str] | None:
    strategy = (fcfg or {}).get("strategy", "all")
    if strategy == "all":
        return None  # no filtering

    if strategy == "sql":
        db_path = Path(str(fcfg["db_path"])).expanduser()
        params = dict(fcfg.get("params") or {})
        for k, v in params.items():
            if isinstance(v, str) and "$SINCE" in v:
                params[k] = v.replace("$SINCE", since_iso)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(fcfg["query"], params).fetchall()
        finally:
            con.close()
        return {str(r[0]) for r in rows if r[0]}

    if strategy == "command":
        out = subprocess.run(
            fcfg["command"].replace("$SINCE", since_iso),
            shell=True, capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            raise RuntimeError(f"filter command failed: {out.stderr[:500]}")
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}

    if strategy == "id_file":
        p = Path(str(fcfg["path"])).expanduser()
        return {line.strip() for line in p.read_text().splitlines() if line.strip()}

    raise ValueError(f"unknown filter strategy: {strategy}")


@register("claude_sessions")
def collect(src, cfg, store, since) -> int:
    projects_dir = Path(str(src["projects_dir"])).expanduser()
    name = src.get("name", "claude-filtered")
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
    ids = resolve_session_ids(src.get("filter"), since_iso)
    if ids is not None:
        log.info("%s: filter matched %d sessions", name, len(ids))
        if not ids:
            return 0
    return harvest_dir(projects_dir, since, name, cfg, store, session_ids=ids,
                       exclude_projects=src.get("exclude_projects"))
