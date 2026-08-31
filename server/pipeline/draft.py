"""Draft writer: digest → claude -p → draft row → Telegram delivery."""
from __future__ import annotations

import re
from pathlib import Path

import requests

from ..util import get_logger
from .claude_cli import extract_json
from .llm import run_llm

log = get_logger("pipeline.draft")

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _prompt_file(cfg, key: str, default_name: str) -> Path:
    """Instance override (config path, survives deploys) or the repo default."""
    override = cfg.path_of(f"pipeline.{key}")
    return override if override and override.exists() else PROMPTS_DIR / default_name


def _always_hashtags(cfg) -> list[str]:
    return [t.strip().lstrip("#") for t in (cfg.get("pipeline.always_hashtags", []) or [])
           if t and t.strip()]


DEAI_PROMPT = """{skill}

---

Apply the ruleset above to the text below. This runs unattended as part of a
posting pipeline — there is no human to hand a report to, so just do the
rewrite and hand back the result, nothing else.

Text:
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object, no other text:
{{"cleaned_text": "the fully rewritten text, ready to publish",
  "flagged": "one short note on any thin claim, unsupported number, or contradiction left for a\
 human to judge, or empty string if none"}}
"""


def deai_cleanup(cfg, text: str) -> str:
    """Run the de-AI skill as a standalone pass over a finished piece of draft text.

    Deliberately a separate `claude -p` call rather than folding into the
    drafting prompt: a model grading its own prose in the same turn it wrote
    it is exactly the "self-narrating rigor" failure mode the skill flags.
    A fresh, single-purpose call catches more than the AI-tells reminder
    already baked into draft-prompt.md's final-pass instruction.

    Never raises — any failure here falls back to the pre-cleanup text rather
    than losing a draft over a cleanup step.
    """
    if not text.strip():
        return text
    skill = _prompt_file(cfg, "deai_skill", "deai-skill.md").read_text(encoding="utf-8")
    prompt = DEAI_PROMPT.format(skill=skill, text=text)
    try:
        result = run_llm(cfg, prompt, timeout=300).text
        data = extract_json(result)
    except Exception as e:
        log.warning("deai cleanup failed (%s) — keeping pre-cleanup text", e)
        return text
    cleaned = (data.get("cleaned_text") or "").strip()
    if not cleaned:
        log.warning("deai cleanup returned no cleaned_text — keeping pre-cleanup text")
        return text
    flagged = (data.get("flagged") or "").strip()
    if flagged:
        log.info("deai cleanup flagged for review: %s", flagged)
    return cleaned


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
    result = run_llm(
        cfg, prompt, session=session_id, timeout=900,
        allow_read_dirs=[str(files_dir)] if files_dir.exists() else None,
        allowed_tools="Read,Grep,Glob",
    ).text
    try:
        data = extract_json(result)
    except ValueError:
        return result.strip()[:2000], None
    post = (data.get("post_text") or "").strip() or None
    if post:
        post = deai_cleanup(cfg, post)
    return (data.get("reply") or "").strip() or "(no reply)", post


IMAGE_BRIEF_PROMPT = """You wrote a LinkedIn post for {day} and the user has approved the
text. Now brief an illustration for it.

The post:
\"\"\"
{post_text}
\"\"\"
{extra}
You know the story behind this post, not just its words — brief a concrete,
literal scene: the real subject, setting and action behind the post, not an
abstract metaphor or symbolic stand-in. Rules for the prompt you write:
- Describe a scene or composition, concretely. No slide layouts, no infographics.
- Derive the scene entirely from what's specific to *this* story — the actual
  object, place, tool or moment it turns on. Don't default to a generic "person
  at a laptop/desk" scene out of habit; reach for it only when that literally is
  the most distinctive part of today's story, and vary the framing, time of day
  and setting rather than repeating the same composition draft after draft.
- Ground it in the post's real subject and what actually happened — not a
  hypothetical example, mockup, quote or sample the post describes in passing as
  something that was produced or considered, and not a different, easier-to-draw
  industry or business than the post's own. You know what this business actually
  does from the day's material; use that, don't substitute a generic retail/craft
  stand-in because it photographs better. If the post mentions the *content* of
  an example (a sample ad, a made-up headline, an illustrative scenario), don't
  render that example's content literally and don't invent specifics (products,
  industries, settings) it doesn't actually contain — depict the real work or
  moment that produced it instead.
- No text, letters, numbers or watermarks anywhere in the image; image models
  render them badly and LinkedIn readers notice. Two exceptions: (1) if the post
  names a specific company, show that company's real logo or brand mark somewhere
  in the scene (screen, sign, product, packaging...), rendered as accurately as
  you can; (2) if the feedback below explicitly asks for specific words to appear
  in the image, include that exact text as a short, bold caption — feedback wins
  over this rule.
- No recognisable real people.
- The composition must fill the whole frame, edge to edge. Never ask for a
  cinematic, widescreen, anamorphic or film-still look: the model answers those
  by painting black letterbox bars into the canvas, which show up as dead bands
  in the feed. Frame the scene to the canvas shape instead.
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
Revise that prompt to address the feedback precisely — if the feedback conflicts
with a rule above (e.g. it asks for specific words in the image), the feedback
wins. Keep everything they didn't complain about.
"""

