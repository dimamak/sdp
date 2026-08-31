"""Codex CLI / ChatGPT desktop session harvester.

The Codex desktop app merged into the ChatGPT desktop app on 2026-07-09, and
both it and the Codex CLI (and its IDE extension) write to the same local
transcript store — one adapter covers "I use Codex" and "I use ChatGPT
(desktop)" alike. Codex Cloud tasks and the plain ChatGPT web/desktop Chat tab
are NOT in this store and have no supported local read path (see the README's
limitations section).
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..util import day_of, get_logger
from . import register

log = get_logger("harvest.codex")

# rollout-<ts>-<uuid>.jsonl, or the fork variant rollout-<ts>-<thread>_<rollout>.jsonl.
# Either way, everything after the timestamp is treated as the id.
_THREAD_RE = re.compile(r"^rollout-[\d\-T:.]+Z?-(.+)$")


def codex_home(src: dict | None = None) -> Path:
    override = (src or {}).get("codex_home")
    if override:
        return Path(str(override)).expanduser()
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def _thread_id(jsonl: Path) -> str:
    m = _THREAD_RE.match(jsonl.stem)
    return m.group(1) if m else jsonl.stem


def _peek_cwd(jsonl: Path, max_lines: int = 50) -> str | None:
    """cwd's location in the record is version-dependent, and rollout files
    reach 700MB-2GB, so this only scans the first handful of lines —
    session_meta/turn_context show up early if they're present at all.
    Degrades to None rather than raising; the caller falls back to the
    filename or directory name."""
    try:
        with jsonl.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (not isinstance(rec, dict)
                        or rec.get("type") not in ("session_meta", "turn_context")):
                    continue
                payload = rec.get("payload") or rec
                if isinstance(payload, dict):
                    if payload.get("cwd"):
                        return payload["cwd"]
                    sel = payload.get("TurnEnvironmentSelections")
                    if isinstance(sel, dict) and sel.get("cwd"):
                        return sel["cwd"]
    except OSError:
        return None
    return None


def register_jsonl(jsonl: Path, source_name: str, cfg, store, cwd: str | None = None) -> bool:
    """Copy a rollout transcript into the store and register it. Dedup by
    filename stem + size, mirroring claude_common.register_jsonl."""
    mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
    day = day_of(mtime, cfg)
    size = jsonl.stat().st_size
    external_id = f"{jsonl.stem}:{size}"
    dest_dir = store.day_files_dir(day, "codex")
    dest = dest_dir / f"{source_name}--{jsonl.name}"
    inserted = store.add_item(
        source=source_name,
        external_id=external_id,
        day=day,
        ts=mtime.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        kind="codex_jsonl",
        path=str(dest),
        meta={"thread_id": _thread_id(jsonl), "project": cwd or jsonl.parent.name, "size": size},
    )
    if inserted:
        shutil.copy2(jsonl, dest)
    return inserted


@register("codex_sessions")
def collect(src, cfg, store, since) -> int:
    home = codex_home(src)
    name = src.get("name", "codex")
    since_ts = since.timestamp()
    # The pipeline's own drafting calls, when pipeline.backend=codex, write rollout
    # files here too — never harvest those, or each night's digest would contain
    # the previous night's drafting conversation (same feedback-loop risk
    # own_project_dirname() guards against for Claude, just cwd-keyed instead of
    # dirname-keyed since Codex's sessions/ tree is dated, not project-named).
    own_cwd = Path(os.getcwd()).resolve()
    count = 0
    found_sessions_dir = False
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.exists():
            continue
        found_sessions_dir = found_sessions_dir or sub == "sessions"
        for jsonl in root.glob("**/rollout-*.jsonl"):
            if jsonl.stat().st_mtime < since_ts:
                continue
            cwd = _peek_cwd(jsonl)
            if cwd:
                try:
                    if Path(cwd).expanduser().resolve() == own_cwd:
                        continue
                except OSError:
                    pass
            if register_jsonl(jsonl, name, cfg, store, cwd=cwd):
                count += 1
    if not found_sessions_dir:
        log.warning("%s: codex sessions dir missing: %s", name, home / "sessions")
    return count
