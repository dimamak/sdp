"""Small shared helpers."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    return log


def local_tz(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.get("pipeline.timezone", "UTC"))


def day_of(ts: datetime, cfg) -> str:
    """YYYY-MM-DD in the configured timezone."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(local_tz(cfg)).strftime("%Y-%m-%d")


def target_day(cfg, now: datetime | None = None) -> str:
    """The day a nightly run reports on: 'yesterday' when run before noon local."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(local_tz(cfg))
    if local.hour < 12:
        local = local - timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def window_start_iso(cfg, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    hours = int(cfg.get("pipeline.window_hours", 26))
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
