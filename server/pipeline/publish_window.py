"""Publish-window arithmetic: when an approved draft is allowed to actually go
out to LinkedIn, as opposed to when it was approved.

Pure functions over an injected `now` — no I/O, no Store, no network. The bot
(server/bot/main.py) and its worker loop are the only callers; everything here
is safe to unit test without either.

`publish.window` empty or unset means "no gate" — publish the moment you
approve, the previous (and default) behaviour. This keeps the change opt-in
for anyone else running this project.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class PublishWindow:
    def __init__(self, start: time, end: time, tz: ZoneInfo, weekdays: set[int]):
        self.start = start
        self.end = end
        self.tz = tz
        self.weekdays = weekdays  # 0=Mon .. 6=Sun, per _DAY_NAMES/date.weekday()


def _parse_time(s: str) -> time:
    hh, _, mm = s.strip().partition(":")
    return time(int(hh), int(mm or 0))


def _parse_days(cfg) -> set[int]:
    raw = cfg.get("publish.days", None)
    if not raw:
        return set(range(7))  # unset = every day
    names = {str(d).strip()[:3].title() for d in raw}
    return {i for i, n in enumerate(_DAY_NAMES) if n in names}


def parse_window(cfg) -> PublishWindow | None:
    """None means unconfigured — callers must treat that as "publish now"."""
    raw = str(cfg.get("publish.window", "") or "").strip()
    if not raw:
        return None
    start_s, _, end_s = raw.partition("-")
    if not end_s:
        raise ValueError(f"publish.window={raw!r} is not 'HH:MM-HH:MM'")
    tz = ZoneInfo(str(cfg.get("publish.window_tz", "Etc/UTC")))
    return PublishWindow(_parse_time(start_s), _parse_time(end_s), tz, _parse_days(cfg))


def in_window(cfg, now: datetime) -> bool:
    """Is `now` (any tzinfo) inside the configured publish window?

    No window configured => always True, so callers that only branch on this
    reproduce today's "publish the instant you approve" behaviour unchanged.
    """
    win = parse_window(cfg)
    if win is None:
        return True
    local = now.astimezone(win.tz)
    if local.weekday() not in win.weekdays:
        return False
    return win.start <= local.time() < win.end


def next_slot(cfg, now: datetime, *, jitter_minutes: int = 10) -> datetime:
    """The UTC datetime of the next eligible slot at or after `now`.

    If `now` already falls inside an eligible window, returns `now` (queueing
    something for "right now" is just an immediate publish). Otherwise walks
    forward day by day to the next eligible weekday and anchors to
    window.start, offset by a small random jitter so a batch of approvals
    doesn't all land at the same second — a queue of one draft per slot still
    needs *some* spread once more than a handful get approved back to back.

    Returns a UTC-aware datetime regardless of window_tz, since that's what
    gets stored and compared against store.due_drafts().
    """
    win = parse_window(cfg)
    if win is None:
        return now
    if in_window(cfg, now):
        return now

    local_now = now.astimezone(win.tz)
    for offset in range(0, 8):  # at most one full week to find an eligible day
        candidate_date = local_now.date() + timedelta(days=offset)
        if candidate_date.weekday() not in win.weekdays:
            continue
        slot_start = datetime.combine(candidate_date, win.start, tzinfo=win.tz)
        if slot_start <= local_now:
            continue  # today's window already passed
        jitter = timedelta(minutes=random.uniform(0, max(jitter_minutes, 0)))  # noqa: S311
        return (slot_start + jitter).astimezone(now.tzinfo or ZoneInfo("UTC"))
    raise ValueError(f"publish.days={win.weekdays!r} leaves no eligible weekday")


def slot_taken(store, slot: datetime, cfg) -> bool:
    """Has `publish.max_per_slot` already been reached for the slot `slot` falls in?

    "The slot" is the calendar day in window_tz — max_per_slot caps posts per
    day, not per exact minute, since a single window is one slot per day by
    construction (parse_window doesn't sub-divide it). Counts drafts that have
    already published today too, not just ones still waiting — see
    Store.scheduled_count.
    """
    cap = int(cfg.get("publish.max_per_slot", 1) or 0)
    if cap <= 0:
        return False
    win = parse_window(cfg)
    tz = win.tz if win else ZoneInfo("Etc/UTC")
    day: date = slot.astimezone(tz).date()
    day_start = datetime.combine(day, time.min, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    day_end = day_start + timedelta(days=1)
    count = store.scheduled_count(day_start.isoformat(), day_end.isoformat())
    return count >= cap
