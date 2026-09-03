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
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import Config
from ..pipeline.draft import converse, image_brief, reddit_body, reddit_title, x_rewrite
from ..pipeline.image_check import check_image_text
from ..pipeline.image_gen import ImageGenError, generate_image
from ..pipeline.publish_window import in_window, next_slot, parse_window, slot_taken
from ..radar import pipeline as radar_pipeline
from ..radar import reply as radar_reply
from ..radar import retrieve as radar_retrieve
from ..radar import watchlist as radar_watchlist
from ..store import Store
from ..util import get_logger
from .linkedin_client import LinkedInClient, feed_url
from .x_client import XClient
from .x_client import tweet_url as x_tweet_url

log = get_logger("bot")

X_PREMIUM_MAX_CHARS = 25000  # X's long-form post cap for Premium/Premium+ subscribers

# Telegram clients split messages over ~4096 chars into several sends before they
# even hit the bot; buffer fragments arriving this close together and process
# them as one turn instead of firing a separate agent run per fragment.
MESSAGE_DEBOUNCE_SECONDS = 1.5

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


def queued_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Post now anyway", callback_data=f"postnow:{draft_id}")],
    ])


def x_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐦 Post to X", callback_data=f"xpost:{draft_id}"),
         InlineKeyboardButton("🔁 Rewrite", callback_data=f"xredo:{draft_id}")],
        [InlineKeyboardButton("✏️ Replace text", callback_data=f"xedit:{draft_id}"),
         InlineKeyboardButton("⏭ Skip X", callback_data=f"xskip:{draft_id}")],
    ])


def x_failed_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry", callback_data=f"xstart:{draft_id}"),
         InlineKeyboardButton("⏭ Skip X", callback_data=f"xskip:{draft_id}")],
    ])


def x_start_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """Shown when the X step was queued behind a still-running message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐦 Write X version", callback_data=f"xstart:{draft_id}")],
    ])


def reddit_keyboard(draft_id: int, app_link: str, browser_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("using reddit app", url=app_link),
         InlineKeyboardButton("using browser", url=browser_link)],
        [InlineKeyboardButton("✅ Mark as posted", callback_data=f"rpost:{draft_id}"),
         InlineKeyboardButton("🔁 New title", callback_data=f"rredo:{draft_id}")],
        [InlineKeyboardButton("✏️ Edit title", callback_data=f"redit:{draft_id}"),
         InlineKeyboardButton("⏭ Skip", callback_data=f"rskip:{draft_id}")],
    ])


def reddit_start_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """Shown when the Reddit step was queued behind a still-running message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📮 Reddit link", callback_data=f"rstart:{draft_id}")],
    ])


