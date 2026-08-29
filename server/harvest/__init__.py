"""Source adapter registry.

Every harvester module registers a collect function:
    collect(src_cfg: dict, cfg: Config, store: Store, since: datetime) -> int
returning the number of NEW items registered. Config declares a list of source
instances; `type` selects the adapter here. Nothing instance-specific lives in code.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

REGISTRY: dict[str, Callable] = {}


def register(type_name: str):
    def deco(fn):
        REGISTRY[type_name] = fn
        return fn
    return deco


def unsafe_sources(cfg) -> list[str]:
    """Names of enabled sources that would defeat another source's ownership filter.

    An unfiltered `claude_projects_dir` pointed at the same directory as a filtered
    `claude_sessions` harvests EVERY user's transcripts on a shared host — silently
    undoing the filter. That's a privacy failure, so those sources are refused.
    """
    from pathlib import Path
    filtered_dirs = {
        Path(str(s["projects_dir"])).expanduser().resolve()
        for s in cfg.sources() if s.get("type") == "claude_sessions" and s.get("projects_dir")
    }
    bad = []
    for s in cfg.sources():
        if s.get("type") != "claude_projects_dir" or not s.get("projects_dir"):
            continue
        if Path(str(s["projects_dir"])).expanduser().resolve() in filtered_dirs:
            bad.append(s.get("name", s["type"]))
    return bad


def can_produce_audio(cfg) -> bool:
    """Is any enabled source capable of registering a kind='audio' item?

    `transcribe_pending()` selects WHERE kind='audio' and returns 0 without a word
    when nothing matches, so transcription can be enabled and silently transcribe
    nothing forever. Only `ingest_dir` ever writes that kind.
    """
    from .ingest_dir import admits_audio
    return any(admits_audio(s) for s in cfg.sources())


def collect_all(cfg, store, since: datetime) -> dict[str, int]:
    """Run every enabled source; returns {source name: new item count}."""
    from . import claude_localdir, claude_sessions, codex_sessions, ingest_dir, telegram, gmail  # noqa: F401  (register)
    from ..util import get_logger

    refused = set(unsafe_sources(cfg))
    results = {}
    for src in cfg.sources():
        type_ = src.get("type")
        name = src.get("name", type_)
        if name in refused:
            get_logger("harvest").error(
                "REFUSING source %s: it harvests %s unfiltered, which also holds other "
                "users' sessions covered by a filtered source. Disable it in config.",
                name, src.get("projects_dir"))
            results[name] = -1
            continue
        fn = REGISTRY.get(type_)
        if fn is None:
            # whatsapp etc. accumulate via webhook, not nightly pull
            continue
        try:
            results[name] = fn(src, cfg, store, since)
        except Exception as e:  # one broken source must not kill the night
            from ..util import get_logger
            get_logger("harvest").exception("source %s failed: %s", name, e)
            results[name] = -1
    return results
