"""Draft writer: digest → claude -p → draft row → Telegram delivery."""
from __future__ import annotations

import json
from pathlib import Path

import requests

from .claude_cli import extract_json, run_claude
from ..util import get_logger

log = get_logger("pipeline.draft")

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _prompt_file(cfg, key: str, default_name: str) -> Path:
    """Instance override (config path, survives deploys) or the repo default."""
    override = cfg.path_of(f"pipeline.{key}")
    return override if override and override.exists() else PROMPTS_DIR / default_name


def write_draft(cfg, store, day: str, digest: str) -> int | None:
    """Returns draft id, or None if nothing post-worthy."""
    style = _prompt_file(cfg, "style_guide", "style-guide.md").read_text(encoding="utf-8")
    task = _prompt_file(cfg, "draft_prompt", "draft-prompt.md").read_text(encoding="utf-8")
    task = task.replace("{LANGUAGE_OUT}", str(cfg.get("pipeline.language_out", "English")))
    prompt = f"{style}\n\n{task}\n\n# Digest\n\n{digest}"

    result = run_claude(cfg, prompt, timeout=900)
    data = extract_json(result)
    post_text = (data.get("post_text") or "").strip()
    if not post_text:
        log.info("model found nothing post-worthy for %s", day)
        return None
    draft_id = store.add_draft(
        day=day,
        text=post_text,
        rationale=data.get("story_rationale"),
        alternates=data.get("alternates") or [],
    )
    log.info("draft %s created for %s", draft_id, day)
    return draft_id


# ---- Telegram delivery (plain Bot API; callbacks handled by the bot service) ----

def tg_api(cfg, method: str, **params):
    token = cfg.secret("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN not set")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def deliver_draft(cfg, store, draft_id: int) -> None:
    draft = store.get_draft(draft_id)
    chat_id = cfg.secret("TG_ALLOWED_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TG_ALLOWED_CHAT_ID not set")
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve & post", "callback_data": f"approve:{draft_id}"},
            {"text": "✏️ Edit", "callback_data": f"edit:{draft_id}"},
            {"text": "⏭ Skip", "callback_data": f"skip:{draft_id}"},
        ]]
    }
    text = f"📝 LinkedIn draft for {draft['day']}\n\n{draft['text']}"
    if draft["rationale"]:
        text += f"\n\n— story: {draft['rationale']}"
    msg = tg_api(cfg, "sendMessage", chat_id=chat_id, text=text[:4000], reply_markup=keyboard)
    store.update_draft(draft_id, tg_message_id=str(msg["message_id"]))
    alternates = json.loads(draft["alternates_json"]) if draft["alternates_json"] else []
    for i, alt in enumerate(alternates[:2], 1):
        tg_api(cfg, "sendMessage", chat_id=chat_id, text=f"(alternate {i})\n\n{alt}"[:4000])


def notify(cfg, text: str) -> None:
    """Plain informational message to the owner."""
    chat_id = cfg.secret("TG_ALLOWED_CHAT_ID")
    if chat_id and cfg.secret("TG_BOT_TOKEN"):
        try:
            tg_api(cfg, "sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log.warning("notify failed: %s", e)
