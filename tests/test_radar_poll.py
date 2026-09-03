from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar import poll
from server.store import Store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _cfg(watchlist=None):
    env = {"X_API_KEY": "k", "X_API_SECRET": "s",
           "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts"}
    watchlist = ["author"] if watchlist is None else watchlist
    return Config({"radar": {"watchlist": watchlist}}, env, None)


def test_empty_watchlist_polls_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")

    def boom(*a, **kw):
        raise AssertionError("must not call the network with an empty watchlist")
    monkeypatch.setattr(poll.requests, "get", boom)
    assert poll.poll_once(_cfg(watchlist=[]), store, now=NOW) == []


def test_missing_credentials_polls_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")

    def boom(*a, **kw):
        raise AssertionError("must not call the network without credentials")
    monkeypatch.setattr(poll.requests, "get", boom)
    cfg = Config({"radar": {"watchlist": ["author"]}}, {}, None)
    assert poll.poll_once(cfg, store, now=NOW) == []


def test_over_budget_polls_nothing(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    store.record_radar_spend("post_read", 10000, 0.005)  # blow well past any default budget

    def boom(*a, **kw):
        raise AssertionError("must not call the network over budget")
    monkeypatch.setattr(poll.requests, "get", boom)
    assert poll.poll_once(_cfg(), store, now=NOW) == []


def test_resolve_author_id_caches_and_bills_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _FakeResponse(200, {"data": {"id": "999", "username": "author"}})
    monkeypatch.setattr(poll.requests, "get", fake_get)
    cfg = _cfg()

    aid1 = poll.resolve_author_id(cfg, store, "author")
    aid2 = poll.resolve_author_id(cfg, store, "author")
    assert aid1 == aid2 == "999"
    assert len(calls) == 1  # second call hit the radar_authors cache
    assert store.radar_spend_since("2020-01-01T00:00:00+00:00") == 0.01


def test_poll_once_stores_new_posts_and_advances_cursor(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    created = (NOW - timedelta(minutes=10)).isoformat()

    def fake_get(url, **kw):
        if "users/by/username" in url:
            return _FakeResponse(200, {"data": {"id": "999"}})
        return _FakeResponse(200, {"data": [
            {"id": "42", "author_id": "999", "text": "hello world",
             "created_at": created,
             "public_metrics": {"impression_count": 5000, "like_count": 50,
                                "retweet_count": 5, "reply_count": 3}},
        ]})
    monkeypatch.setattr(poll.requests, "get", fake_get)

    posts = poll.poll_once(_cfg(), store, now=NOW)
    assert len(posts) == 1
    assert posts[0]["author_handle"] == "author"
    assert posts[0]["views"] == 5000
    assert store.get_radar_post("42") is not None
    assert store.get_cursor("radar_x_search") == "42"


def test_poll_once_skips_posts_from_unresolved_authors(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    created = (NOW - timedelta(minutes=10)).isoformat()

    def fake_get(url, **kw):
        if "users/by/username" in url:
            return _FakeResponse(200, {"data": {"id": "999"}})
        return _FakeResponse(200, {"data": [
            {"id": "42", "author_id": "someone-else", "text": "hello",
             "created_at": created, "public_metrics": {}},
        ]})
    monkeypatch.setattr(poll.requests, "get", fake_get)

    assert poll.poll_once(_cfg(), store, now=NOW) == []


def test_build_query_ors_handles_and_excludes_replies_and_retweets():
    q = poll.build_query(["alice", "@bob"])
    assert q == "(from:alice OR from:bob) -is:reply -is:retweet"