def radar_keyboard(post_id: str, post_url: str, reply_text: str) -> InlineKeyboardMarkup:
    """Copy/Open are plain client-side buttons — Telegram handles copy_text and
    url entirely on-device, no callback ever fires for them. Only the bottom
    row round-trips to on_callback. This deliberately departs from plan.md
    §7's literal radcopy/radpost callback names, since those two aren't
    actually callback-shaped: nothing here can ever open X's composer or tap
    Post, so there is no automated-posting path to guard against."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy reply", copy_text=CopyTextButton(reply_text[:256])),
         InlineKeyboardButton("↗️ Open post", url=post_url)],
        [InlineKeyboardButton("✅ Replied", callback_data=f"radreplied:{post_id}"),
         InlineKeyboardButton("🔁 Redo", callback_data=f"radredo:{post_id}")],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"radedit:{post_id}"),
         InlineKeyboardButton("⏭ Skip", callback_data=f"radskip:{post_id}")],
    ])


def radar_ask_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip", callback_data=f"radqskip:{post_id}")],
    ])


def radar_watchlist_add_keyboard(author_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add", callback_data=f"radadd:{author_id}"),
         InlineKeyboardButton("Not now", callback_data=f"radnotnow:{author_id}"),
         InlineKeyboardButton("🚫 Never", callback_data=f"radnever:{author_id}")],
    ])


def radar_watchlist_swap_keyboard(author_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Swap", callback_data=f"radswap:{author_id}"),
         InlineKeyboardButton("Keep incumbent", callback_data=f"radkeep:{author_id}"),
         InlineKeyboardButton("🚫 Never", callback_data=f"radnever:{author_id}")],
    ])


def _fmt_rate(views_per_min: float) -> str:
    v = float(views_per_min or 0)
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"


def radar_watchlist_add_text(candidate, size: int, cap: int) -> str:
    handle = candidate["handle"]
    rate = _fmt_rate(candidate["baseline_reach_rate"])
    replies = candidate["median_replies_at_sighting"] or 0
    overlap = radar_watchlist.overlap_count(candidate)
    total_cost = (size + 1) * radar_watchlist.COST_PER_ACCOUNT_USD
    return (
        f"👤 @{handle} looks worth watching\n\n"
        f"Seen {candidate['times_seen']} times · median {rate} views/min · "
        f"usually {replies:.0f} replies when you see it\n"
        f"{overlap} of their posts overlap your work\n\n"
        f"Adding costs ~${radar_watchlist.COST_PER_ACCOUNT_USD:.2f}/mo → watchlist would be "
        f"{size + 1}/{cap}, ~${total_cost:.2f}/mo"
    )


def radar_watchlist_swap_text(candidate, incumbent) -> str:
    cand_rate = _fmt_rate(candidate["baseline_reach_rate"])
    inc_rate = _fmt_rate(incumbent["baseline_reach_rate"])
    overlap = radar_watchlist.overlap_count(candidate)
    return (
        f"🔁 @{candidate['handle']} looks stronger than @{incumbent['handle']}\n\n"
        f"@{candidate['handle']}   {cand_rate} views/min median · "
        f"{candidate['times_seen']} sightings · {overlap} topic matches\n"
        f"@{incumbent['handle']}  {inc_rate} views/min median · "
        f"{incumbent['times_seen']} sightings"
    )


def _radar_quote(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def radar_ask_text(post: dict, question: str) -> str:
    handle = post.get("author_handle") or "?"
    views = int(post.get("views") or 0)
    return (f"❓ @{handle} · {views} views\n\n\"{_radar_quote(post.get('text', ''))}\"\n\n"
            f"{question}\n\nReply here with the answer, or Skip.")


def radar_draft_text(post: dict, reply_text: str, reason: str = "") -> str:
    handle = post.get("author_handle") or "?"
    views = int(post.get("views") or 0)
    body = (f"🐦 @{handle} · {views} views\n\n\"{_radar_quote(post.get('text', ''))}\"\n\n"
            f"↳ {reply_text}")
    return f"{body}\n\n{reason}" if reason else body


def reddit_submit_link(subreddit: str, title: str, body: str,
                       max_chars: int = 4000) -> tuple[tuple[str, bool], tuple[str, bool]]:
    """Prefilled Reddit submit URLs. Returns ((app_url, body_included),
    (browser_url, body_included)).

    quote(), not quote_plus() — Reddit's form reads a literal '+' in the body.
    If a variant's full URL would exceed max_chars, its body is dropped from
    the link (title-only prefill); the body always still ships in its own
    copy-block message regardless, so nothing is silently lost, only the
    prefill.

    Two variants because they need different newline encoding and there's no
    single string that works for both: tapping the link from Telegram hands
    off to the Reddit app (universal link), whose deep-link handler decodes
    the URL an extra time before Reddit's own composer sees it, so a single
    %0A arrives as a raw, invalid newline and gets dropped, flattening
    paragraphs — it needs %250A instead, which survives that extra decode. A
    browser opening the link directly only decodes once, so it needs the
    plain single %0A; %250A would show up as literal "%0A" text there. Every
    other character only needs one decode either way, so only the newlines
    differ between the two.
    """
    base = f"https://www.reddit.com/r/{subreddit}/submit?selftext=true"
    title_q = f"&title={quote(title)}" if title else ""
    body_q_single = quote(body)
    body_q_double = body_q_single.replace("%0A", "%250A")

    def _variant(body_q: str) -> tuple[str, bool]:
        full = f"{base}{title_q}&text={body_q}"
        if len(full) <= max_chars:
            return full, True
        return f"{base}{title_q}", False

    return _variant(body_q_double), _variant(body_q_single)


def _tg_code_block(text: str) -> str:
    """MarkdownV2 fenced code block — one-tap copyable and immune to Telegram
    mangling the text as formatting. Inside code entities only '`' and '\\'
    need escaping (Telegram's MarkdownV2 spec)."""
    escaped = text.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{escaped}\n```"


def _hours_since(sqlite_ts: str) -> float:
    """Hours elapsed since a sqlite datetime('now') timestamp (space-separated, UTC)."""
    then = datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.path_of("store_dir"))
        self.chat_id = int(cfg.secret("TG_ALLOWED_CHAT_ID", "0"))
        self.linkedin = LinkedInClient(cfg)
        self.x = XClient(cfg)
        self.busy = asyncio.Lock()
        self.active_draft_id: int | None = None  # last draft you interacted with
        self._pending_updates: list[Update] = []  # buffered fragments of a Telegram-split message
        self._pending_task: asyncio.Task | None = None

    def allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.chat_id

    def backend(self) -> str:
        return str(self.cfg.get("pipeline.backend", "claude") or "claude")

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
        row = self.store.latest_day_session(backend=self.backend())
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
        # a render that failed (safety refusal, quota, timeout...) never reached you
        # for review, so it shouldn't burn your regeneration budget
        used = sum(1 for i in self.store.images_for_draft(draft_id) if i["status"] != "failed")
        if cap > 0 and used >= cap:
            await context.bot.send_message(
                self.chat_id,
                f"{cap} takes on this one already — post it, or Cancel and change the text.")
            return
        if self.busy.locked():
            await context.bot.send_message(self.chat_id, "Still working on the previous message…")
            return

        # resolve everything the workers need while still on the event-loop thread
        session_id = self.store.session_for_day(draft["day"], backend=self.backend())
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

            warning = ""
            if bool(self.cfg.get("image.text_check", True)):
                problem, note = await asyncio.to_thread(check_image_text, self.cfg, img.path)
                retries = int(self.cfg.get("image.text_check_retries", 1))
                attempt = 0
                # a take that gets re-rendered here was never shown to you, so it
                # doesn't burn a slot of max_regenerations the way a real Regenerate
                # tap does
                while problem and attempt < retries:
                    attempt += 1
                    await status.edit_text("🎨 that take had garbled text in it — one more…")
                    try:
                        img = await asyncio.to_thread(
                            generate_image, self.cfg, prompt, out_path=out_path)
                    except ImageGenError as e:
                        log.warning("re-render after text-check failure also failed (%s): %s",
                                   e.reason, e)
                        break
                    problem, note = await asyncio.to_thread(check_image_text, self.cfg, img.path)
                if problem:
                    warning = f"\n\n⚠️ garbled text in the image: {note}" if note else \
                              "\n\n⚠️ garbled text in the image"

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
                    except Exception as e:
                        log.debug("could not clear buttons on stale image message: %s", e)

            await status.delete()
            # photo carries the alt text (short, and worth reviewing); the post text
            # goes in its own message because it routinely exceeds the 1024 caption cap
            with open(img.path, "rb") as fh:
                photo = await context.bot.send_photo(self.chat_id, photo=fh,
                                                     caption=((alt or "") + warning)[:1024])
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

        # lock released — the X rewrite is another slow Claude call and self.busy
        # is not reentrant, so it starts from out here, never from inside the
        # block above (same rule start_image documents for its own callers).
        if self.cfg.get("x.enabled", False) and self.x.configured():
            fresh = self.store.get_draft(draft_id)
            if fresh is not None:
                await self.start_x(context, fresh)
        else:
            await self.maybe_start_reddit(context, draft_id)

    # ---- publish queue ---------------------------------------------------------

    def _next_free_slot(self, after: datetime) -> datetime:
        """The next eligible slot at or after `after` whose day isn't already at
        publish.max_per_slot. Rolls day by day — see plan.md §1, "a second
        approved draft rolls past a taken slot to the next one".
        """
        candidate = after
        for _ in range(8):
            slot = next_slot(self.cfg, candidate)
            if not slot_taken(self.store, slot, self.cfg):
                return slot
            candidate = slot + timedelta(days=1)
        raise ValueError(f"publish.days={self.cfg.get('publish.days')!r} leaves no free slot")

    async def publish_or_queue(self, context: ContextTypes.DEFAULT_TYPE, draft, img=None) -> None:
        """Approve's actual destination: publish immediately if no window is
        configured (today's behaviour) or the current slot is free, otherwise
        queue for the next eligible slot. The only entry point other than
        `postnow` that decides whether `publish()` runs now or later.
        """
        win = parse_window(self.cfg)
        now = datetime.now(timezone.utc)
        if win is None or (in_window(self.cfg, now) and not slot_taken(self.store, now, self.cfg)):
            await self.publish(context, draft, img)
            return

        slot = self._next_free_slot(now)
        self.store.queue_draft(draft["id"], slot.isoformat(), img["id"] if img else None)
        if img is not None:
            self.store.update_image(img["id"], status="scheduled")
        when = slot.astimezone(win.tz).strftime("%a %H:%M %Z")
        await context.bot.send_message(
            self.chat_id, f"⏳ Queued for {when}. It'll go out on its own.",
            reply_markup=queued_keyboard(draft["id"]))

    async def _process_due_queue(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """One tick of the publish-queue worker — see run_publish_queue()."""
        if self.busy.locked():
            return  # try again next tick
        now = datetime.now(timezone.utc)
        max_age_days = int(self.cfg.get("publish.max_age_days", 3))
        cap = int(self.cfg.get("publish.max_per_slot", 1) or 0)
        published = 0
        for draft in self.store.due_drafts(now.isoformat()):
            scheduled = datetime.fromisoformat(draft["scheduled_at"])
            age_days = (now - scheduled).days
            if age_days > max_age_days:
                self.store.expire_draft(draft["id"])
                await context.bot.send_message(
                    self.chat_id,
                    f"⌛ Dropped the queued draft for {draft['day']} — it's {age_days} days "
                    "old and the fact has gone stale.")
                continue
            if cap > 0 and published >= cap:
                # today's cap is already reached (by the publishes just above),
                # so _next_free_slot rolls this one to the next eligible day
                next_dt = self._next_free_slot(now)
                self.store.queue_draft(draft["id"], next_dt.isoformat(),
                                       draft["scheduled_image_id"])
                continue
            img = (self.store.image_by_id(int(draft["scheduled_image_id"]))
                  if draft["scheduled_image_id"] else None)
            await self.publish(context, draft, img)
            published += 1

    async def run_publish_queue(self, bot_api) -> None:
        """Long-running task, started from post_init in main(): the bot process
        is already alive for Telegram polling, so this rides the same event
        loop instead of adding a scheduler dependency (see plan.md §1).
        `bot_api` is a plain telegram.Bot, wrapped so the rest of this class's
        methods can keep using `context.bot` unchanged.
        """
        context = SimpleNamespace(bot=bot_api)
        poll = max(1, int(self.cfg.get("publish.poll_seconds", 60)))
        while True:
            try:
                await self._process_due_queue(context)
            except Exception:
                log.exception("publish queue tick failed")
            await asyncio.sleep(poll)

    # ---- X (Twitter) ---------------------------------------------------------

    async def start_x(self, context: ContextTypes.DEFAULT_TYPE, draft,
                      feedback: str | None = None) -> None:
        """After a successful LinkedIn publish: rewrite the post for X and show it
        for its own approval.

        Acquires self.busy exactly once and never calls converse_turn or publish()
        (the lock is not reentrant). Rewrite/retry re-enter here from a handler,
        after release — same contract as start_image.
        """
        draft_id = draft["id"]
        if draft["status"] != "posted":
            return  # nothing published yet for this draft to base an X post on
        prev = self.store.latest_x(draft_id)
        n = (prev["n"] + 1) if prev else 1
        cap = int(self.cfg.get("x.max_rewrites", 5))
        used = sum(1 for x in self.store.x_for_draft(draft_id) if x["status"] != "failed")
        if cap > 0 and used >= cap:
            await context.bot.send_message(
                self.chat_id, f"{cap} takes on the X version already — post one, or Skip X.")
            return
        if self.busy.locked():
            await context.bot.send_message(
                self.chat_id, "🐦 X version queued — tap when the current message finishes.",
                reply_markup=x_start_keyboard(draft_id))
            return

        session_id = self.store.session_for_day(draft["day"], backend=self.backend())
        prev_text = prev["text"] if prev else None
        post_text = draft["text"]
        limit = int(self.cfg.get("x.max_chars", 280))
        try:
            sub = await asyncio.to_thread(self.x.subscription_type)
            if sub:
                limit = max(limit, int(self.cfg.get("x.premium_max_chars", X_PREMIUM_MAX_CHARS)))
                log.info("X account has subscription %r — using %d-char limit", sub, limit)
        except Exception as e:
            log.warning("X subscription check failed (%s) — keeping %d-char limit", e, limit)

        async with self.busy:
            status = await context.bot.send_message(self.chat_id, "✍️ writing the X version…")
            try:
                text = await asyncio.to_thread(
                    x_rewrite, self.cfg, draft["day"], session_id, post_text,
                    feedback, prev_text, limit)
            except Exception as e:
                log.exception("x rewrite failed")
                self.store.add_x(draft_id, n, feedback=feedback, status="failed", error=str(e))
                await status.edit_text(f"⚠️ X rewrite failed: {e}"[:4000],
                                       reply_markup=x_failed_keyboard(draft_id))
                return

            x_id = self.store.add_x(draft_id, n, text=text, feedback=feedback, status="ready")
            self.active_draft_id = draft_id

            # only the newest take keeps live buttons
            if prev is not None:
                self.store.update_x(prev["id"], status="discarded")
                if prev["tg_message_id"]:
                    try:
                        await context.bot.edit_message_reply_markup(
                            self.chat_id, int(prev["tg_message_id"]), reply_markup=None)
                    except Exception as e:
                        log.debug("could not clear buttons on stale tweet message: %s", e)

            await status.delete()
            sent = await context.bot.send_message(
                self.chat_id,
                (f"{text}\n\n— X take {n} ({len(text)}/{limit} chars). Reply to steer it, "
                 f"or use the buttons.")[:4000],
                reply_markup=x_keyboard(draft_id))
            self.store.update_x(x_id, status="pending_review", tg_message_id=str(sent.message_id))

    async def publish_x(self, context: ContextTypes.DEFAULT_TYPE, draft, xrow) -> None:
        """The only place that publishes to X. Reuses the LinkedIn image if one was
        attached to this draft; a failed media upload still posts the text (see
        XClient.post). Never touches the drafts table — a LinkedIn post that
        already succeeded is unaffected by anything that happens here."""
        draft_id = draft["id"]
        if self.busy.locked():
            await context.bot.send_message(self.chat_id, "Still working on the previous message…")
            return
        async with self.busy:
            img = self.store.latest_image(draft_id, status="attached")
            note = await context.bot.send_message(
                self.chat_id, "📤 posting to X…" if img is None
                else "📤 uploading the image and posting to X…")
            try:
                tweet_id, media_id = await asyncio.to_thread(
                    self.x.post, xrow["text"],
                    Path(img["path"]) if img else None,
                    img["alt_text"] if img else None)
            except Exception as e:
                log.exception("x post failed")
                await note.edit_text(f"⚠️ X post failed: {e}"[:4000],
                                     reply_markup=x_keyboard(draft_id))
                return
            self.store.update_x(xrow["id"], status="posted", tweet_id=tweet_id)
            link = x_tweet_url(tweet_id)
            await note.edit_text(f"✅ Posted to X{' with image' if media_id else ''}. {link}")

        # lock released — Reddit's title call is another slow Claude call and
        # self.busy is not reentrant, so it starts from out here, never from
        # inside the block above (same rule publish() documents for start_x).
        await self.maybe_start_reddit(context, draft_id)

    # ---- Reddit (draft assist) -------------------------------------------------

    async def maybe_start_reddit(self, context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
        if not (self.cfg.get("reddit.enabled", False) and self.cfg.get("reddit.subreddit")):
            return
        fresh = self.store.get_draft(draft_id)
        if fresh is not None and fresh["status"] == "posted" and not fresh["reddit_status"]:
            await self.start_reddit(context, fresh)

    async def _send_reddit_delivery(self, context: ContextTypes.DEFAULT_TYPE, draft_id: int,
                                    subreddit: str, title: str, body: str) -> None:
        """The three delivery messages — link+buttons, title, body — in that
        order, chosen for one-thumb use on a phone. Title/body ship in their own
        MarkdownV2 code blocks so Telegram makes them one-tap copyable and never
        mangles the text as formatting."""
        max_chars = int(self.cfg.get("reddit.max_link_chars", 4000))
        (app_link, app_included), (browser_link, browser_included) = reddit_submit_link(
            subreddit, title, body, max_chars)
        head = f"📮 Reddit — r/{subreddit}"
        if not title:
            head += "\n\n⚠️ title generation failed — type one on the form."
        if not (app_included and browser_included):
            head += "\n\n⚠️ body too long for the link — copy it from below."
        sent = await context.bot.send_message(
            self.chat_id, head[:4000],
            reply_markup=reddit_keyboard(draft_id, app_link, browser_link))
        self.store.set_reddit(draft_id, reddit_tg_message_id=str(sent.message_id))
        if title:
            await context.bot.send_message(self.chat_id, _tg_code_block(title)[:4000],
                                           parse_mode="MarkdownV2")
        await context.bot.send_message(self.chat_id, _tg_code_block(body)[:4000],
                                       parse_mode="MarkdownV2")

    async def start_reddit(self, context: ContextTypes.DEFAULT_TYPE, draft,
                           force: bool = False) -> None:
        """After the X step resolves (posts, is skipped, or is disabled): hand
        you a prefilled Reddit submit link plus a copy block for the LinkedIn
        text reused verbatim (see pipeline.draft.reddit_body and plan.md §2).
        Nothing is submitted by code — see plan.md §1 for why this step is
        assisted, not automated.

        Acquires self.busy exactly once and never calls converse_turn/publish/
        publish_x (the lock is not reentrant). "New title" and /reddit both
        re-enter here from a handler, after release — same contract as
        start_image/start_x.
        """
        draft_id = draft["id"]
        if draft["status"] != "posted":
            return  # nothing published yet for this draft to base a Reddit post on
        if draft["reddit_status"] not in (None, "pending") and not force:
            return  # idempotent — a double-tap or a stale /reddit can't duplicate
        subreddit = str(self.cfg.get("reddit.subreddit", "buildinpublic"))

        if not force:
            last = self.store.last_posted_reddit()
            min_hours = int(self.cfg.get("reddit.min_hours_between_posts", 48))
            if last is not None and min_hours > 0:
                age_h = _hours_since(last["updated_at"])
                if age_h < min_hours:
                    await context.bot.send_message(
                        self.chat_id,
                        f"Last Reddit post was {age_h:.0f}h ago; r/{subreddit} gets touchy "
                        f"about daily self-promo. /reddit forces it.")
                    return

        if self.busy.locked():
            await context.bot.send_message(
                self.chat_id, "📮 Reddit step queued — tap when the current message finishes.",
                reply_markup=reddit_start_keyboard(draft_id))
            return

        session_id = self.store.session_for_day(draft["day"], backend=self.backend())
        post_text = draft["text"]
        title_max = int(self.cfg.get("reddit.title_max", 300))
        prev_msg_id = draft["reddit_tg_message_id"]

        async with self.busy:
            status = await context.bot.send_message(self.chat_id, "📮 writing the Reddit title…")
            try:
                title = await asyncio.to_thread(
                    reddit_title, self.cfg, draft["day"], session_id, post_text,
                    subreddit, title_max)
            except Exception as e:
                log.warning("reddit title failed: %s", e)
                title = ""  # not fatal — you type it on the form instead

            body = reddit_body(post_text)
            self.store.set_reddit(draft_id, reddit_status="pending", reddit_title=title)
            self.active_draft_id = draft_id

            # only the newest delivery message keeps live buttons
            if prev_msg_id:
                try:
                    await context.bot.edit_message_reply_markup(
                        self.chat_id, int(prev_msg_id), reply_markup=None)
                except Exception as e:
                    log.debug("could not clear buttons on stale reddit delivery message: %s", e)

            await status.delete()
            await self._send_reddit_delivery(context, draft_id, subreddit, title, body)

    # ---- X reply radar ---------------------------------------------------------

    @staticmethod
    def _radar_live_post(row) -> dict:
        """Rebuild the fixture-shaped post dict cli.py/reply.py expect from a
        stored radar_posts row."""
        return {"id": row["post_id"], "author_handle": row["author_handle"],
                "text": row["text"], "created_at": row["created_at"],
                "views": row["views"], "author_id": row["author_id"]}

    def _process_radar_answer(self, post: dict, question: str, answer: str) -> dict:
        """Runs radar_pipeline.process_answer on a throwaway Store bound to
        this worker thread — same "own Store: sqlite per-thread" rule
        radar/scheduler.py follows, since process_answer mixes DB reads/writes
        with the slow LLM call and self.store is bound to the event-loop
        thread (see module docstring)."""
        store = Store(self.cfg.path_of("store_dir"))
        return radar_pipeline.process_answer(post, question, answer, self.cfg, store)

    async def deliver_radar_result(self, tg_bot, post: dict, result: dict) -> None:
        """Sends the Telegram card for an 'ask' or 'draft' decision. Takes a
        bare telegram.Bot rather than a full Context, so the radar scheduler's
        background thread can call this the same way a handler does — via
        asyncio.run_coroutine_threadsafe against the loop captured in
        Application.post_init (see start_radar below)."""
        post_id = str(post["id"])
        decision = result["decision"]
        if decision == "ask":
            await tg_bot.send_message(
                self.chat_id, radar_ask_text(post, result["question"])[:4000],
                reply_markup=radar_ask_keyboard(post_id))
        elif decision == "draft":
            post_url = x_tweet_url(post_id)
            text = radar_draft_text(post, result["reply"], result.get("reason", ""))[:4000]
            sent = await tg_bot.send_message(
                self.chat_id, text,
                reply_markup=radar_keyboard(post_id, post_url, result["reply"]))
            self.store.update_radar_reply(result["reply_id"], tg_message_id=str(sent.message_id))

    async def deliver_radar_growth_result(self, tg_bot, proposal) -> None:
        """Sends the "add?"/"replace?" watchlist prompt (plan.md §2). Same
        bare-Bot signature as deliver_radar_result so notify() (below) can
        treat every radar Telegram surface uniformly."""
        candidate = proposal.candidate
        author_id = candidate["author_id"]
        if proposal.kind == "add":
            text = radar_watchlist_add_text(candidate, proposal.watchlist_size,
                                            proposal.watchlist_cap)
            await tg_bot.send_message(self.chat_id, text[:4000],
                                       reply_markup=radar_watchlist_add_keyboard(author_id))
        else:
            text = radar_watchlist_swap_text(candidate, proposal.incumbent)
            await tg_bot.send_message(self.chat_id, text[:4000],
                                       reply_markup=radar_watchlist_swap_keyboard(author_id))

    async def redo_radar_reply(self, tg_bot, row) -> None:
        """'Redo' — a fresh take from a fresh retrieval pass, same one-row-
        per-take shape as start_x/start_reddit's redo paths."""
        post = self._radar_live_post(row)
        prev = self.store.latest_radar_reply(row["post_id"])
        n = (prev["n"] + 1) if prev else 1
        status = await tg_bot.send_message(self.chat_id, "🔁 redrafting…")
        # retrieval is a cheap in-process DB read, so it stays on the event-loop
        # thread (self.store is thread-bound — see module docstring); only the
        # slow LLM call below is worth offloading
        matches = radar_retrieve.retrieve(self.store, post["text"], 8)
        r = await asyncio.to_thread(radar_reply.draft_reply, self.cfg, post, matches)
        await status.delete()
        if r.status != "ready":
            await tg_bot.send_message(self.chat_id, f"⚠️ redraft failed: {r.error}"[:4000])
            return
        reply_id = self.store.add_radar_reply(
            row["post_id"], n=n, text=r.text, status="ready", source="claude",
            evidence_json=json.dumps([m["summary"] for m in matches], ensure_ascii=False))
        if prev is not None and prev["tg_message_id"]:
            try:
                await tg_bot.edit_message_reply_markup(
                    self.chat_id, int(prev["tg_message_id"]), reply_markup=None)
            except Exception as e:
                log.debug("could not clear buttons on stale radar reply message: %s", e)
        self.store.set_radar_post_state(row["post_id"], "suggested")
        await self.deliver_radar_result(
            tg_bot, post,
            {"decision": "draft", "reply": r.text, "reply_id": reply_id,
             "reason": row["score_reason"] or ""})

    async def on_radar_growth_callback(self, tg_bot, q, action: str, author_id: str) -> None:
        row = self.store.get_radar_author(author_id)
        if row is None:
            await q.edit_message_reply_markup(None)
            return

        if action == "radadd":
            radar_watchlist.add_to_watchlist(self.store, author_id)
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, f"➕ Added @{row['handle']} to the watchlist.")

        elif action == "radswap":
            # Recompute the weakest incumbent at tap-time rather than trusting
            # the prompt's stale snapshot — the watchlist may have changed
            # since it was sent (plan.md §2's hysteresis/grace guards gate
            # *proposing* a swap, not a user's own confirmed tap).
            await q.edit_message_reply_markup(None)
            current = self.store.radar_watchlist_authors()
            cap = radar_watchlist.watchlist_max(self.cfg)
            if len(current) < cap:
                radar_watchlist.add_to_watchlist(self.store, author_id)
                await tg_bot.send_message(self.chat_id,
                                          f"➕ Added @{row['handle']} to the watchlist.")
                return
            incumbents = sorted(current, key=radar_watchlist.strength)
            if not incumbents:
                await tg_bot.send_message(self.chat_id, "Nothing to swap out.")
                return
            weakest = incumbents[0]
            radar_watchlist.remove_from_watchlist(self.store, weakest["author_id"])
            radar_watchlist.add_to_watchlist(self.store, author_id)
            await tg_bot.send_message(
                self.chat_id, f"🔁 Swapped @{weakest['handle']} out for @{row['handle']}.")

        elif action == "radnever":
            radar_watchlist.never(self.store, author_id)
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, f"🚫 Won't suggest @{row['handle']} again.")

        elif action in ("radnotnow", "radkeep"):
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, "👍 Left as-is.")

    async def on_radar_callback(self, tg_bot, q, action: str, post_id: str) -> None:
        if action in ("radadd", "radswap", "radnever", "radnotnow", "radkeep"):
            await self.on_radar_growth_callback(tg_bot, q, action, post_id)
            return

        row = self.store.get_radar_post(post_id)
        if row is None:
            await q.edit_message_reply_markup(None)
            return

        if action == "radreplied":
            rep = self.store.latest_radar_reply(post_id)
            if rep is not None:
                self.store.update_radar_reply(rep["id"], status="replied")
            self.store.set_radar_post_state(post_id, "replied")
            if row["author_id"]:
                self.store.mark_radar_author_replied(row["author_id"])
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, "✅ Marked replied.")

        elif action == "radredo":
            await q.edit_message_reply_markup(None)
            await self.redo_radar_reply(tg_bot, row)

        elif action == "radedit":
            prev = self.store.editing_radar_reply()
            if prev is not None and prev["post_id"] != post_id:
                self.store.update_radar_reply(prev["id"], status="ready")
            rep = self.store.latest_radar_reply(post_id)
            if rep is None:
                await tg_bot.send_message(self.chat_id, "No radar reply to edit yet.")
                return
            self.store.update_radar_reply(rep["id"], status="editing")
            await tg_bot.send_message(
                self.chat_id,
                "✏️ Send the exact replacement reply as your next message "
                "(it will be used verbatim, the model won't see it).")

        elif action == "radskip":
            rep = self.store.latest_radar_reply(post_id)
            if rep is not None:
                self.store.update_radar_reply(rep["id"], status="skipped")
            self.store.set_radar_post_state(post_id, "skipped")
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, "⏭ Skipped.")

        elif action == "radqskip":
            self.store.set_radar_post_state(post_id, "skipped", pending_question=None)
            await q.edit_message_reply_markup(None)
            await tg_bot.send_message(self.chat_id, "⏭ Skipped.")

    # ---- handlers ------------------------------------------------------------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        q = update.callback_query
        await q.answer()
        action, _, draft_id_s = q.data.partition(":")
        if action.startswith("rad"):
            await self.on_radar_callback(context.bot, q, action, draft_id_s)
            return
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
                await self.publish_or_queue(context, draft)

        elif action == "postimg":
            img = self.store.latest_image(draft_id, status="pending_review")
            if img is None or not img["path"]:
                await context.bot.send_message(self.chat_id, "No image is waiting on that draft.")
                return
            if draft["status"] == "posted":
                await q.edit_message_reply_markup(None)
                return
            await q.edit_message_reply_markup(None)
            await self.publish_or_queue(context, draft, img)

        elif action == "posttxt":
            if draft["status"] == "posted":
                await q.edit_message_reply_markup(None)
                return
            await q.edit_message_reply_markup(None)
            img = self.store.latest_image(draft_id, status="pending_review")
            if img is not None:
                self.store.update_image(img["id"], status="discarded")
            await self.publish_or_queue(context, draft)

        elif action == "postnow":
            if draft["status"] != "queued":
                await q.edit_message_reply_markup(None)
                return
            await q.edit_message_reply_markup(None)
            img = (self.store.image_by_id(int(draft["scheduled_image_id"]))
                  if draft["scheduled_image_id"] else None)
            await self.publish(context, draft, img)

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

        elif action == "xpost":
            xrow = self.store.latest_x(draft_id, status="pending_review")
            if xrow is None:
                await context.bot.send_message(
                    self.chat_id, "No X candidate is waiting on that draft.")
                return
            await q.edit_message_reply_markup(None)
            await self.publish_x(context, draft, xrow)

        elif action == "xredo":
            await q.edit_message_reply_markup(None)
            await self.start_x(context, draft)

        elif action == "xedit":
            prev = self.store.editing_x(int(self.cfg.get("x.pending_hours", 12)))
            if prev is not None and prev["draft_id"] != draft_id:
                self.store.update_x(prev["id"], status="pending_review")
            xrow = self.store.latest_x(draft_id)
            if xrow is None:
                await context.bot.send_message(self.chat_id, "No X candidate to edit yet.")
                return
            self.store.update_x(xrow["id"], status="editing")
            await context.bot.send_message(
                self.chat_id,
                "✏️ Send the exact X replacement text as your next message "
                "(it will be used verbatim, the model won't see it).")

        elif action == "xskip":
            xrow = self.store.latest_x(draft_id)
            if xrow is not None:
                self.store.update_x(xrow["id"], status="discarded")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(self.chat_id, "⏭ X skipped.")
            await self.maybe_start_reddit(context, draft_id)

        elif action == "xstart":
            await q.edit_message_reply_markup(None)
            await self.start_x(context, draft)

        elif action == "rpost":
            if draft["reddit_status"] != "pending":
                await context.bot.send_message(
                    self.chat_id, "No Reddit link is waiting on that draft.")
                return
            await q.edit_message_reply_markup(None)
            subreddit = str(self.cfg.get("reddit.subreddit", "buildinpublic"))
            self.store.set_reddit(draft_id, reddit_status="posted")
            await context.bot.send_message(
                self.chat_id,
                f"✅ Marked posted to r/{subreddit}. Reply here with the URL if you'd "
                "like it recorded (optional).")

        elif action == "rredo":
            await q.edit_message_reply_markup(None)
            await self.start_reddit(context, draft, force=True)

        elif action == "redit":
            prev = self.store.editing_reddit()
            if prev is not None and prev["id"] != draft_id:
                self.store.set_reddit(prev["id"], reddit_status="pending")
            self.store.set_reddit(draft_id, reddit_status="editing")
            await context.bot.send_message(
                self.chat_id,
                "✏️ Send the exact replacement title as your next message "
                "(it will be used verbatim, the model won't see it).")

        elif action == "rskip":
            self.store.set_reddit(draft_id, reddit_status="skipped")
            await q.edit_message_reply_markup(None)
            await context.bot.send_message(self.chat_id, "⏭ Reddit skipped.")

        elif action == "rstart":
            await q.edit_message_reply_markup(None)
            await self.start_reddit(context, draft)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update) or not update.message or not update.message.text:
            return
        self._pending_updates.append(update)
        if self._pending_task is not None:
            self._pending_task.cancel()
        self._pending_task = asyncio.create_task(self._debounced_dispatch(context))

    async def _debounced_dispatch(self, context: ContextTypes.DEFAULT_TYPE):
        """Fires MESSAGE_DEBOUNCE_SECONDS after the last fragment in a burst; a
        new fragment arriving before then cancels and restarts this wait."""
        try:
            await asyncio.sleep(MESSAGE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        updates, self._pending_updates = self._pending_updates, []
        self._pending_task = None
        text = "".join(u.message.text for u in updates).strip()
        # a reply-to only lands on the fragment Telegram sent first
        rep_update = next((u for u in updates if u.message.reply_to_message is not None),
                          updates[-1])
        await self._process_message(rep_update, context, text)

    async def _process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                               text: str):
        # explicit verbatim-replace mode for a radar reply (set by "Edit") — checked
        # first since the radar lane is independent of the LinkedIn pipeline below
        redit_reply = self.store.editing_radar_reply()
        if redit_reply is not None:
            self.store.update_radar_reply(
                redit_reply["id"], text=text, source="manual", status="ready")
            post_url = x_tweet_url(redit_reply["post_id"])
            await update.message.reply_text(
                f"Updated reply:\n\n{text}"[:4000],
                reply_markup=radar_keyboard(redit_reply["post_id"], post_url, text))
            return

        # a radar question is waiting on your free-text answer (plan.md §6 step 2b)
        awaiting = self.store.radar_post_awaiting_answer()
        if awaiting is not None:
            post = self._radar_live_post(awaiting)
            result = await asyncio.to_thread(
                self._process_radar_answer, post, awaiting["pending_question"], text)
            if result["decision"] == "draft":
                await self.deliver_radar_result(context.bot, post, result)
            elif result["decision"] == "saved_stale":
                await update.message.reply_text(
                    "Noted — thanks, but that post's window already closed.")
            else:
                await update.message.reply_text(
                    f"⚠️ {result.get('reason', 'could not draft a reply')}"[:4000])
            return

        # explicit verbatim-replace mode for the LinkedIn draft (set by "Replace text")
        draft = self.store.latest_editing_draft()
        if draft is not None:
            self.store.update_draft(draft["id"], text=text, status="pending")
            await update.message.reply_text(
                f"Updated draft:\n\n{text}"[:4000], reply_markup=draft_keyboard(draft["id"]))
            return

        # explicit verbatim-replace mode for the X candidate (set by "Replace text")
        xedit = self.store.editing_x(int(self.cfg.get("x.pending_hours", 12)))
        if xedit is not None:
            self.store.update_x(xedit["id"], text=text, source="manual", status="pending_review")
            await update.message.reply_text(
                f"Updated X take ({len(text)} chars):\n\n{text}"[:4000],
                reply_markup=x_keyboard(xedit["draft_id"]))
            return

        # an image is on the table → free text steers the next take
        img = self.store.pending_image(int(self.cfg.get("image.pending_hours", 12)))
        if img is not None:
            d = self.store.get_draft(img["draft_id"])
            if d is not None and d["status"] == "imaging":
                await self.start_image(context, d, feedback=text)
                return

        # an X candidate is on the table → free text steers the next rewrite
        xrow = self.store.pending_x(int(self.cfg.get("x.pending_hours", 12)))
        if xrow is not None:
            d = self.store.get_draft(xrow["draft_id"])
            if d is not None and d["status"] == "posted":
                await self.start_x(context, d, feedback=text)
                return

        # explicit verbatim-replace mode for the Reddit title (set by "Edit title").
        # Safe alongside the X checks above: once the Reddit step is live the X
        # row is posted/discarded/failed, so it no longer matches pending_x's
        # status='pending_review' filter.
        redit = self.store.editing_reddit()
        if redit is not None:
            title = text.strip()
            title_max = int(self.cfg.get("reddit.title_max", 300))
            if len(title) > title_max:
                await update.message.reply_text(
                    f"That's {len(title)} chars — Reddit's title cap is {title_max}.")
                return
            subreddit = str(self.cfg.get("reddit.subreddit", "buildinpublic"))
            body = reddit_body(redit["text"])
            prev_msg_id = redit["reddit_tg_message_id"]
            if prev_msg_id:
                try:
                    await context.bot.edit_message_reply_markup(
                        self.chat_id, int(prev_msg_id), reply_markup=None)
                except Exception as e:
                    log.debug("could not clear buttons on stale reddit edit message: %s", e)
            self.store.set_reddit(redit["id"], reddit_status="pending", reddit_title=title)
            await self._send_reddit_delivery(context, redit["id"], subreddit, title, body)
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
        x_status = ("disabled" if not self.cfg.get("x.enabled", False)
                   else "ready" if self.x.configured() else "enabled but keys missing")
        reddit_status = ("disabled" if not self.cfg.get("reddit.enabled", False)
                         else f"r/{self.cfg.get('reddit.subreddit', 'buildinpublic')}")
        lines = [
            f"Social Daily Poster alive. Items last 24h: {row['c']}. "
            f"LinkedIn token: {'n/a' if days is None else f'{days}d left'}. "
            f"X: {x_status}. "
            f"Reddit: {reddit_status}. "
            f"Session day: {sess['day'] if sess else 'none'}."
        ]
        # A microphone whose state you can't see is a microphone you shouldn't
        # trust, so capture reports here even when it's off.
        from ..capture import capture_status_lines
        lines += capture_status_lines(self.cfg)
        queued = self.store.queued_drafts()
        if queued:
            win = parse_window(self.cfg)
            tz = win.tz if win else timezone.utc
            lines.append("Queued:")
            for d in queued:
                when = datetime.fromisoformat(d["scheduled_at"]).astimezone(tz)
                lines.append(f"  #{d['id']} {d['day']} → {when.strftime('%a %H:%M %Z')}")
        await update.message.reply_text("\n".join(lines))

    async def on_x(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/x — recover the X step for a draft that published to LinkedIn but never
        got an X candidate started (e.g. the bot restarted in between)."""
        if not self.allowed(update):
            return
        if not (self.cfg.get("x.enabled", False) and self.x.configured()):
            await update.message.reply_text("X posting is not enabled/configured.")
            return
        draft = self.store.draft_awaiting_x(int(self.cfg.get("x.pending_hours", 12)))
        if draft is None:
            await update.message.reply_text("Nothing waiting for an X version right now.")
            return
        self.active_draft_id = draft["id"]
        await self.start_x(context, draft)

    async def on_reddit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/reddit — recover the Reddit step for a draft that published to
        LinkedIn but never got one started (e.g. the bot restarted in between),
        or force it past the cadence nudge."""
        if not self.allowed(update):
            return
        if not (self.cfg.get("reddit.enabled", False) and self.cfg.get("reddit.subreddit")):
            await update.message.reply_text("Reddit posting is not enabled/configured.")
            return
        draft = self.store.draft_awaiting_reddit()
        if draft is None:
            await update.message.reply_text("Nothing waiting for a Reddit step right now.")
            return
        self.active_draft_id = draft["id"]
        await self.start_reddit(context, draft, force=True)

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
            "use /talk to go back to discussing the words.\n\n"
            "If a publish window is configured, posting with image/text-only doesn't send "
            "it right away either — it queues for the next eligible slot and tells you when. "
            "Post now anyway sends it immediately instead. /status lists what's queued.\n\n"
            "If X posting is enabled, once a post reaches LinkedIn I write a separate, "
            "shorter X-native rewrite and send it with its own buttons — nothing reaches "
            "X until you tap Post to X, and skipping or failing that never touches the "
            "LinkedIn post already made. /x recovers that step if I ever miss it.\n\n"
            "If Reddit posting is enabled, once the X step resolves (posts, is skipped, "
            "or is disabled) you get a prefilled Reddit submit link plus a copy block — "
            "the LinkedIn text reused verbatim (hashtags stripped) with a short generated "
            "title, since Reddit needs one and LinkedIn posts don't have one. Nothing is "
            "ever submitted by me; you tap Submit yourself in your browser. /reddit "
            "recovers that step if I ever miss it.\n\n"
            "If the X reply radar is enabled, a fast-travelling post from your watchlist "
            "shows up here with a drafted reply — Copy reply puts it on your clipboard, "
            "Open post takes you to it, and you paste and send by hand. It never posts, "
            "likes, follows, or opens anything for you.\n"
            "/status shows health.")


def start_radar(cfg: Config, bot: Bot, tg_bot, loop: asyncio.AbstractEventLoop) -> None:
    """Bridges the radar's background threads (Lane B's poller, Lane A's
    local API) to Telegram.

    PTB's Application only accepts bot API calls made from its own event
    loop, so both threads hand their notify() calls to that loop with
    run_coroutine_threadsafe instead of awaiting them directly. Called from
    Application.post_init, the first point at which that loop exists.
    radar_scheduler.start()/localapi.start() no-op on their own when
    radar.enabled is false, so this is safe to call unconditionally.
    """
    from ..radar import scheduler as radar_scheduler
    from . import localapi

    def notify(post, result) -> None:
        async def _send() -> None:
            try:
                if result["decision"] == "budget_warning":
                    await tg_bot.send_message(bot.chat_id, result["message"])
                elif result["decision"] in ("watchlist_add", "watchlist_replace"):
                    await bot.deliver_radar_growth_result(tg_bot, result["proposal"])
                else:
                    await bot.deliver_radar_result(tg_bot, post, result)
            except Exception:
                log.exception("radar notify failed")
        asyncio.run_coroutine_threadsafe(_send(), loop)

    radar_scheduler.start(cfg, notify=notify)
    localapi.start(cfg, notify=notify)


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


async def _post_init(application: Application) -> None:
    """Starts the publish-queue worker and the radar bridge on the same event
    loop PTB polls on — see Bot.run_publish_queue for why the publish queue is
    a plain asyncio task rather than PTB's JobQueue (that needs an extra we
    don't depend on), and start_radar for why radar needs this loop handle."""
    bot: Bot = application.bot_data["bot"]
    cfg: Config = application.bot_data["cfg"]
    asyncio.create_task(bot.run_publish_queue(application.bot))
    start_radar(cfg, bot, application.bot, asyncio.get_running_loop())


def main() -> None:
    cfg = Config.load()
    token = cfg.secret("TG_BOT_TOKEN")
    if not token or not cfg.secret("TG_ALLOWED_CHAT_ID"):
        raise SystemExit("TG_BOT_TOKEN / TG_ALLOWED_CHAT_ID not set in .env")

    start_waha_webhook(cfg)
    if cfg.get("mode", "server") == "laptop":
        # Server installs are scheduled by cron; a laptop needs a scheduler
        # that survives sleep, so it lives in this already-running process.
        from .scheduler import start as start_scheduler
        start_scheduler(cfg)
        # In server mode these run on the laptop as logon tasks and push over
        # SSH. In laptop mode there is no separate laptop, so they run here —
        # which means capture stops when this process stops.
        if cfg.get("audio.enabled", False):
            from ..capture.audio import start as start_audio
            start_audio(cfg)
        if cfg.get("activity.enabled", False):
            from ..capture.activity import start as start_activity
            start_activity(cfg)

    bot = Bot(cfg)
    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["bot"] = bot
    app.bot_data["cfg"] = cfg
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(CommandHandler("status", bot.on_status))
    app.add_handler(CommandHandler("help", bot.on_help))
    app.add_handler(CommandHandler("talk", bot.on_talk))
    app.add_handler(CommandHandler("x", bot.on_x))
    app.add_handler(CommandHandler("reddit", bot.on_reddit))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_message))
    log.info("bot polling started")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
