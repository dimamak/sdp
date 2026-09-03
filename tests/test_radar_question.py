from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar import cli as radar_cli
from server.radar import reply, score
from server.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cfg():
    return Config({}, {}, None)


def _post(**kw):
    base = {
        "id": "42", "author_handle": "author", "text": "some post text",
        "created_at": (NOW - timedelta(minutes=10)).isoformat(), "views": 5000,
    }
    base.update(kw)
    return base


def test_answer_is_saved_as_a_corpus_item(tmp_path):
    store = Store(tmp_path / "store")
    ok = reply.save_answer(store, _post(), "did you hit this?", "yes, twice", "2026-09-01")
    assert ok
    rows = store.radar_corpus()
    assert len(rows) == 1
    assert "yes, twice" in rows[0]["summary"]
    assert rows[0]["source"] == "radar_qa"


def test_stale_post_is_still_saved_but_not_drafted_by_cli(tmp_path, monkeypatch):
    cfg = _cfg()
    store = Store(tmp_path / "store")
    stale_post = _post(created_at=(NOW - timedelta(minutes=999)).isoformat())
    assert score.is_expired(stale_post, cfg, now=NOW)

    def must_not_be_called(*a, **kw):
        raise AssertionError("draft_reply must not run for an expired post")
    monkeypatch.setattr(radar_cli.reply, "draft_reply", must_not_be_called)

    out = radar_cli.run_answer(stale_post, "q", "a", cfg, store, now=NOW)
    assert out["decision"] == "saved_stale"
    assert len(store.radar_corpus()) == 1


def test_fresh_post_drafts_from_the_saved_answer(tmp_path, monkeypatch):
    cfg = _cfg()
    store = Store(tmp_path / "store")
    fresh_post = _post()

    class _Result:
        status = "ready"
        text = "drafted from the answer"
        error = None
    monkeypatch.setattr(radar_cli.reply, "draft_reply", lambda *a, **kw: _Result())

    out = radar_cli.run_answer(fresh_post, "q", "a", cfg, store, now=NOW)
    assert out["decision"] == "draft"
    assert out["reply"] == "drafted from the answer"
    assert len(store.radar_corpus()) == 1
