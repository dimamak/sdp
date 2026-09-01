from datetime import datetime, time, timezone

from server.config import Config
from server.pipeline.publish_window import in_window, next_slot, parse_window, slot_taken
from server.store import Store


def _cfg(**publish):
    return Config({"publish": publish} if publish else {}, {}, None)


def test_no_window_configured_means_always_in_window_and_immediate():
    cfg = _cfg()
    now = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)  # Sunday, an arbitrary hour
    assert parse_window(cfg) is None
    assert in_window(cfg, now) is True
    assert next_slot(cfg, now) == now


def test_approval_inside_the_window_publishes_immediately():
    cfg = _cfg(window="15:00-18:00", days=["Tue", "Wed", "Thu"])
    now = datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)  # Tuesday
    assert in_window(cfg, now) is True
    assert next_slot(cfg, now) == now


def test_approval_outside_window_queues_for_next_eligible_weekday():
    cfg = _cfg(window="15:00-18:00", days=["Tue", "Wed", "Thu"])
    now = datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc)  # Sunday
    assert in_window(cfg, now) is False
    slot = next_slot(cfg, now, jitter_minutes=0)
    assert slot.astimezone(timezone.utc).date() == datetime(2026, 9, 8).date()  # next Tuesday
    assert slot.astimezone(timezone.utc).time() == time(15, 0)


def test_window_is_read_in_window_tz_not_pipeline_timezone():
    cfg = Config({
        "publish": {"window": "09:00-10:00", "window_tz": "America/New_York",
                    "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
        "pipeline": {"timezone": "Asia/Tokyo"},
    }, {}, None)
    # 13:30 UTC is 09:30 in New York (EDT) in September, but 22:30 in Tokyo —
    # if the window were read in pipeline.timezone this would be out of window.
    now = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    assert in_window(cfg, now) is True


def test_slot_taken_respects_max_per_slot(tmp_path):
    cfg = _cfg(window="15:00-18:00", days=["Tue"], max_per_slot=1)
    store = Store(tmp_path / "store")
    slot = datetime(2026, 9, 8, 15, 5, tzinfo=timezone.utc)
    assert slot_taken(store, slot, cfg) is False

    draft_id = store.add_draft("2026-09-07", "post text")
    store.queue_draft(draft_id, slot.isoformat())
    assert slot_taken(store, slot, cfg) is True


def test_slot_taken_disabled_when_max_per_slot_is_zero(tmp_path):
    cfg = _cfg(window="15:00-18:00", days=["Tue"], max_per_slot=0)
    store = Store(tmp_path / "store")
    slot = datetime(2026, 9, 8, 15, 5, tzinfo=timezone.utc)
    draft_id = store.add_draft("2026-09-07", "post text")
    store.queue_draft(draft_id, slot.isoformat())
    assert slot_taken(store, slot, cfg) is False
