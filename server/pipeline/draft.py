"""Draft writer: digest → claude -p → draft row → Telegram delivery."""
from __future__ import annotations

import json
import uuid
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


FOLLOW_UP_PROMPT = """The user is reviewing the LinkedIn draft you wrote for {day}.
Their message:
\"\"\"
{message}
\"\"\"

The full day digest is already in this conversation. If they ask about something
not covered there, you may Read/Grep/Glob the raw source files under {files_dir}
to dig deeper — those are the unabridged transcripts and messages behind the digest.

Do what they ask. If they want a different story, pick a genuinely different one
that still passes all three gates from your instructions. Any post you write must
obey the style guide from the start of this conversation, including the AI-tells pass.

Return ONLY a JSON object, no other text:
{{"reply": "one or two sentences to the user: what you did, found, or need from them",
  "post_text": "the full new/revised post, or null if you didn't produce one"}}
"""


def converse(cfg, store, day: str, session_id: str, message: str,
             current_post: str | None = None) -> tuple[str, str | None]:
    """Continue the day's session with a user message.

    Returns (reply_to_user, new_post_text_or_None). The session carries the whole
    digest and every previous turn, so this is a real conversation, not a one-shot.
    `current_post` names which of the day's drafts is under discussion.
    """
    files_dir = Path(store.files_dir) / day
    prompt = FOLLOW_UP_PROMPT.format(day=day, message=message, files_dir=files_dir)
    if current_post:
        prompt += f"\n\nThe post currently under discussion is:\n\"\"\"\n{current_post}\n\"\"\"\n"
    result = run_claude(
        cfg, prompt, session_id=session_id, resume=True, timeout=900,
        allow_read_dirs=[str(files_dir)] if files_dir.exists() else None,
        allowed_tools="Read,Grep,Glob",
    )
    try:
        data = extract_json(result)
    except ValueError:
        return result.strip()[:2000], None
    post = (data.get("post_text") or "").strip() or None
    return (data.get("reply") or "").strip() or "(no reply)", post


IMAGE_BRIEF_PROMPT = """You wrote a LinkedIn post for {day} and the user has approved the
text. Now brief an illustration for it.

The post:
\"\"\"
{post_text}
\"\"\"
{extra}
You know the story behind this post, not just its words — brief the *story*, one
single visual idea. Rules for the prompt you write:
- Describe a scene or composition, concretely. No slide layouts, no infographics.
- No text, letters, numbers, logos or watermarks anywhere in the image; image
  models render them badly and LinkedIn readers notice.
- No recognisable real people, no company branding.
- No literal screenshots of code, dashboards or chat windows.
- Write it in this house style, and open the prompt by naming that style — a
  detailed scene description otherwise overrides a style note tacked on the end:
  {style}

Return ONLY a JSON object, no other text:
{{"image_prompt": "the full prompt for the image model, 1-3 sentences",
  "alt_text": "one plain sentence describing the finished image, for screen readers"}}
"""

IMAGE_BRIEF_REVISION = """
Your previous prompt was:
\"\"\"
{prev_prompt}
\"\"\"
The user's feedback on the image it produced:
\"\"\"
{feedback}
\"\"\"
Revise that prompt to address the feedback. Keep everything they didn't complain about.
"""


def image_brief(cfg, day: str, session_id: str | None, post_text: str,
                feedback: str | None = None,
                prev_prompt: str | None = None) -> tuple[str, str]:
    """Ask for an image prompt + alt text for an approved post.

    Resumes the day's session when there is one, so the brief comes from the day's
    material rather than a re-reading of the post's wording. Falls back to a
    one-shot call when the session is gone — which happens whenever you approve a
    draft older than the CLI's session retention.

    Takes plain values, no Store: safe to call from asyncio.to_thread.
    """
    extra = ""
    if feedback:
        extra = IMAGE_BRIEF_REVISION.format(prev_prompt=prev_prompt or "(not recorded)",
                                            feedback=feedback)
    style = str(cfg.get("image.style_suffix", "") or "").strip() or "no particular house style"
    prompt = IMAGE_BRIEF_PROMPT.format(day=day, post_text=post_text, extra=extra, style=style)

    if session_id:
        try:
            result = run_claude(cfg, prompt, session_id=session_id, resume=True, timeout=300)
        except Exception as e:
            log.warning("image brief: session %s unusable (%s) — falling back", session_id, e)
            result = run_claude(cfg, prompt, timeout=300)
    else:
        result = run_claude(cfg, prompt, timeout=300)

    data = extract_json(result)
    image_prompt = (data.get("image_prompt") or "").strip()
    if not image_prompt:
        raise ValueError(f"no image_prompt in response: {result[:300]}")
    alt = (data.get("alt_text") or "").strip() or "Illustration for this post"
    return image_prompt, alt


