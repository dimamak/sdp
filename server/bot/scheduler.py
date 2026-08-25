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

from ..config import Config
from ..pipeline.run_nightly import main as run_nightly_main
from ..store import Store
from ..util import get_logger, local_tz, target_day

log = get_logger("bot.scheduler")


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


def start(cfg: Config, poll_seconds: int = 600) -> None:
    """Start the scheduler thread. Call once, from the laptop-mode bot process."""

    def run() -> None:
        # A brief stagger avoids racing a nightly run already kicked off by
        # something else (e.g. a manual run) at bot startup.
        time.sleep(5)
        while True:
            try:
                store = Store(cfg.path_of("store_dir"))  # own Store: sqlite per-thread
                if _due(cfg, store):
                    log.info("scheduled time reached — running nightly pipeline")
                    run_nightly_main(["--config", str(cfg.path)])
            except Exception:
                log.exception("scheduled nightly run failed — will retry next poll")
            time.sleep(poll_seconds)

    threading.Thread(target=run, daemon=True, name="nightly-scheduler").start()
    log.info("laptop scheduler started (target %s local, polling every %ds)",
              cfg.get("pipeline.schedule_time", "23:30"), poll_seconds)
