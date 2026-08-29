"""In-process nightly scheduler for laptop mode.

Server installs use cron, which only fires at its scheduled minute — fine on
a machine that's always on. A laptop is routinely asleep at 23:30, and cron
has no notion of "catch up when the machine wakes"; a missed slot is just
gone. The bot process is already running continuously for Telegram polling,
so laptop mode adds a scheduler thread to it instead of a second OS-level
job: it wakes periodically and drafts the moment the scheduled time has
passed AND the day isn't drafted yet, whether that's exactly on time or
hours late because the lid was closed. `Store.has_drafts_for_day` (already
used by run_nightly's own idempotency check) makes repeated wake-ups safe.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..pipeline.run_nightly import main as run_nightly_main
from ..store import Store
from ..util import get_logger, local_tz, target_day

log = get_logger("bot.scheduler")

DEFAULT_POLL_SECONDS = 600
HEARTBEAT_FILE = "bot-heartbeat"


def _parse_schedule(cfg: Config) -> tuple[int, int]:
    raw = str(cfg.get("pipeline.schedule_time", "23:30"))
    hh, _, mm = raw.partition(":")
    try:
        return int(hh), int(mm or 0)
    except ValueError:
        log.warning("pipeline.schedule_time=%r is not HH:MM — defaulting to 23:30", raw)
        return 23, 30


def _due(cfg: Config, store: Store) -> bool:
    hour, minute = _parse_schedule(cfg)
    now = datetime.now(local_tz(cfg))
    if (now.hour, now.minute) < (hour, minute):
        return False
    return not store.has_drafts_for_day(target_day(cfg))


def heartbeat_path(cfg: Config) -> Path:
    return cfg.path_of("store_dir") / HEARTBEAT_FILE


def heartbeat(cfg: Config) -> tuple[float, int] | None:
    """(seconds since the bot last polled, the interval it polls at), or None if
    no bot has ever run against this store.

    In laptop mode nothing drafts unless this process is up, and "I closed the
    terminal three weeks ago" looks exactly like a working install from the
    outside — so the poll leaves a mark the doctor can read.
    """
    p = heartbeat_path(cfg)
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return None
    try:
        # the interval is recorded rather than assumed, so a bot started with a
        # non-default poll doesn't read as dead
        interval = int(p.read_text(encoding="utf-8").split()[1])
    except (OSError, IndexError, ValueError):
        interval = DEFAULT_POLL_SECONDS
    return age, interval


def start(cfg: Config, poll_seconds: int = DEFAULT_POLL_SECONDS) -> None:
    """Start the scheduler thread. Call once, from the laptop-mode bot process."""

    def beat() -> None:
        try:
            heartbeat_path(cfg).write_text(f"{int(time.time())} {poll_seconds}\n",
                                           encoding="utf-8")
        except OSError as e:
            log.warning("cannot write heartbeat: %s", e)

    def run() -> None:
        # A brief stagger avoids racing a nightly run already kicked off by
        # something else (e.g. a manual run) at bot startup.
        beat()
        time.sleep(5)
        while True:
            beat()
            try:
                store = Store(cfg.path_of("store_dir"))  # own Store: sqlite per-thread
                if _due(cfg, store):
                    log.info("scheduled time reached — running nightly pipeline")
                    run_nightly_main(["--config", str(cfg.path)])
            except Exception:
                log.exception("scheduled nightly run failed — will retry next poll")
            beat()  # again: a nightly run can outlast the poll interval by itself
            time.sleep(poll_seconds)

    threading.Thread(target=run, daemon=True, name="nightly-scheduler").start()
    log.info("laptop scheduler started (target %s local, polling every %ds)",
              cfg.get("pipeline.schedule_time", "23:30"), poll_seconds)
