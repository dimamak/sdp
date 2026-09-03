from datetime import datetime, timezone

from server.config import Config
from server.radar import spend
from server.store import Store

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _cfg(budget=5.0):
    return Config({"radar": {"monthly_budget_usd": budget}}, {}, None)


def test_record_returns_and_persists_cost(tmp_path):
    store = Store(tmp_path / "store")
    cost = spend.record(store, "post_read", units=10, note="search/recent")
    assert cost == 0.05
    assert store.radar_spend_since("2020-01-01T00:00:00+00:00") == 0.05


def test_status_reports_zero_when_nothing_spent(tmp_path):
    store = Store(tmp_path / "store")
    st = spend.status(_cfg(), store, now=NOW)
    assert st.spent == 0.0
    assert st.pct == 0.0
    assert not st.blocked


def test_status_blocks_at_100_percent(tmp_path):
    store = Store(tmp_path / "store")
    spend.record(store, "post_read", units=1000)  # $5.00
    st = spend.status(_cfg(5.0), store, now=NOW)
    assert st.pct >= 1.0
    assert st.blocked


def test_status_only_counts_current_utc_month(tmp_path):
    store = Store(tmp_path / "store")
    store.db.execute(
        "INSERT INTO radar_spend(ts, kind, units, unit_usd) VALUES(?,?,?,?)",
        ("2026-08-15 00:00:00", "post_read", 1000, 0.005))
    store.db.commit()
    st = spend.status(_cfg(5.0), store, now=NOW)
    assert st.spent == 0.0  # last month's spend doesn't count against this month's budget


def test_warning_fires_once_per_month(tmp_path):
    store = Store(tmp_path / "store")
    spend.record(store, "post_read", units=850)  # 85% of a $5 budget
    messages = []
    cfg = _cfg(5.0)
    spend.maybe_warn(cfg, store, messages.append, now=NOW)
    spend.maybe_warn(cfg, store, messages.append, now=NOW)
    assert len(messages) == 1


def test_no_warning_below_threshold(tmp_path):
    store = Store(tmp_path / "store")
    spend.record(store, "post_read", units=100)  # 10% of a $5 budget
    messages = []
    spend.maybe_warn(_cfg(5.0), store, messages.append, now=NOW)
    assert messages == []