IMAGE_BRIEF_VARY = """
Your most recent prompt for this same post was:
\"\"\"
{prev_prompt}
\"\"\"
No specific complaint this time — the user just tapped Regenerate for a fresh
take. Pick a genuinely different concrete angle on the story than that one AND
than any other prompt you wrote earlier in this conversation: a different
moment, object or setting, not a small variation on the same composition (e.g.
don't just re-frame the same scene or swap what's on the table — find a
different scene entirely).
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
    elif prev_prompt:
        extra = IMAGE_BRIEF_VARY.format(prev_prompt=prev_prompt)
    style = str(cfg.get("image.style_suffix", "") or "").strip() or "no particular house style"
    prompt = IMAGE_BRIEF_PROMPT.format(day=day, post_text=post_text, extra=extra, style=style)

    if session_id:
        try:
            result = run_llm(cfg, prompt, session=session_id, timeout=300).text
        except Exception as e:
            log.warning("image brief: session %s unusable (%s) — falling back", session_id, e)
            result = run_llm(cfg, prompt, timeout=300).text
    else:
        result = run_llm(cfg, prompt, timeout=300).text

    data = extract_json(result)
    image_prompt = (data.get("image_prompt") or "").strip()
    if not image_prompt:
        raise ValueError(f"no image_prompt in response: {result[:300]}")
    alt = (data.get("alt_text") or "").strip() or "Illustration for this post"
    return image_prompt, alt


X_REWRITE_PROMPT = """You wrote a LinkedIn post for {day} and it has already been
published. Now write the X (Twitter) version of the SAME fact — by default a genuine
rewrite for that platform's voice and length, not a truncation of the LinkedIn text.

