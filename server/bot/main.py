"""Approval bot service (long polling) + WAHA webhook receiver.

Runs as a systemd service. Handles:
  - Approve / Another story / Replace text / Skip buttons on nightly drafts
  - The image step: Approve renders an illustration and shows it for a second
    confirmation, so nothing reaches LinkedIn on a single tap
  - Free-text conversation about the day: every message continues the SAME
    Claude session the draft was written in, so the model still has the whole
    digest (and can Read/Grep the raw day files when you ask it to dig)
  - WhatsApp messages arriving via WAHA webhook (localhost FastAPI, own thread)

Hard-locked to TG_ALLOWED_CHAT_ID; everything else is ignored.

Threading rule: Store owns one thread-bound sqlite connection, so everything
handed to asyncio.to_thread takes and returns plain values, and every DB call
stays on the event-loop thread.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from ..config import Config
from ..pipeline.draft import converse, image_brief
from ..pipeline.image_gen import ImageGenError, generate_image
from ..store import Store
from ..util import get_logger
from .linkedin_client import LinkedInClient, feed_url

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


def image_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Post with image", callback_data=f"postimg:{draft_id}"),
         InlineKeyboardButton("🔄 Regenerate", callback_data=f"regen:{draft_id}")],
        [InlineKeyboardButton("📄 Post text-only", callback_data=f"posttxt:{draft_id}"),
         InlineKeyboardButton("⏭ Cancel", callback_data=f"cancelimg:{draft_id}")],
    ])


def image_failed_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """After a failed render — you are never stuck with an approved, unpostable draft."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry image", callback_data=f"regen:{draft_id}"),
         InlineKeyboardButton("📄 Post text-only", callback_data=f"posttxt:{draft_id}")],
        [InlineKeyboardButton("⏭ Cancel", callback_data=f"cancelimg:{draft_id}")],
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

    # ---- images --------------------------------------------------------------

    async def start_image(self, context: ContextTypes.DEFAULT_TYPE, draft,
                          feedback: str | None = None) -> None:
        """Stage one of approval: brief → render → show it for confirmation.

        Acquires self.busy exactly once and never calls converse_turn (the lock is
        not reentrant). Regenerate re-enters here from a handler, after release.
        """
        draft_id = draft["id"]
        prev = self.store.latest_image(draft_id)
        n = (prev["n"] + 1) if prev else 1
        cap = int(self.cfg.get("image.max_regenerations", 6))
        if n > cap:
            await context.bot.send_message(
                self.chat_id,
                f"{cap} takes on this one already — post it, or Cancel and change the text.")
            return
        if self.busy.locked():
            await context.bot.send_message(self.chat_id, "Still working on the previous message…")
            return

        # resolve everything the workers need while still on the event-loop thread
        session_id = self.store.session_for_day(draft["day"])
        prev_prompt = prev["prompt"] if prev else None
        out_path = self.store.image_path_for(draft["day"], draft_id, n)
        post_text = draft["text"]

        async with self.busy:
            status = await context.bot.send_message(self.chat_id, "🎨 writing the image brief…")
            try:
                prompt, alt = await asyncio.to_thread(
                    image_brief, self.cfg, draft["day"], session_id, post_text,
                    feedback, prev_prompt)
            except Exception as e:
                log.exception("image brief failed")
                self.store.update_draft(draft_id, status="imaging")
                await status.edit_text(f"⚠️ image brief failed: {e}"[:4000],
                                       reply_markup=image_failed_keyboard(draft_id))
                return

            await status.edit_text(f"🎨 rendering take {n}…")
            try:
                img = await asyncio.to_thread(generate_image, self.cfg, prompt, out_path=out_path)
            except ImageGenError as e:
                log.warning("render failed (%s): %s", e.reason, e)
                self.store.add_image(draft_id, n, prompt=prompt, alt_text=alt,
                                     feedback=feedback, status="failed", error=str(e))
                self.store.update_draft(draft_id, status="imaging")
                detail = f"\n\n{e.detail}" if e.detail else ""
                await status.edit_text(
                    f"⚠️ image generation failed ({e.reason}): {e}{detail}"[:4000],
                    reply_markup=image_failed_keyboard(draft_id))
                return

            img_id = self.store.add_image(draft_id, n, prompt=prompt, alt_text=alt,
                                          feedback=feedback, path=str(img.path),
                                          mime=img.mime, model=img.model, status="ready")
            self.store.update_draft(draft_id, status="imaging")
            self.active_draft_id = draft_id

            # only the newest take keeps live buttons
            if prev is not None:
                self.store.update_image(prev["id"], status="discarded")
                if prev["tg_message_id"]:
                    try:
                        await context.bot.edit_message_reply_markup(
                            self.chat_id, int(prev["tg_message_id"]), reply_markup=None)
                    except Exception:
                        pass

            await status.delete()
            # photo carries the alt text (short, and worth reviewing); the post text
            # goes in its own message because it routinely exceeds the 1024 caption cap
            with open(img.path, "rb") as fh:
                photo = await context.bot.send_photo(self.chat_id, photo=fh,
                                                     caption=(alt or "")[:1024])
            sent = await context.bot.send_message(
                self.chat_id,
                (f"{post_text}\n\n— take {n}. Reply to steer the image "
                 f"(\"more abstract, no people\"). /talk to discuss the text instead.")[:4000],
                reply_to_message_id=photo.message_id,
                reply_markup=image_keyboard(draft_id))
            self.store.update_image(img_id, status="pending_review",
                                    tg_message_id=str(sent.message_id),
                                    tg_photo_message_id=str(photo.message_id))

    async def publish(self, context: ContextTypes.DEFAULT_TYPE, draft, img=None) -> None:
        """Stage two: actually send it to LinkedIn. The only place that publishes."""
        draft_id = draft["id"]
        if self.busy.locked():
            await context.bot.send_message(self.chat_id, "Still working on the previous message…")
            return
        async with self.busy:
            note = await context.bot.send_message(
                self.chat_id, "📤 posting to LinkedIn…" if img is None
                else "📤 uploading the image and posting…")
            try:
                urn, image_urn = await asyncio.to_thread(
                    self.linkedin.post, draft["text"],
                    Path(img["path"]) if img else None,
                    img["alt_text"] if img else None)
            except Exception as e:
                log.exception("post failed")
                await note.edit_text(
                    f"⚠️ LinkedIn post failed: {e}"[:4000],
                    reply_markup=image_keyboard(draft_id) if img else draft_keyboard(draft_id))
                return
            self.store.update_draft(draft_id, status="posted", posted_urn=urn)
            if img is not None:
                self.store.update_image(img["id"], status="attached", li_image_urn=image_urn)
            link = feed_url(urn)
            await note.edit_text(f"✅ Posted{' with image' if img else ''}. {link}")

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
            # strip the buttons before any slow work: q.answer() returns instantly,
            # so a second tap can otherwise arrive mid-flight
            await q.edit_message_reply_markup(None)
            if self.cfg.get("image.enabled", True):
                await self.start_image(context, draft)
            else:
                await self.publish(context, draft)

        elif action == "postimg":
            img = self.store.latest_image(draft_id, status="pending_review")
            if img is None or not img["path"]:
                await context.bot.send_message(self.chat_id, "No image is waiting on that draft.")
                return
            if draft["status"] == "posted":
                await q.edit_message_reply_markup(None)
                return
            await q.edit_message_reply_markup(None)
            await self.publish(context, draft, img)

        elif action == "posttxt":
            if draft["status"] == "posted":
                await q.edit_message_reply_markup(None)
                return
            await q.edit_message_reply_markup(None)
            img = self.store.latest_image(draft_id, status="pending_review")
            if img is not None:
                self.store.update_image(img["id"], status="discarded")
            await self.publish(context, draft)

        elif action == "regen":
            await q.edit_message_reply_markup(None)
            await self.start_image(context, draft)

        elif action == "cancelimg":
            img = self.store.latest_image(draft_id, status="pending_review")
            if img is not None:
                self.store.update_image(img["id"], status="discarded")
            self.store.update_draft(draft_id, status="pending")
            await q.edit_message_reply_markup(draft_keyboard(draft_id))
            await context.bot.send_message(
                self.chat_id, "⏭ Image dropped — the draft is back on the table.")

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

        # an image is on the table → free text steers the next take
        img = self.store.pending_image(int(self.cfg.get("image.pending_hours", 12)))
        if img is not None:
            d = self.store.get_draft(img["draft_id"])
            if d is not None and d["status"] == "imaging":
                await self.start_image(context, d, feedback=text)
                return

        # everything else is a conversation turn in the day's session, about the
        # draft you replied to (or the last one you touched)
        await self.converse_turn(context, text, target=self.target_draft(update))

    async def on_talk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Talk about the text while an image is pending, instead of steering the image."""
        if not self.allowed(update) or not update.message:
            return
        text = " ".join(context.args or []).strip()
        if not text:
            await update.message.reply_text("Usage: /talk make it shorter, lead with the number")
            return
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
            "Buttons: Approve illustrates it · Another angle rewrites it · "
            "Replace text takes your exact wording · Skip drops it.\n\n"
            "Approve doesn't publish. It draws an image for the post and shows it to you; "
            "nothing reaches LinkedIn until you tap Post with image (or Post text-only). "
            "While an image is on the table, plain messages steer the picture — "
            "use /talk to go back to discussing the words.\n"
            "/status shows health.")


def start_waha_webhook(cfg: Config):
    src = cfg.source_by_type("whatsapp")
    if not (src and src.get("enabled")):
        return
    port = int(src.get("webhook_port", 8477))
    # WAHA runs in a container and reaches the host on the docker bridge gateway
    # (host.docker.internal -> 172.17.0.1), so binding to loopback alone makes the
    # webhook undeliverable. Bind to that gateway when configured; it's a host-local
    # interface, not routable from outside, and the endpoint still checks the API key.
    host = str(src.get("webhook_host", "127.0.0.1"))

    def run():
        import time

        import uvicorn
        from .waha_webhook import build_app
        app = build_app(cfg, Store(cfg.path_of("store_dir")))  # own Store: sqlite per-thread
        # At boot the docker bridge may not exist yet, so binding to the gateway
        # address fails. Uvicorn exits on that, which used to kill this thread for
        # good: Telegram kept working (outbound polling) while WhatsApp captured
        # nothing until someone noticed. Keep retrying instead.
        for attempt in range(1, 61):
            try:
                uvicorn.run(app, host=host, port=port, log_level="warning")
                return  # served and shut down cleanly
            except (SystemExit, OSError) as e:
                log.warning("webhook bind on %s:%d failed (attempt %d): %s",
                            host, port, attempt, e or "address unavailable")
                time.sleep(10)
        log.error("giving up binding the WAHA webhook on %s:%d", host, port)

    threading.Thread(target=run, daemon=True, name="waha-webhook").start()
    log.info("WAHA webhook listening on %s:%d", host, port)


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
    app.add_handler(CommandHandler("talk", bot.on_talk))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    log.info("bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
