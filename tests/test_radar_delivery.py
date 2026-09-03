"""Telegram delivery for the X reply radar (plan.md §7) — server/bot/main.py.

Runtime, not AST-based like test_radar_never_posts.py: FakeTgBot below has no
method literally named `post`, so any code path that reached for one (the
real XClient/tweet-create call) would blow up here with an AttributeError
instead of silently succeeding. Belt-and-braces alongside the static guard,
which only scans server/radar/*.py and can't see this file.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from server.bot import main as bot_main
from server.config import Config
from server.store import Store

CHAT_ID = 555


class FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeTgBot:
    """No .post(...) anywhere on this object — see module docstring."""

    def __init__(self):
        self.sent = []
        self.cleared_markup = []
        self._next_id = 1

    async def send_message(self, chat_id, text, **kw):
        assert chat_id == CHAT_ID
        self.sent.append({"text": text, **kw})
        msg = FakeMessage(self._next_id)
        self._next_id += 1
        return msg

    async def edit_message_reply_markup(self, chat_id, message_id, **kw):
        self.cleared_markup.append(message_id)


class FakeQuery:
    def __init__(self):
        self.cleared = False

    async def edit_message_reply_markup(self, markup):
        assert markup is None
        self.cleared = True


class FakeUpdateMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append({"text": text, **kw})


class FakeUpdate:
    def __init__(self):
        self.message = FakeUpdateMessage()


class FakeContext:
    def __init__(self, tg_bot):
        self.bot = tg_bot


def _cfg(tmp_path):
    return Config({"store_dir": str(tmp_path / "store")},
                  {"TG_ALLOWED_CHAT_ID": str(CHAT_ID)}, None)


def _post(**kw):
    base = {"id": "42", "author_handle": "author", "text": "some post text",
            "created_at": "2026-09-01T11:50:00+00:00", "views": 5000}
    base.update(kw)
    return base


def _seed_post(store, post):
    store.upsert_radar_post(post["id"], author_handle=post["author_handle"], text=post["text"],
                            created_at=post["created_at"], lane="api",
                            first_seen_at="2026-09-01T12:00:00+00:00", views=post["views"],
                            state="seen", author_id="999")


def test_radar_keyboard_has_no_url_that_opens_x_composer():
    kb = bot_main.radar_keyboard("42", "https://x.com/i/status/42", "hit this once")
    urls = [btn.url for row in kb.inline_keyboard for btn in row if btn.url]
    assert urls == ["https://x.com/i/status/42"]
    assert all("intent/post" not in u for u in urls)
    copy_buttons = [btn for row in kb.inline_keyboard for btn in row if btn.copy_text]
    assert len(copy_buttons) == 1
    assert copy_buttons[0].copy_text.text == "hit this once"
    callbacks = {btn.callback_data for row in kb.inline_keyboard
                for btn in row if btn.callback_data}
    assert callbacks == {"radreplied:42", "radredo:42", "radedit:42", "radskip:42"}


def test_deliver_ask_sends_the_question_and_touches_no_reply_row(tmp_path):
    store = Store(tmp_path / "store")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    post = _post()
    asyncio.run(bot.deliver_radar_result(tg, post, {"decision": "ask", "question": "seen this?"}))
    assert len(tg.sent) == 1
    assert "seen this?" in tg.sent[0]["text"]


def test_deliver_draft_sends_the_card_and_records_the_message_id(tmp_path):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    reply_id = store.add_radar_reply(post["id"], text="hit this once", status="ready")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    asyncio.run(bot.deliver_radar_result(
        tg, post, {"decision": "draft", "reply": "hit this once", "reply_id": reply_id}))
    assert len(tg.sent) == 1
    assert "hit this once" in tg.sent[0]["text"]
    rep = store.get_radar_reply(reply_id)
    assert rep["tg_message_id"] == "1"


def test_radreplied_marks_reply_and_post_and_author(tmp_path):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.upsert_radar_author("999", handle="author")  # resolve_author_id upserts this before
                                                        # any post from them is ever sighted
    store.set_radar_post_state(post["id"], "suggested")
    reply_id = store.add_radar_reply(post["id"], text="hit this once", status="ready")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    q = FakeQuery()
    asyncio.run(bot.on_radar_callback(tg, q, "radreplied", post["id"]))
    assert q.cleared
    assert store.get_radar_reply(reply_id)["status"] == "replied"
    assert store.get_radar_post(post["id"])["state"] == "replied"
    assert store.get_radar_author("999")["user_replied"] == 1


def test_radskip_marks_post_and_reply_skipped(tmp_path):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    reply_id = store.add_radar_reply(post["id"], text="hit this once", status="ready")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    q = FakeQuery()
    asyncio.run(bot.on_radar_callback(tg, q, "radskip", post["id"]))
    assert q.cleared
    assert store.get_radar_reply(reply_id)["status"] == "skipped"
    assert store.get_radar_post(post["id"])["state"] == "skipped"


def test_radqskip_clears_the_pending_question(tmp_path):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.set_radar_post_state(post["id"], "asking", pending_question="seen this?")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    q = FakeQuery()
    asyncio.run(bot.on_radar_callback(tg, q, "radqskip", post["id"]))
    assert q.cleared
    row = store.get_radar_post(post["id"])
    assert row["state"] == "skipped"
    assert row["pending_question"] is None


class _FakeLLMResult:
    def __init__(self, text):
        self.text = text


def test_radredo_adds_a_new_take_and_clears_the_old_message(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    store.add_radar_reply(post["id"], n=1, text="take one", status="ready", tg_message_id="7")
    monkeypatch.setattr(
        bot_main.radar_reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeLLMResult('{"reply": "take two"}'))
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    q = FakeQuery()
    asyncio.run(bot.on_radar_callback(tg, q, "radredo", post["id"]))
    assert q.cleared
    assert 7 in tg.cleared_markup
    rows = store.db.execute(
        "SELECT * FROM radar_replies WHERE post_id=? ORDER BY id", (post["id"],)).fetchall()
    assert [r["n"] for r in rows] == [1, 2]
    assert rows[1]["text"] == "take two"
    assert store.get_radar_post(post["id"])["state"] == "suggested"


def test_process_message_edit_mode_replaces_the_reply_verbatim(tmp_path):
    store = Store(tmp_path / "store")
    post = _post()
    _seed_post(store, post)
    reply_id = store.add_radar_reply(post["id"], text="old text", status="editing")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    update = FakeUpdate()
    ctx = FakeContext(FakeTgBot())
    asyncio.run(bot._process_message(update, ctx, "new verbatim text"))
    rep = store.get_radar_reply(reply_id)
    assert rep["text"] == "new verbatim text"
    assert rep["status"] == "ready"
    assert rep["source"] == "manual"
    assert "new verbatim text" in update.message.sent[0]["text"]


def test_process_message_answers_a_pending_radar_question(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    post = _post(created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    _seed_post(store, post)
    store.set_radar_post_state(post["id"], "asking", pending_question="seen this?")
    monkeypatch.setattr(
        bot_main.radar_reply, "run_llm",
        lambda cfg, prompt, **kw: _FakeLLMResult('{"reply": "yes, twice"}'))
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    update = FakeUpdate()
    ctx = FakeContext(tg)
    asyncio.run(bot._process_message(update, ctx, "yes, twice"))
    row = store.get_radar_post(post["id"])
    assert row["state"] == "suggested"
    assert row["pending_question"] is None
    assert len(tg.sent) == 1
    rep = store.latest_radar_reply(post["id"])
    assert rep["source"] == "qa"
    assert rep["answer"] == "yes, twice"


def test_unknown_post_id_just_clears_the_button(tmp_path):
    store = Store(tmp_path / "store")
    bot = bot_main.Bot(_cfg(tmp_path))
    bot.store = store
    tg = FakeTgBot()
    q = FakeQuery()
    asyncio.run(bot.on_radar_callback(tg, q, "radskip", "does-not-exist"))
    assert q.cleared
    assert tg.sent == []
