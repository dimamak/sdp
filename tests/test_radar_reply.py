import json
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar import reply

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, text):
        self.text = text


def _cfg():
    return Config({}, {}, None)


def _post(**kw):
    base = {
        "id": "1", "author_handle": "author",
        "text": "Anyone hit disk blowups from Postgres replication slots?",
        "created_at": (NOW - timedelta(minutes=10)).isoformat(),
        "views": 5000,
    }
    base.update(kw)
    return base


def _mock_llm(monkeypatch, response_text, capture: dict | None = None):
    def fake(cfg, prompt, **kwargs):
        if capture is not None:
            capture["prompt"] = prompt
            capture["kwargs"] = kwargs
        return _FakeResult(response_text)
    monkeypatch.setattr(reply, "run_llm", fake)


def test_draft_reply_returns_ready_within_limit(monkeypatch):
    _mock_llm(monkeypatch, json.dumps(
        {"reply": "Hit this exact issue once — WAL piled up fast."}))
    result = reply.draft_reply(
        _cfg(), _post(), [{"summary": "ran into replication slot bloat"}], now=NOW)
    assert result.status == "ready"
    assert result.text


def test_draft_reply_never_passes_tools_or_read_dirs(monkeypatch):
    capture: dict = {}
    _mock_llm(monkeypatch, json.dumps({"reply": "short reply"}), capture)
    reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert "allow_read_dirs" not in capture["kwargs"]
    assert "allowed_tools" not in capture["kwargs"]


def test_draft_question_never_passes_tools_or_read_dirs(monkeypatch):
    capture: dict = {}
    _mock_llm(monkeypatch, json.dumps({"question": "seen this before?"}), capture)
    reply.draft_question(_cfg(), _post(), now=NOW)
    assert "allow_read_dirs" not in capture["kwargs"]
    assert "allowed_tools" not in capture["kwargs"]


def test_draft_reply_delimits_post_as_untrusted(monkeypatch):
    capture: dict = {}
    _mock_llm(monkeypatch, json.dumps({"reply": "ok"}), capture)
    post = _post()
    reply.draft_reply(_cfg(), post, [], now=NOW)
    assert "UNTRUSTED THIRD-PARTY CONTENT" in capture["prompt"]
    assert post["text"] in capture["prompt"]


def test_draft_reply_over_limit_fails_soft(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"reply": "x" * 300}))
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "failed"
    assert result.text is None
    assert "300" in result.error


def test_draft_reply_extra_mention_fails_soft(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"reply": "@author nice, also cc @randomguy"}))
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "failed"


def test_draft_reply_mentioning_only_the_author_is_fine(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"reply": "@author hit this exact issue once"}))
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "ready"


def test_draft_reply_new_url_fails_soft(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"reply": "check this out https://example.com"}))
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "failed"


def test_draft_reply_url_allowed_if_original_had_one(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"reply": "same link works for me https://example.com"}))
    post = _post(text="check this out https://example.com")
    result = reply.draft_reply(_cfg(), post, [], now=NOW)
    assert result.status == "ready"


def test_draft_reply_malformed_json_fails_soft_not_crash(monkeypatch):
    _mock_llm(monkeypatch, "not json at all, no braces either")
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "failed"
    assert result.text is None


def test_draft_reply_llm_exception_fails_soft_not_crash(monkeypatch):
    def boom(cfg, prompt, **kwargs):
        raise RuntimeError("claude -p failed")
    monkeypatch.setattr(reply, "run_llm", boom)
    result = reply.draft_reply(_cfg(), _post(), [], now=NOW)
    assert result.status == "failed"
    assert "claude -p failed" in result.error


def test_draft_question_returns_ready(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"question": "Have you seen this in prod?"}))
    result = reply.draft_question(_cfg(), _post(), now=NOW)
    assert result.status == "ready"
    assert "?" in result.text


def test_draft_question_empty_answer_fails_soft(monkeypatch):
    _mock_llm(monkeypatch, json.dumps({"question": "  "}))
    result = reply.draft_question(_cfg(), _post(), now=NOW)
    assert result.status == "failed"
