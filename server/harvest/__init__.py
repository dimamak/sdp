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


def collect_all(cfg, store, since: datetime) -> dict[str, int]:
    """Run every enabled source; returns {source name: new item count}."""
    from . import claude_localdir, claude_sessions, ingest_dir, telegram, gmail  # noqa: F401  (register)

    results = {}
    for src in cfg.sources():
        type_ = src.get("type")
        name = src.get("name", type_)
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
