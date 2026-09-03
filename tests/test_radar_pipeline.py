import json
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar import pipeline, reply
from server.store import Store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, text):
        self.text = text


def _cfg():
    return Config({}, {}, None)


def _post(**kw):
    base = {
        "id": "42", "author_handle": "author", "text": "some post text",
        "created_at": (NOW - timedelta(minutes=10)).isoformat(), "views": 5000,
    }
    base.update(kw)
    return base


def _seed_post(store, post):
    store.upsert_radar_post(post["id"], author_handle=post["author_handle"], text=post["text"],
                            created_at=post["created_at"], lane="api",
                            first_seen_at=NOW.isoformat(), views=post["views"], state="seen")


def test_discard_marks_the_post_skipped(tmp_path):
    store = Store(tmp_path / "store")
    post = _post(views=1)  # below the reach floor -> gated
    _seed_post(store, post)
    result = pipeline.process(post, _cfg(), store, now=NOW)
    assert result["decision"] == "discard"
    assert store.get_radar_post("42")["state"] == "skipped"


def test_ask_persists_the_pending_question(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"question": "seen this?"})))
    result = pipeline.process(post, _cfg(), store, now=NOW)
    assert result["decision"] == "ask"
    row = store.get_radar_post("42")
    assert row["state"] == "asking"
    assert row["pending_question"] == "seen this?"


def test_draft_persists_a_radar_reply_row(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "hit this once"})))
    result = pipeline.process(post, _cfg(), store, now=NOW)
    assert result["decision"] == "draft"
    assert "reply_id" in result
    row = store.get_radar_post("42")
    assert row["state"] == "suggested"
    rep = store.get_radar_reply(result["reply_id"])
    assert rep["text"] == "hit this once"
    assert rep["status"] == "ready"


def test_process_answer_draft_records_qa_source(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.set_radar_post_state("42", "asking", pending_question="seen this?")
    monkeypatch.setattr(reply, "run_llm",
                        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "yes, twice"})))
    result = pipeline.process_answer(post, "seen this?", "yes, twice", _cfg(), store, now=NOW)
    assert result["decision"] == "draft"
    row = store.get_radar_post("42")
    assert row["state"] == "suggested"
    assert row["pending_question"] is None
    rep = store.get_radar_reply(result["reply_id"])
    assert rep["source"] == "qa"
    assert rep["answer"] == "yes, twice"


def test_low_score_with_evidence_is_discarded_not_drafted(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")

    def must_not_run(*a, **kw):
        raise AssertionError("must not draft a reply below min_score")
    monkeypatch.setattr(reply, "run_llm", must_not_run)

    cfg = Config({"radar": {"min_score": 0.999}}, {}, None)
    result = pipeline.process(post, cfg, store, now=NOW)
    assert result["decision"] == "discard"
    assert result["gate"] == "low_score"
    assert store.get_radar_post("42")["state"] == "skipped"


def test_daily_suggestion_cap_blocks_before_any_llm_call(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    for i in range(12):
        store.upsert_radar_post(f"seed-{i}", state="suggested", suggested_at=NOW.isoformat())

    def must_not_run(*a, **kw):
        raise AssertionError("must not score/draft once the daily cap is hit")
    monkeypatch.setattr(reply, "run_llm", must_not_run)

    cfg = Config({"radar": {"max_suggestions_per_day": 12}}, {}, None)
    result = pipeline.process(post, cfg, store, now=NOW)
    assert result["decision"] == "discard"
    assert result["gate"] == "daily_cap"
    assert store.get_radar_post("42")["state"] == "skipped"


def test_hourly_draft_cap_discards_a_would_be_draft(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    recent = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(3):
        reply_id = store.add_radar_reply(f"prior-{i}", text="t", status="ready", source="claude")
        store.db.execute("UPDATE radar_replies SET created_at=? WHERE id=?", (recent, reply_id))
    store.db.commit()
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "hit this once"})))

    cfg = Config({"radar": {"max_drafts_per_hour": 3}}, {}, None)
    result = pipeline.process(post, cfg, store, now=NOW)
    assert result["decision"] == "discard"
    assert result["gate"] == "hourly_cap"
    assert store.get_radar_post("42")["state"] == "skipped"


def test_process_answer_respects_the_hourly_draft_cap(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.set_radar_post_state("42", "asking", pending_question="seen this?")
    recent = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(3):
        reply_id = store.add_radar_reply(f"prior-{i}", text="t", status="ready", source="claude")
        store.db.execute("UPDATE radar_replies SET created_at=? WHERE id=?", (recent, reply_id))
    store.db.commit()
    monkeypatch.setattr(reply, "run_llm",
                        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "yes, twice"})))

    cfg = Config({"radar": {"max_drafts_per_hour": 3}}, {}, None)
    result = pipeline.process_answer(post, "seen this?", "yes, twice", cfg, store, now=NOW)
    assert result["decision"] == "discard"
    assert result["gate"] == "hourly_cap"
    assert store.get_radar_post("42")["state"] == "skipped"


def test_process_answer_stale_expires_without_drafting(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    stale = _post(created_at=(NOW - timedelta(minutes=999)).isoformat())
    _seed_post(store, stale)
    store.set_radar_post_state("42", "asking", pending_question="seen this?")

    def must_not_run(*a, **kw):
        raise AssertionError("must not draft from a stale answer")
    monkeypatch.setattr(reply, "draft_reply", must_not_run)

    result = pipeline.process_answer(stale, "seen this?", "yes", _cfg(), store, now=NOW)
    assert result["decision"] == "saved_stale"
    assert store.get_radar_post("42")["state"] == "expired"
