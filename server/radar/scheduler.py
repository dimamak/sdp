"""Radar scheduler thread — Lane B polling on its own cadence (plan.md §9).

Deliberately a separate thread from server/bot/scheduler.py, whose `_due()`
contract is "once per day"; this polls every `radar.poll_seconds` (default
300s) instead, and is a no-op whenever `radar.enabled` is false.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..store import Store
from ..util import get_logger
from . import pipeline, poll, spend
from .hours import in_active_hours

log = get_logger("radar.scheduler")

DEFAULT_POLL_SECONDS = 300
HEARTBEAT_FILE = "radar-heartbeat"


def heartbeat_path(cfg) -> Path:
    return cfg.path_of("store_dir") / HEARTBEAT_FILE


def last_poll_age_seconds(cfg) -> float | None:
    """Seconds since the radar scheduler last ticked, or None if it never has —
    read by --doctor (plan.md §10)."""
    try:
        return time.time() - heartbeat_path(cfg).stat().st_mtime
    except OSError:
        return None


def run_once(cfg, store, notify=None, now: datetime | None = None) -> list[dict]:
    """One scheduler tick. `notify(post, result)` is called for anything worth
    surfacing (a draft or a question); returns the raw results, mainly for
    tests. Lane B posts already carry their own sighting fields (poll.py
    upserts before returning them)."""
    now = now or datetime.now(timezone.utc)
    if not in_active_hours(cfg, "api", now):
        return []
    results = []
    for post in poll.poll_once(cfg, store, now):
        result = pipeline.process(post, cfg, store, now=now)
        results.append(result)
        if notify and result["decision"] in ("draft", "ask"):
            notify(post, result)
    if notify:
        spend.maybe_warn(cfg, store, lambda msg: notify(None, {"decision": "budget_warning",
                                                                "message": msg}), now)
    return results


def start(cfg, notify=None, poll_seconds: int | None = None) -> None:
    """Start the Lane B scheduler thread. Call once, from the bot process,
    only when radar.enabled."""
    if not cfg.get("radar.enabled", False):
        return
    poll_seconds = poll_seconds or int(cfg.get("radar.poll_seconds", DEFAULT_POLL_SECONDS))

    def beat() -> None:
        try:
            heartbeat_path(cfg).write_text(f"{int(time.time())} {poll_seconds}\n",
                                           encoding="utf-8")
        except OSError as e:
            log.warning("cannot write radar heartbeat: %s", e)

    def run() -> None:
        beat()
        while True:
            try:
                store = Store(cfg.path_of("store_dir"))  # own Store: sqlite per-thread
                run_once(cfg, store, notify=notify)
            except Exception:
                log.exception("radar poll failed — will retry next cycle")
            beat()
            time.sleep(poll_seconds)

    threading.Thread(target=run, daemon=True, name="radar-scheduler").start()
    log.info("radar scheduler started (polling every %ds)", poll_seconds)
