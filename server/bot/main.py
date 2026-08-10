"""Approval bot service (long polling) + WAHA webhook receiver.

Runs as a systemd service. Handles:
  - Approve / Another story / Replace text / Skip buttons on nightly drafts
  - Free-text conversation about the day: every message continues the SAME
    Claude session the draft was written in, so the model still has the whole
    digest (and can Read/Grep the raw day files when you ask it to dig)
  - WhatsApp messages arriving via WAHA webhook (localhost FastAPI, own thread)

Hard-locked to TG_ALLOWED_CHAT_ID; everything else is ignored.
"""
from __future__ import annotations

import asyncio
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from ..config import Config
from ..pipeline.draft import converse
from ..store import Store
from ..util import get_logger
from .linkedin_client import LinkedInClient

log = get_logger("bot")

ANOTHER_STORY_MSG = ("Write a different angle on the post under discussion, or a post for "
                     "another fact from today if this one is exhausted. Say briefly what changed.")


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve & post", callback_data=f"approve:{draft_id}"),
         InlineKeyboardButton("🔁 Another story", callback_data=f"another:{draft_id}")],
        [InlineKeyboardButton("✏️ Replace text", callback_data=f"edit:{draft_id}"),
         InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{draft_id}")],
    ])


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.path_of("store_dir"))
        self.chat_id = int(cfg.secret("TG_ALLOWED_CHAT_ID", "0"))
        self.linkedin = LinkedInClient(cfg)
        self.busy = asyncio.Lock()
        self.active_draft_id: int | None = None  # last draft you interacted with

    def allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.chat_id

    # ---- conversation --------------------------------------------------------

    def target_draft(self, update: Update):
        """Which draft is this message about? Explicit reply wins, then the last
        one you touched, then the newest of the day."""
        msg = update.message if update else None
        if msg is not None and msg.reply_to_message is not None:
            d = self.store.draft_by_tg_message(msg.reply_to_message.message_id)
            if d is not None:
                return d
        if self.active_draft_id is not None:
            d = self.store.get_draft(self.active_draft_id)
            if d is not None:
                return d
        row = self.store.latest_day_session()
        return self.store.latest_draft_for_day(row["day"]) if row else None

    async def converse_turn(self, context: ContextTypes.DEFAULT_TYPE, text: str,
                            target=None, new_draft: bool = False):
        """Send one message into the day's session and act on the answer.

        `target` is the draft under discussion; a returned post updates it unless
        new_draft is set, in which case it becomes an additional draft.
        """
        row = self.store.latest_day_session()
        if row is None:
            await context.bot.send_message(
                self.chat_id, "No day session yet — the nightly run creates one when it drafts.")
            return
        if self.busy.locked():
            await context.bot.send_message(self.chat_id, "Still working on the previous message…")
            return
        async with self.busy:
            ack = await context.bot.send_message(self.chat_id, "🤔 thinking…")
            try:
                reply, post = await asyncio.to_thread(
                    converse, self.cfg, self.store, row["day"], row["session_id"], text,
                    target["text"] if target is not None else None)
            except Exception as e:
                log.exception("converse failed")
                await ack.edit_text(f"⚠️ {e}")
                return
            if not post:
                await ack.edit_text(reply[:4000])
                return
            if target is not None and not new_draft:
                self.store.update_draft(target["id"], text=post, status="pending")
                draft_id = target["id"]
            else:
                draft_id = self.store.add_draft(row["day"], post)
            self.active_draft_id = draft_id
            sent = await ack.edit_text(f"{reply}\n\n{post}"[:4000],
                                       reply_markup=draft_keyboard(draft_id))
            self.store.update_draft(draft_id, tg_message_id=str(sent.message_id))

    # ---- handlers ------------------------------------------------------------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        q = update.callback_query
        await q.answer()
        action, _, draft_id_s = q.data.partition(":")
        draft_id = int(draft_id_s)
        draft = self.store.get_draft(draft_id)
        if draft is None:
            await q.edit_message_reply_markup(None)
            return
        self.active_draft_id = draft_id

        if action == "approve":
            if draft["status"] == "posted":
                await q.edit_message_reply_markup(None)
                return
            try:
                urn = self.linkedin.post(draft["text"])
            except Exception as e:
                log.exception("post failed")
                await context.bot.send_message(self.chat_id, f"⚠️ LinkedIn post failed: {e}")
                return
            self.store.update_draft(draft_id, status="posted", posted_urn=urn)
            await q.edit_message_reply_markup(None)
            link = f"https://www.linkedin.com/feed/update/{urn}/" if urn.startswith("urn:") else ""
            await context.bot.send_message(self.chat_id, f"✅ Posted. {link}")

        elif action == "another":
            await self.converse_turn(context, ANOTHER_STORY_MSG, target=draft, new_draft=True)

        elif action == "edit":
            prev = self.store.latest_editing_draft()
            if prev and prev["id"] != draft_id:
                self.store.update_draft(prev["id"], status="pending")
            self.store.update_draft(draft_id, status="editing")
            await context.bot.send_message(
                self.chat_id,
                "✏️ Send the exact replacement text as your next message "
                "(it will be used verbatim, the model won't see it).")

        elif action == "skip":
            self.store.update_draft(draft_id, status="skipped")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(self.chat_id, "⏭ Skipped.")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update) or not update.message or not update.message.text:
            return
        text = update.message.text.strip()

        # explicit verbatim-replace mode (set by the "Replace text" button)
        draft = self.store.latest_editing_draft()
        if draft is not None:
            self.store.update_draft(draft["id"], text=text, status="pending")
            await update.message.reply_text(
                f"Updated draft:\n\n{text}"[:4000], reply_markup=draft_keyboard(draft["id"]))
            return

        # everything else is a conversation turn in the day's session, about the
        # draft you replied to (or the last one you touched)
        await self.converse_turn(context, text, target=self.target_draft(update))

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        row = self.store.db.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= datetime('now','-1 day')").fetchone()
        days = self.linkedin.days_until_expiry()
        sess = self.store.latest_day_session()
        await update.message.reply_text(
            f"dailypost alive. Items last 24h: {row['c']}. "
            f"LinkedIn token: {'n/a' if days is None else f'{days}d left'}. "
            f"Session day: {sess['day'] if sess else 'none'}.")

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        await update.message.reply_text(
            "Each night you get one draft per interesting fact found in your day, "
            "plus a list of what was considered and dropped.\n\n"
            "Just talk to me — every message continues the same conversation, with the "
            "full day in context. Reply to a specific draft to talk about that one; "
            "otherwise I assume the last one you touched.\n\n"
            "Try:\n"
            "• make it shorter, lead with the number\n"
            "• draft the one about the idle workloads instead\n"
            "• search today for anything about the payment bug\n"
            "• end this one by asking how others handle it\n\n"
            "Buttons: Approve posts it · Another angle rewrites it · "
            "Replace text takes your exact wording · Skip drops it.\n"
            "/status shows health.")


def start_waha_webhook(cfg: Config):
    src = cfg.source_by_type("whatsapp")
    if not (src and src.get("enabled")):
        return
    port = int(src.get("webhook_port", 8477))

    def run():
        import uvicorn
        from .waha_webhook import build_app
        app = build_app(cfg, Store(cfg.path_of("store_dir")))  # own Store: sqlite per-thread
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    threading.Thread(target=run, daemon=True, name="waha-webhook").start()
    log.info("WAHA webhook listening on 127.0.0.1:%d", port)


def main() -> None:
    cfg = Config.load()
    token = cfg.secret("TG_BOT_TOKEN")
    if not token or not cfg.secret("TG_ALLOWED_CHAT_ID"):
        raise SystemExit("TG_BOT_TOKEN / TG_ALLOWED_CHAT_ID not set in .env")

    start_waha_webhook(cfg)

    bot = Bot(cfg)
    app = Application.builder().token(token).build()
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(CommandHandler("status", bot.on_status))
    app.add_handler(CommandHandler("help", bot.on_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    log.info("bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
