"""Approval bot service (long polling) + WAHA webhook receiver.

Runs as a systemd service. Handles:
  - Approve / Edit / Skip buttons on nightly drafts
  - Edit flow: next text message from the owner replaces the draft text
  - WhatsApp messages arriving via WAHA webhook (localhost FastAPI, own thread)

Hard-locked to TG_ALLOWED_CHAT_ID; everything else is ignored.
"""
from __future__ import annotations

import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from ..config import Config
from ..store import Store
from ..util import get_logger
from .linkedin_client import LinkedInClient

log = get_logger("bot")


def draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & post", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{draft_id}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{draft_id}"),
    ]])


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.path_of("store_dir"))
        self.chat_id = int(cfg.secret("TG_ALLOWED_CHAT_ID", "0"))
        self.linkedin = LinkedInClient(cfg)

    def allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.chat_id

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

        elif action == "edit":
            # any prior half-finished edit goes back to pending
            prev = self.store.latest_editing_draft()
            if prev and prev["id"] != draft_id:
                self.store.update_draft(prev["id"], status="pending")
            self.store.update_draft(draft_id, status="editing")
            await context.bot.send_message(
                self.chat_id, "✏️ Send the revised post text as your next message.")

        elif action == "skip":
            self.store.update_draft(draft_id, status="skipped")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(self.chat_id, "⏭ Skipped.")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update) or not update.message or not update.message.text:
            return
        draft = self.store.latest_editing_draft()
        if draft is None:
            return
        new_text = update.message.text.strip()
        self.store.update_draft(draft["id"], text=new_text, status="pending")
        await update.message.reply_text(
            f"Updated draft:\n\n{new_text}"[:4000], reply_markup=draft_keyboard(draft["id"]))

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        row = self.store.db.execute(
            "SELECT COUNT(*) c FROM items WHERE created_at >= datetime('now','-1 day')").fetchone()
        days = self.linkedin.days_until_expiry()
        await update.message.reply_text(
            f"dailypost alive. Items last 24h: {row['c']}. "
            f"LinkedIn token: {'n/a' if days is None else f'{days}d left'}.")


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    log.info("bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
