from datetime import datetime, timezone

from server.config import Config
from server.radar.hours import in_active_hours


def _cfg(overrides=None):
    return Config({"radar": overrides or {}, "pipeline": {"timezone": "UTC"}}, {}, None)


def test_within_global_window_is_active():
    cfg = _cfg({"active_hours": "08:00-23:00"})
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    assert in_active_hours(cfg, "api", now)


def test_outside_global_window_is_inactive():
    cfg = _cfg({"active_hours": "08:00-23:00"})
    now = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert not in_active_hours(cfg, "api", now)


def test_null_window_means_always_on():
    cfg = _cfg({"active_hours": None})
    now = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert in_active_hours(cfg, "api", now)


def test_per_lane_override_beats_global():
    cfg = _cfg({"active_hours": "08:00-23:00", "extension": {"active_hours": "00:00-23:59"}})
    now = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert in_active_hours(cfg, "extension", now)       # extension override is wide open
    assert not in_active_hours(cfg, "api", now)          # api still uses the global window


def test_lane_null_override_means_always_on_even_if_global_is_narrow():
    cfg = _cfg({"active_hours": "08:00-23:00", "api": {"active_hours": None}})
    now = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert in_active_hours(cfg, "api", now)


def test_window_spanning_midnight():
    cfg = _cfg({"active_hours": "22:00-02:00"})
    assert in_active_hours(cfg, "api", datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc))
    assert in_active_hours(cfg, "api", datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc))
    assert not in_active_hours(cfg, "api", datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
