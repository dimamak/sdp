import json

from server.config import Config
from server.pipeline import draft
from server.pipeline.llm import LLMResult
from server.store import Store


def _cfg():
    return Config({"pipeline": {"max_drafts": 4}}, {}, None)


def _store(tmp_path) -> Store:
    return Store(tmp_path / "store")


def test_write_drafts_renders_recent_shapes_and_days_since_ask(tmp_path, monkeypatch):
    store = _store(tmp_path)
    ask_id = store.add_draft("2026-08-30", "An ask post?\n")
    store.update_draft(ask_id, status="posted", shape="ask")

    captured = {}

    def fake_run_llm(cfg, prompt, **kwargs):
        captured["prompt"] = prompt
        payload = {"candidates": [], "rejected": []}
        return LLMResult(text=json.dumps(payload), session_id="sess-1", backend="claude")

    monkeypatch.setattr(draft, "run_llm", fake_run_llm)
    draft.write_drafts(_cfg(), store, "2026-09-06", "digest text")

    prompt = captured["prompt"]
    assert "{RECENT_SHAPES}" not in prompt
    assert "{DAYS_SINCE_ASK}" not in prompt
    assert "2026-08-30" in prompt
    assert "ask" in prompt
    assert "7 days ago" in prompt  # 2026-09-06 - 2026-08-30


def test_write_drafts_shows_never_when_no_ask_yet(tmp_path, monkeypatch):
    store = _store(tmp_path)
    captured = {}

    def fake_run_llm(cfg, prompt, **kwargs):
        captured["prompt"] = prompt
        payload = {"candidates": [], "rejected": []}
        return LLMResult(text=json.dumps(payload), session_id="sess-1", backend="claude")

    monkeypatch.setattr(draft, "run_llm", fake_run_llm)
    draft.write_drafts(_cfg(), store, "2026-09-06", "digest text")

    assert "never" in captured["prompt"]


def test_write_drafts_persists_shape_from_candidate(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def fake_run_llm(cfg, prompt, **kwargs):
        payload = {
            "candidates": [
                {"fact": "a fact", "why": "reason", "shape": "ask", "post_text": "A post?\n"},
            ],
            "rejected": [],
        }
        return LLMResult(text=json.dumps(payload), session_id="sess-1", backend="claude")

    monkeypatch.setattr(draft, "run_llm", fake_run_llm)
    monkeypatch.setattr(draft, "deai_cleanup", lambda cfg, text: text)
    ids, rejected = draft.write_drafts(_cfg(), store, "2026-09-06", "digest text")

    assert len(ids) == 1
    assert store.get_draft(ids[0])["shape"] == "ask"