def write_drafts(cfg, store, day: str, digest: str) -> tuple[list[int], list[dict]]:
    """Draft one post per interesting fact found in the day.

    Returns (draft_ids best-first, rejected candidates). Runs inside a named
    session so the bot can keep talking about this day afterwards.
    """
    style = _prompt_file(cfg, "style_guide", "style-guide.md").read_text(encoding="utf-8")
    task = _prompt_file(cfg, "draft_prompt", "draft-prompt.md").read_text(encoding="utf-8")
    task = (task.replace("{LANGUAGE_OUT}", str(cfg.get("pipeline.language_out", "English")))
                .replace("{MAX_DRAFTS}", str(cfg.get("pipeline.max_drafts", 4))))
    prompt = f"{style}\n\n{task}\n\n# Digest\n\n{digest}"

    session_id = str(uuid.uuid4())
    result = run_claude(cfg, prompt, timeout=1800, session_id=session_id)
    store.set_day_session(day, session_id)
    data = extract_json(result)

    rejected = data.get("rejected") or []
    ids = []
    for cand in (data.get("candidates") or [])[:int(cfg.get("pipeline.max_drafts", 4))]:
        text = (cand.get("post_text") or "").strip()
        if not text:
            continue
        ids.append(store.add_draft(
            day=day, text=text,
            rationale=cand.get("why") or cand.get("fact"),
        ))
    log.info("%d draft(s) created for %s (%d rejected)", len(ids), day, len(rejected))
    return ids, rejected


# ---- Telegram delivery (plain Bot API; callbacks handled by the bot service) ----
# tg_api posts JSON, so it can only carry a file_id or a public URL for a photo.
# Sending image bytes needs a multipart request (requests' files=) — the bot does
# that with PTB's send_photo instead; add a branch here if the nightly ever needs it.

def tg_api(cfg, method: str, **params):
    token = cfg.secret("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN not set")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def deliver_draft(cfg, store, draft_id: int, label: str = "") -> None:
    draft = store.get_draft(draft_id)
    chat_id = cfg.secret("TG_ALLOWED_CHAT_ID")
    if not chat_id:
        raise RuntimeError("TG_ALLOWED_CHAT_ID not set")
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ Approve & post", "callback_data": f"approve:{draft_id}"},
             {"text": "🔁 Another angle", "callback_data": f"another:{draft_id}"}],
            [{"text": "✏️ Replace text", "callback_data": f"edit:{draft_id}"},
             {"text": "⏭ Skip", "callback_data": f"skip:{draft_id}"}],
        ]
    }
    head = label or f"📝 LinkedIn draft for {draft['day']}"
    text = f"{head}\n\n{draft['text']}"
    if draft["rationale"]:
        text += f"\n\n— why: {draft['rationale']}"
    msg = tg_api(cfg, "sendMessage", chat_id=chat_id, text=text[:4000], reply_markup=keyboard)
    store.update_draft(draft_id, tg_message_id=str(msg["message_id"]))


def deliver_drafts(cfg, store, draft_ids: list[int], rejected: list[dict] | None = None) -> None:
    """One message per draft, then a compact list of what was considered and dropped."""
    day = store.get_draft(draft_ids[0])["day"] if draft_ids else ""
    for i, did in enumerate(draft_ids, 1):
        deliver_draft(cfg, store, did, label=f"📝 {i}/{len(draft_ids)} · {day}")
    if rejected:
        lines = [f"• {r.get('fact', '')} — {r.get('reason', '')}" for r in rejected[:12]]
        tg_api(cfg, "sendMessage",
               chat_id=cfg.secret("TG_ALLOWED_CHAT_ID"),
               text=("Also considered, not drafted (reply if you want one of these):\n"
                     + "\n".join(lines))[:4000])


def notify(cfg, text: str) -> None:
    """Plain informational message to the owner."""
    chat_id = cfg.secret("TG_ALLOWED_CHAT_ID")
    if chat_id and cfg.secret("TG_BOT_TOKEN"):
        try:
            tg_api(cfg, "sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log.warning("notify failed: %s", e)
