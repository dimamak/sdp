"""Drain a spool directory of dropped files into the store.

Files arrive either from `laptop/push_daily.sh` over SSH (server mode) or from a
local recorder writing straight into the folder (laptop mode). Layout is
preserved but ignored: the *kind* of every file is decided by its extension via
KIND_BY_EXT, and the sub-source name comes from that same table — not from the
first path segment. Ingested files are copied into the store's files/<day>/ tree
and removed from the spool.

Optional per-source keys, all defaulting to today's behaviour:
    include: ["*.speech.opus"]   # only these (default: everything)
    exclude: ["PAUSED", "*.log"] # never these (default: nothing)
    min_age_seconds: 90          # ignore files still being written

Files that any of those rules skip are left on disk untouched. That matters:
the recorders use an extensionless PAUSED flag file to stop the microphone, and
ingesting it would delete it and silently resume recording.
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from ..util import day_of, get_logger
from . import register

log = get_logger("harvest.ingest")

KIND_BY_EXT = {
    ".jsonl": ("claude", "claude_jsonl"),
    ".png": ("screenshots", "screenshot"),
    ".jpg": ("screenshots", "screenshot"),
    ".jpeg": ("screenshots", "screenshot"),
    ".webp": ("screenshots", "screenshot"),
    ".wav": ("audio", "audio"),
    ".m4a": ("audio", "audio"),
    ".mp3": ("audio", "audio"),
    ".ogg": ("audio", "audio"),
    ".opus": ("audio", "audio"),
    ".txt": ("notes", "note"),
    ".md": ("notes", "note"),
    ".ndjson": ("activity", "activity_log"),
}

AUDIO_EXTS = tuple(ext for ext, (_, kind) in KIND_BY_EXT.items() if kind == "audio")


def _matches(patterns, name: str, rel: str) -> bool:
    """A pattern may address the bare filename (`*.opus`) or the path relative to
    the spool root (`audio/*.opus`); both spellings are accepted."""
    return any(fnmatch(name, p) or fnmatch(rel, p) for p in patterns)


def admits_audio(src) -> bool:
    """Could this ingest_dir source ever produce a kind='audio' item?

    Pure predicate over one source dict — no disk access — so the doctor can ask
    "transcription is on, but can anything actually feed it?" (see setup/wizard.py).
    """
    if src.get("type") != "ingest_dir" or not src.get("enabled", True):
        return False
    include = [str(p) for p in (src.get("include") or [])]
    exclude = [str(p) for p in (src.get("exclude") or [])]
    probes = []
    for ext in AUDIO_EXTS:
        if not include:
            probes.append(f"sample{ext}")
            continue
        for p in include:
            # a filename the pattern would accept, e.g. "*.speech.opus" -> the
            # recorder's own naming; "audio/*" -> any audio file under audio/
            stem = p.replace("*", "sample").replace("?", "x")
            suffix = Path(p).suffix.lower()
            if suffix == ext:
                probes.append(stem)
            elif suffix in ("", ".*"):
                probes.append(stem + ext)
    return any(not (exclude and _matches(exclude, probe.rsplit("/", 1)[-1], probe))
               for probe in probes)


@register("ingest_dir")
def collect(src, cfg, store, since) -> int:
    root = Path(str(src["path"])).expanduser()
    name = src.get("name", "ingest")
    if not root.exists():
        log.warning("%s: ingest dir missing: %s", name, root)
        return 0
    include = [str(p) for p in (src.get("include") or [])]
    exclude = [str(p) for p in (src.get("exclude") or [])]
    min_age = float(src.get("min_age_seconds") or 0)
    now = time.time()

    count = 0
    drained_dirs: set[Path] = set()
    for f in sorted(root.glob("**/*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        rel = f.relative_to(root)
        rel_str = rel.as_posix()
        if exclude and _matches(exclude, f.name, rel_str):
            continue
        if include and not _matches(include, f.name, rel_str):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if min_age and now - st.st_mtime < min_age:
            continue

        sub, kind = KIND_BY_EXT.get(f.suffix.lower(), ("misc", "file"))
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        day = day_of(mtime, cfg)
        dest = store.day_files_dir(day, sub) / f"{name}--{'--'.join(rel.parts)}"
        if store.add_item(
            source=f"{name}:{sub}",
            external_id=f"{rel_str}:{st.st_size}",
            day=day,
            ts=mtime.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kind=kind,
            path=str(dest),
            meta={"original": rel_str},
        ):
            shutil.copy2(f, dest)
            count += 1
        try:
            f.unlink()  # the spool is drained once a file has been considered
            for p in f.parents:
                if p == root:
                    break
                drained_dirs.add(p)
        except OSError as e:
            # Either the file is locked by the process still writing it (Windows
            # holds an exclusive handle), or it was pushed by a different remote
            # user and we lack write access on the containing dir. The item is
            # already stored either way; raising min_age_seconds fixes the first.
            log.warning("%s: cannot remove %s after ingest: %s", name, f, e)
    # tidy up only the subdirectories this run emptied; never the spool root
    for d in sorted(drained_dirs, reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return count
