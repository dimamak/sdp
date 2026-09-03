from datetime import datetime, timedelta, timezone

from server.config import Config
from server.pipeline.run_nightly import prune_radar_posts
from server.store import Store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _cfg(retention_days=30):
    return Config({"retention_days": retention_days}, {}, None)


def _seed_post(store, post_id: str, first_seen_at: datetime) -> None:
    store.upsert_radar_post(
        post_id, author_id="a1", author_handle="author", text="hi",
        created_at=first_seen_at.isoformat(), lane="extension",
        first_seen_at=first_seen_at.isoformat(), views=100, replies=0,
        likes=0, reposts=0, reach_rate=10.0, state="seen",
    )


def test_prune_radar_posts_deletes_only_stale_rows_and_their_replies(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    monkeypatch.setattr(
        "server.pipeline.run_nightly.datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: NOW)}),
    )
    _seed_post(store, "stale", NOW - timedelta(days=31))
    _seed_post(store, "fresh", NOW - timedelta(days=1))
    store.add_radar_reply("stale", text="old reply")
    store.add_radar_reply("fresh", text="new reply")

    prune_radar_posts(_cfg(), store)

    assert store.get_radar_post("stale") is None
    assert store.get_radar_post("fresh") is not None
    assert store.latest_radar_reply("stale") is None
    assert store.latest_radar_reply("fresh") is not None


def test_prune_radar_posts_never_touches_radar_authors(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    monkeypatch.setattr(
        "server.pipeline.run_nightly.datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: NOW)}),
    )
    store.upsert_radar_author("a1", handle="author", on_watchlist=1,
                              watchlist_added_at=(NOW - timedelta(days=999)).isoformat())
    _seed_post(store, "stale", NOW - timedelta(days=31))

    prune_radar_posts(_cfg(), store)

    assert store.get_radar_author("a1") is not None


def test_prune_radar_posts_disabled_when_retention_days_is_zero(tmp_path):
    store = Store(tmp_path / "store")
    _seed_post(store, "stale", NOW - timedelta(days=999))
    prune_radar_posts(_cfg(retention_days=0), store)
    assert store.get_radar_post("stale") is not None
