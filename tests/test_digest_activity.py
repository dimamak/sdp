"""The activity recorder rotates its NDJSON hourly, so one day of screen
activity arrives as a dozen separate items. Rendered one section each, the
digest would carry twelve "What was on screen" headings with the same-app
collapsing restarting inside every one — the timeline has to be merged first.
"""
import json
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.pipeline.digest import _activity_timeline, build_digest
from server.store import Store


def _cfg(tmp_path):
    return Config({"store_dir": str(tmp_path / "store"), "sources": []},
                  {}, tmp_path / "config.yaml")


def _hour_log(store, day: str, hour: int, samples: list[tuple[int, str, str]]):
    """One hourly NDJSON, as the recorder writes it: (minute, app, title) rows."""
    path = store.day_files_dir(day, "activity") / f"activity-{day.replace('-', '')}-{hour:02d}.ndjson"
    now = datetime.now(timezone.utc)
    with path.open("w", encoding="utf-8") as fh:
        for minute, app, title in samples:
            ts = (now - timedelta(hours=2)).replace(hour=hour, minute=minute, second=0)
            fh.write(json.dumps({"ts": ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                                 "app": app, "title": title}) + "\n")
    return path


# ---------------------------------------------------------------------------
# the merge itself
# ---------------------------------------------------------------------------

def test_rows_from_several_files_collapse_as_one_run():
    # the same app spanning an hour boundary is one line, not two
    rows = [("2026-08-29T09:58:00+00:00", "Chrome", "pricing page"),
            ("2026-08-29T10:02:00+00:00", "Chrome", "pricing page"),
            ("2026-08-29T10:06:00+00:00", "Slack", "#eng")]
    out = _activity_timeline(rows, 10000).splitlines()
    assert out[0] == "09:58 [Chrome] pricing page"
    assert out[1] == "10:02 pricing page"       # collapsed: no app prefix
    assert out[2] == "10:06 [Slack] #eng"


def test_rows_are_sorted_before_collapsing():
    # items come back from the store in whatever order; an unsorted merge would
    # collapse against the wrong neighbour
    rows = [("2026-08-29T11:00:00+00:00", "Slack", "#eng"),
            ("2026-08-29T09:00:00+00:00", "Chrome", "docs"),
            ("2026-08-29T10:00:00+00:00", "Chrome", "docs")]
    out = _activity_timeline(rows, 10000).splitlines()
    assert [l[:5] for l in out] == ["09:00", "10:00", "11:00"]
    assert out[1] == "10:00 docs"               # collapsed against 09:00, not 11:00


def test_no_rows_render_nothing():
    assert _activity_timeline([], 10000) == ""


# ---------------------------------------------------------------------------
# end to end through the digest
# ---------------------------------------------------------------------------

def test_a_days_hourly_logs_become_one_digest_section(tmp_path):
    day = "2026-08-29"
    cfg = _cfg(tmp_path)
    store = Store(cfg.path_of("store_dir"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    for hour in (9, 10, 11):
        p = _hour_log(store, day, hour, [(0, "Chrome", "docs"), (30, "Slack", "#eng")])
        store.add_item(source="activity", external_id=p.name, day=day, ts=now,
                       kind="activity_log", path=str(p))

    digest, ids = build_digest(cfg, store, day)
    assert digest.count("### What was on screen") == 1
    assert len(ids) == 3, "every hourly log is still marked used"
    for hour in ("09:", "10:", "11:"):
        assert hour in digest


def test_an_unreadable_log_does_not_lose_the_others(tmp_path):
    day = "2026-08-29"
    cfg = _cfg(tmp_path)
    store = Store(cfg.path_of("store_dir"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    good = _hour_log(store, day, 9, [(0, "Chrome", "docs")])
    broken = store.day_files_dir(day, "activity") / "activity-broken.ndjson"
    broken.write_text("{not json\n", encoding="utf-8")
    for p in (good, broken):
        store.add_item(source="activity", external_id=p.name, day=day, ts=now,
                       kind="activity_log", path=str(p))

    digest, _ = build_digest(cfg, store, day)
    assert "[Chrome] docs" in digest


def test_no_activity_section_when_every_log_is_empty(tmp_path):
    day = "2026-08-29"
    cfg = _cfg(tmp_path)
    store = Store(cfg.path_of("store_dir"))
    p = _hour_log(store, day, 9, [])
    store.add_item(source="activity", external_id=p.name, day=day,
                   ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                   kind="activity_log", path=str(p))
    digest, _ = build_digest(cfg, store, day)
    assert "What was on screen" not in digest