The published LinkedIn post:
\"\"\"
{post_text}
\"\"\"
{extra}
Rules below are defaults — if the note above explicitly asks for something these
conflict with (e.g. reusing the LinkedIn text as-is, keeping detail these rules would
otherwise cut, or going long now that there's room), follow the note instead:
- Keep the same fact and hook — don't introduce anything the LinkedIn post didn't say.
- Drop the "why it matters" elaboration and any mechanism detail; X readers want the
  fact and the stake, not the walkthrough.
- Hard limit: {limit} characters, including any hashtags. Aim well under it — a post
  that just barely fits is fragile to X's own link/emoji weighting.
{hashtag_rule}
- Every hard rule from the style guide earlier in this conversation still applies:
  no stack names, no fabrication, anonymize people, not salesy.

Return ONLY a JSON object, no other text:
{{"post_text": "the full X post, ready to publish"}}
"""

X_REWRITE_REVISION = """
Your previous attempt was:
\"\"\"
{prev_text}
\"\"\"
Instruction for this revision: {feedback}
Follow that instruction exactly, even if it means replacing most or all of the
previous attempt — e.g. if it asks to reuse the LinkedIn post, use that post's own
wording verbatim rather than paraphrasing it again. Otherwise keep what was working.
"""


def x_rewrite(cfg, day: str, session_id: str | None, post_text: str,
             feedback: str | None = None, prev_text: str | None = None,
             limit: int = 280) -> str:
    """Ask for an X-native rewrite of an already-published LinkedIn post.

    Resumes the day's session when there is one, so the rewrite comes from the
    day's story rather than a re-reading of the LinkedIn post's wording, and the
    style guide's hard rules are still in context. Falls back to a one-shot call
    when the session is gone. Takes plain values, no Store: safe to call from
    asyncio.to_thread.

    Retries once, internally, if the model's own answer runs over `limit` — the
    API is the final authority on X's exact (weighted) character count, but there
    is no reason to show the user an answer already known to be too long.
    """
    extra = ""
    if feedback:
        extra = X_REWRITE_REVISION.format(
            prev_text=prev_text or "(not recorded)", feedback=feedback)
    always_tags = _always_hashtags(cfg)
    if always_tags:
        tag_str = " ".join(f"#{t}" for t in always_tags)
        hashtag_rule = (f"- Always include these exact hashtags, verbatim: {tag_str}. On top of "
                        f"those, add 0-2 more hashtags genuinely relevant to X's audience for "
                        f"THIS post — don't just repeat whatever tags the LinkedIn version used.")
    else:
        hashtag_rule = "- 0-2 hashtags, only if truly relevant to X's audience for this post."
    prompt = X_REWRITE_PROMPT.format(day=day, post_text=post_text, extra=extra, limit=limit,
                                     hashtag_rule=hashtag_rule)

    def _ask(p: str) -> str:
        if session_id:
            try:
                result = run_llm(cfg, p, session=session_id, timeout=300).text
            except Exception as e:
                log.warning("x rewrite: session %s unusable (%s) — falling back", session_id, e)
                result = run_llm(cfg, p, timeout=300).text
        else:
            result = run_llm(cfg, p, timeout=300).text
        data = extract_json(result)
        text = (data.get("post_text") or "").strip()
        if not text:
            raise ValueError(f"no post_text in response: {result[:300]}")
        return text

    text = _ask(prompt)
    if len(text) > limit:
        over = len(text) - limit
        retry_prompt = prompt + X_REWRITE_REVISION.format(
            prev_text=text,
            feedback=f"that was {len(text)} characters, {over} over the {limit} limit. "
                     f"Cut content, don't just trim words off the end.")
        text = _ask(retry_prompt)
    cleaned = deai_cleanup(cfg, text)
    if len(cleaned) <= limit:
        text = cleaned
    else:
        log.warning("deai cleanup pushed X rewrite over the %d limit (%d chars) — keeping "
                    "pre-cleanup text",
                    limit, len(cleaned))
    return text


REDDIT_TITLE_PROMPT = """You wrote a LinkedIn post for {day} and it has already been
published. The same text will be reused verbatim as a Reddit text post in
r/{subreddit} — write ONLY the title for it; LinkedIn posts don't have one and
Reddit requires one.

The published LinkedIn post:
\"\"\"
{post_text}
\"\"\"
{extra}
Rules:
- A statement or a question, in r/{subreddit}'s register. Not a headline, not
  clickbait, no "Here's what I learned".
- No hashtags, no emoji, no ALL-CAPS — they read as spam on Reddit.
- Concrete over vague — name the actual thing that happened.
- Hard limit: {limit} characters. Aim for 60-120.

Return ONLY a JSON object, no other text:
{{"title": "the title, ready to post"}}
"""

REDDIT_TITLE_REVISION = """
Your previous attempt was:
\"\"\"
{prev_title}
\"\"\"
Problem with it: {feedback}
Revise it to fix that. Keep everything else that worked.
"""


def reddit_title(cfg, day, session_id, post_text,
                 subreddit="", limit=300) -> str:
    """Ask for a title-only Reddit companion to an already-published LinkedIn post.

    The body is the LinkedIn text reused verbatim (see reddit_body) — Reddit's
    only real gap is the missing title. Same session-resume-then-fallback
    structure as x_rewrite, so the title comes from the day's story rather than
    a re-reading of the LinkedIn post's wording. Takes plain values, no Store:
    safe to call from asyncio.to_thread.

    Retries once, internally, if the model's own answer runs over `limit`.
    """
    prompt = REDDIT_TITLE_PROMPT.format(
        day=day, post_text=post_text, extra="", subreddit=subreddit or "buildinpublic",
        limit=limit)

    def _ask(p: str) -> str:
        if session_id:
            try:
                result = run_llm(cfg, p, session=session_id, timeout=300).text
            except Exception as e:
                log.warning("reddit title: session %s unusable (%s) — falling back", session_id, e)
                result = run_llm(cfg, p, timeout=300).text
        else:
            result = run_llm(cfg, p, timeout=300).text
        data = extract_json(result)
        title = (data.get("title") or "").strip()
        if not title:
            raise ValueError(f"no title in response: {result[:300]}")
        return title

    title = _ask(prompt)
    if len(title) > limit:
        over = len(title) - limit
        retry_prompt = prompt + REDDIT_TITLE_REVISION.format(
            prev_title=title, feedback=f"that was {len(title)} characters, {over} over the "
                                       f"{limit} limit. Cut words, don't just truncate the end.")
        title = _ask(retry_prompt)
    cleaned = deai_cleanup(cfg, title)
    if len(cleaned) <= limit:
        title = cleaned
    else:
        log.warning("deai cleanup pushed reddit title over the %d limit (%d chars) — keeping "
                    "pre-cleanup title",
                    limit, len(cleaned))
    return title


_HASHTAG_LINE_RE = re.compile(r"(?m)^[ \t]*(?:#\w+[ \t]*)+$\n?")
_TRAILING_HASHTAG_RUN_RE = re.compile(r"(?:[ \t]*#\w+)+[ \t]*$")
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")


def reddit_body(text: str) -> str:
    """LinkedIn draft text -> the box on the Reddit submit form.

    Reuses the text verbatim except for hashtags, which read as spam on Reddit
    and are a LinkedIn-only convention here (pipeline.always_hashtags is scoped
    to X posts only, but the drafting agent still adds its own to the LinkedIn
    text sometimes). Deliberately NOT reformatting or "de-LinkedIn-ing" the
    prose beyond that — see plan.md §2 for why a fuller rewrite is a separate,
    evidence-gated upgrade.
    """
    out = _HASHTAG_LINE_RE.sub("", text)
    out = _TRAILING_HASHTAG_RUN_RE.sub("", out)
    out = _EXTRA_BLANK_LINES_RE.sub("\n\n", out)
    return out.strip()


def write_drafts(cfg, store, day: str, digest: str) -> tuple[list[int], list[dict]]:
    """Draft one post per interesting fact found in the day.

    Returns (draft_ids best-first, rejected candidates). Runs inside a named
    session so the bot can keep talking about this day afterwards.
    """
    style = _prompt_file(cfg, "style_guide", "style-guide.md").read_text(encoding="utf-8")
    task = _prompt_file(cfg, "draft_prompt", "draft-prompt.md").read_text(encoding="utf-8")
    hooks = store.recent_hooks(day, limit=int(cfg.get("pipeline.recent_hooks", 10)))
    task = (task.replace("{LANGUAGE_OUT}", str(cfg.get("pipeline.language_out", "English")))
                .replace("{MAX_DRAFTS}", str(cfg.get("pipeline.max_drafts", 4)))
                .replace("{RECENT_HOOKS}",
                         "\n".join(f"- {h}" for h in hooks) if hooks else "(none yet)"))
    prompt = f"{style}\n\n{task}\n\n# Digest\n\n{digest}"

    # Each coding-session entry in the digest is a summary, not the transcript
    # itself, and links its own "Full transcript: <path>" line — give the model
    # Read/Grep to actually follow those links (e.g. to pull an exact number the
    # summary only gestured at) rather than working from the summary alone.
    files_dir = store.files_dir
    read_kwargs = (dict(allow_read_dirs=[str(files_dir)], allowed_tools="Read,Grep,Glob")
                  if files_dir.exists() else {})

    # Rarely, the model appends a trailing thinking-only turn after already
    # answering, and the CLI reports that turn's (empty) text as the result —
    # the good answer earlier in the same response is lost to us. That's
    # non-deterministic, so a single retry on a fresh session usually gets a
    # clean answer rather than losing the whole day's draft to it.
    #
    # Call-then-record, not mint-then-call: Codex can't be handed a session id
    # up front (no --session-id equivalent), only discovered from its response,
    # so the session id is learned from the result rather than chosen before it.
    result = run_llm(cfg, prompt, timeout=1800, **read_kwargs)
    try:
        data = extract_json(result.text)
    except ValueError as e:
        log.warning("write_drafts: %s — retrying once on a fresh session", e)
        result = run_llm(cfg, prompt, timeout=1800, **read_kwargs)
        data = extract_json(result.text)
    store.set_day_session(day, result.session_id, backend=result.backend)

    rejected = data.get("rejected") or []
    ids = []
    for cand in (data.get("candidates") or [])[:int(cfg.get("pipeline.max_drafts", 4))]:
        text = (cand.get("post_text") or "").strip()
        if not text:
            continue
        text = deai_cleanup(cfg, text)
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
