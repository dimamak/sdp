"""Tests run the ASGI app directly over httpx's ASGITransport inside
asyncio.run(), rather than fastapi.testclient.TestClient — TestClient runs
the app on a separate portal thread, which would touch this test's Store
connection from a thread other than the one that created it (the same
cross-thread sqlite rule localapi.py itself has to respect; see its
module docstring and server/bot/main.py's)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from server.bot import localapi
from server.config import Config
from server.radar import reply
from server.store import Store

TOKEN = "test-local-token"  # noqa: S105 -- test fixture, not a real secret


class _FakeResult:
    def __init__(self, text):
        self.text = text


def _cfg(**radar_over):
    data = {"radar": {"enabled": True, **radar_over}}
    return Config(data, {"RADAR_LOCAL_TOKEN": TOKEN}, None)


def _post_payload(**kw):
    # localapi.py scores against real wall-clock time (it has no `now`
    # override like the fixture-driven cli.py/pipeline.py), so created_at
    # has to be relative to it rather than a fixed fixture instant.
    base = {
        "id": "42", "author_handle": "author", "text": "some post text",
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "views": 5000,
    }
    base.update(kw)
    return base


def _app(tmp_path, cfg=None, notify=None):
    store = Store(tmp_path / "store")
    app = localapi.build_app(cfg or _cfg(), store, notify=notify)
    return app, store


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


async def _req(app, method, path, **kw):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
        return await client.request(method, path, **kw)


def test_missing_token_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[]))
    assert r.status_code == 403


def test_wrong_token_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[],
                          headers={"Authorization": "Bearer nope"}))
    assert r.status_code == 403


def test_oversized_body_is_rejected(tmp_path):
    app, _ = _app(tmp_path)
    huge = json.dumps([_post_payload(text="x" * (localapi.MAX_BODY_BYTES + 1000))])
    r = asyncio.run(_req(app, "POST", "/radar/posts", content=huge, headers=_auth()))
    assert r.status_code == 413


def test_valid_post_response_has_no_cors_headers(tmp_path):
    app, _ = _app(tmp_path)
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[], headers=_auth()))
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-private-network" not in r.headers


def test_draft_is_scored_and_notified(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "hit this once"})))
    notified = []
    app, store = _app(tmp_path, notify=lambda post, result: notified.append(result))
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == [{"post_id": "42", "decision": "draft"}]
    assert len(notified) == 1
    assert store.get_radar_post("42")["state"] == "suggested"


def test_resighting_the_same_post_is_a_noop(tmp_path, monkeypatch):
    calls = []

    def fake_run_llm(cfg, prompt, **kw):
        calls.append(1)
        return _FakeResult(json.dumps({"reply": "hit this once"}))
    monkeypatch.setattr(reply, "run_llm", fake_run_llm)
    app, store = _app(tmp_path)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    assert r.json()["results"] == []
    assert len(calls) == 1


def test_outside_active_hours_posts_are_seen_but_not_scored(tmp_path, monkeypatch):
    def must_not_run(*a, **kw):
        raise AssertionError("must not score outside active hours")
    monkeypatch.setattr(reply, "run_llm", must_not_run)
    cfg = _cfg(**{"extension": {"active_hours": "00:00-00:01"}})
    app, store = _app(tmp_path, cfg=cfg)
    r = asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    assert r.json()["results"] == []
    assert store.get_radar_post("42") is None


def test_suggestions_endpoint_returns_ready_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "hit this once"})))
    app, store = _app(tmp_path)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    r = asyncio.run(_req(app, "GET", "/radar/suggestions", params={"ids": "42,999"},
                         headers=_auth()))
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["post_id"] == "42"
    assert suggestions[0]["reply"] == "hit this once"


def test_replied_marks_post_and_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeResult(json.dumps({"reply": "hit this once"})))
    app, store = _app(tmp_path)
    store.add_item("claude_sessions", "1", "2026-09-01", summary="some post text overlap")
    asyncio.run(_req(app, "POST", "/radar/posts", json=[_post_payload()], headers=_auth()))
    r = asyncio.run(_req(app, "POST", "/radar/replied", json={"post_id": "42"}, headers=_auth()))
    assert r.status_code == 200
    assert store.get_radar_post("42")["state"] == "replied"
    rep = store.latest_radar_reply("42")
    assert rep["status"] == "replied"
