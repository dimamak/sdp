"""Active-hours gating, shared by both lanes (plan.md §4).

Outside the window Lane B stops polling and Lane A stops scoring/alerting —
drafted-but-undelivered suggestions are dropped rather than queued, since a
stale suggestion is worse than none.
"""
from __future__ import annotations

from datetime import datetime, time

from ..util import local_tz


def _parse_window(raw: str) -> tuple[time, time]:
    start_s, _, end_s = raw.partition("-")

    def _t(s: str) -> time:
        hh, _, mm = s.strip().partition(":")
        return time(int(hh), int(mm or 0))
    return _t(start_s), _t(end_s)


def in_active_hours(cfg, lane: str, now: datetime | None = None) -> bool:
    """`lane` is "api" or "extension". A per-lane override (`radar.<lane>.active_hours`)
    beats the global `radar.active_hours`; either being explicitly null means
    "always on". A window spanning midnight (e.g. "22:00-02:00") is supported."""
    override = cfg.get(f"radar.{lane}.active_hours", "__unset__")
    raw = override if override != "__unset__" else cfg.get("radar.active_hours", "08:00-23:00")
    if not raw:
        return True
    now = now or datetime.now(local_tz(cfg))
    start, end = _parse_window(str(raw))
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end
