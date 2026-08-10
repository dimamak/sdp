"""Normalize files pushed from the laptop (or phone) into the store.

The push script drops files under <path>/ preserving relative layout, e.g.:
    claude/<project>/<session>.jsonl
    screenshots/Screenshot_5.png
    audio/call-debrief-2026-08-09.m4a
Classification is by extension; the first path segment names the sub-source.
Processed files are moved into the store's files/<day>/ tree (ingest dir stays clean).
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import register
from ..util import day_of, get_logger

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
}


@register("ingest_dir")
def collect(src, cfg, store, since) -> int:
    root = Path(str(src["path"])).expanduser()
    name = src.get("name", "ingest")
    if not root.exists():
        log.warning("%s: ingest dir missing: %s", name, root)
        return 0
    count = 0
    for f in sorted(root.glob("**/*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        sub, kind = KIND_BY_EXT.get(f.suffix.lower(), ("misc", "file"))
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        day = day_of(mtime, cfg)
        rel = f.relative_to(root)
        dest = store.day_files_dir(day, sub) / f"{name}--{'--'.join(rel.parts)}"
        source = f"{name}:{sub}"
        external_id = f"{rel}:{f.stat().st_size}"
        if store.add_item(
            source=source,
            external_id=str(external_id),
            day=day,
            ts=mtime.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kind=kind,
            path=str(dest),
            meta={"original": str(rel)},
        ):
            shutil.copy2(f, dest)
            count += 1
        try:
            f.unlink()  # ingest dir is a spool: always drain
        except OSError as e:
            # e.g. pushed as a different remote user, so we lack write access on
            # the containing dir — the item is already stored, just warn
            log.warning("%s: cannot remove %s after ingest: %s", name, f, e)
    # remove now-empty subdirectories
    for d in sorted((p for p in root.glob("**/*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return count
