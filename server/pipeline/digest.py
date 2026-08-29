"""Assemble the day's digest from unused store items.

- Claude JSONL transcripts are reduced to user/assistant text (tool calls carry
  no args or results in this narrative, so their names add noise, not signal —
  dropped), then a cheap model summarizes that narrative into the digest entry.
  The entry also links the transcript's own file path so the drafting agent can
  Read/Grep the unabridged original when the summary isn't enough.
- Screenshots without a summary get a one-line description via claude -p (vision
  through the Read tool), stored back on the item so it's done once.
- Per-item length is a summarization target, not a slice. If the total digest
  still overflows its cap, the oldest entries are dropped first (not just
  whatever source sorts last alphabetically).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .llm import run_llm
from ..util import get_logger, window_start_iso

log = get_logger("pipeline.digest")


def _activity_rows(path: Path, since: datetime | None) -> list[tuple[str, str, str]]:
    """(timestamp, app, title) samples from one foreground-window log."""
    if not path.exists():
        return []
    rows = []
    try:
        # utf-8-sig tolerates a BOM from Windows-side writers
        with path.open(encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(rec.get("ts") or "")
                if since and ts:
                    try:
                        if datetime.fromisoformat(ts.replace("Z", "+00:00")) < since:
                            continue
                    except ValueError:
                        pass
                rows.append((ts, str(rec.get("app") or "?"), str(rec.get("title") or "")))
    except OSError:
        return []
    return rows


def _activity_timeline(rows: list[tuple[str, str, str]], cap: int) -> str:
    """Foreground-window samples -> a compact timeline of non-coding activity.

    Consecutive samples in the same app collapse into one line, so a day of work
    reads as a handful of lines rather than hundreds of samples. Rows from every
    log file are merged before collapsing: the recorder rotates hourly, so one
    day arrives as a dozen items that would otherwise render as a dozen
    disconnected sections with the collapsing restarting in each.
    """
    out, last_app = [], None
    for ts, app, title in sorted(rows):
        hhmm = ts[11:16]
        out.append(f"{hhmm} {title}" if app == last_app else f"{hhmm} [{app}] {title}")
        last_app = app
    return "\n".join(out)[:cap]


def _read_note(path: Path, cap: int) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").strip()[:cap]
    except OSError:
        return ""


def _in_window(rec: dict, since: datetime | None) -> bool:
    """A session file's mtime says when it was last touched, which can be long
    after the conversation happened (or when an unrelated tool rewrote it).
    Only messages actually written inside the window belong in the digest."""
    if since is None:
        return True
    ts = rec.get("timestamp")
    if not ts:
        return True  # keep un-timestamped records (e.g. summaries)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")) >= since
    except ValueError:
        return True


# Safety bound on what gets sent to the summarizer model, independent of any
# digest sizing config — protects a single pathological session (huge tool
# output pasted into an assistant message, say) from blowing the call's context.
_NARRATIVE_SAFETY_CAP = 150_000

SESSION_SUMMARY_PROMPT = """Below is a coding-agent work session transcript (user
prompts and assistant text; tool calls are omitted). Summarize what actually
happened — what was investigated, and any concrete numbers, findings, or
conclusions reached, not just what was attempted. If the session found nothing
noteworthy (routine chores, a dead end, no real result), say that plainly in one
line instead of padding.

Write dense prose, not a bulleted recap. Stay under {cap} characters.

Transcript:
\"\"\"
{narrative}
\"\"\"
"""


def _compress_jsonl(path: Path, since: datetime | None = None) -> str:
    """Reduce a Claude Code session transcript to a readable narrative,
    keeping only messages written within the harvest window."""
    if not path.exists():
        return ""
    lines_out: list[str] = []
    cwd = branch = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = rec.get("cwd") or cwd
                branch = rec.get("gitBranch") or branch
                if not _in_window(rec, since):
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role")
                content = msg.get("content")
                if role == "user" and isinstance(content, str) and content.strip():
                    if not content.startswith(("<command-name>", "<local-command", "Caveat:")):
                        lines_out.append(f"USER: {content.strip()[:600]}")
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if role == "assistant" and block.get("type") == "text" and block.get("text", "").strip():
                            lines_out.append(f"ASSISTANT: {block['text'].strip()[:400]}")
                        elif role == "user" and block.get("type") == "text" and str(block.get("text", "")).strip():
                            t = str(block["text"]).strip()
                            if not t.startswith(("<command-name>", "<local-command", "Caveat:", "<system-reminder>")):
                                lines_out.append(f"USER: {t[:600]}")
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return ""
    if not lines_out:
        return ""  # touched file, but nothing said inside the window
    header = f"(project: {cwd or path.name}, branch: {branch or '?'})"
    text = header + "\n" + "\n".join(lines_out)
    return text[:_NARRATIVE_SAFETY_CAP]


def _codex_cwd(rec: dict) -> str | None:
    if rec.get("type") not in ("session_meta", "turn_context"):
        return None
    payload = rec.get("payload") or rec
    if not isinstance(payload, dict):
        return None
    if payload.get("cwd"):
        return payload["cwd"]
    sel = payload.get("TurnEnvironmentSelections")
    return sel.get("cwd") if isinstance(sel, dict) else None


def _codex_message_text(rec: dict) -> tuple[str | None, str | None]:
    """(role, text) from a response_item message record, or (None, None) if
    this record isn't one. Defensive on purpose — the Codex rollout schema is
    undocumented, version-dependent, and has already changed shape across
    releases; an unrecognised record is skipped, never raised on."""
    if rec.get("type") != "response_item":
        return None, None
    payload = rec.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None, None
    role = payload.get("role")
    content = payload.get("content")
    if not isinstance(content, list):
        return None, None
    parts = [str(b["text"]) for b in content
            if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text") and b.get("text")]
    text = "\n".join(parts).strip()
    return (role, text) if text else (None, None)


def _compress_codex_jsonl(path: Path, since: datetime | None = None) -> str:
    """Reduce a Codex rollout transcript to a readable narrative, keeping only
    messages written within the harvest window. Mirrors _compress_jsonl but for
    Codex's {"type": "session_meta"|"turn_context"|"response_item"|"event_msg"}
    record shape — every line still carries an ISO-8601 timestamp, so _in_window
    works unchanged."""
    if not path.exists():
        return ""
    lines_out: list[str] = []
    cwd = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                cwd = _codex_cwd(rec) or cwd
                if not _in_window(rec, since):
                    continue
                role, text = _codex_message_text(rec)
                if not text:
                    continue
                if role == "user":
                    lines_out.append(f"USER: {text[:600]}")
                elif role == "assistant":
                    lines_out.append(f"ASSISTANT: {text[:400]}")
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return ""
    if not lines_out:
        return ""  # touched file, but nothing said inside the window
    header = f"(project: {cwd or path.name})"
    text = header + "\n" + "\n".join(lines_out)
    return text[:_NARRATIVE_SAFETY_CAP]


def _summarize_session(cfg, narrative: str, cap: int) -> str:
    """Dense summary of a session's in-window narrative via a cheap model.

    Falls back to a raw head-slice of the narrative on any failure — losing the
    density but never losing the item outright over a transient model error.
    """
    backend = str(cfg.get("pipeline.backend", "claude") or "claude")
    default_model = "claude-haiku-4-5" if backend == "claude" else None
    model = cfg.get("pipeline.summary_model", default_model)
    prompt = SESSION_SUMMARY_PROMPT.format(narrative=narrative, cap=cap)
    try:
        summary = run_llm(cfg, prompt, model=str(model) if model else None, timeout=300).text.strip()
    except Exception as e:
        log.warning("session summary failed (%s) — falling back to raw excerpt", e)
        return narrative[:cap]
    return summary[:cap] if summary else narrative[:cap]


def describe_screenshots(cfg, store, items) -> None:
    """One claude -p call describing all new screenshots; summaries saved on items."""
    todo = [i for i in items if i["kind"] == "screenshot" and not i["summary"] and i["path"] and Path(i["path"]).exists()]
    if not todo:
        return
    paths = [i["path"] for i in todo]
    dirs = sorted({str(Path(p).parent) for p in paths})
    prompt = (
        "Each screenshot below is available to you, either via the Read tool or "
        "as an attached image. For each one output exactly one line:\n"
        "<filename>: <one-sentence description of what the screenshot shows>\n"
        "No other text.\n\n" + "\n".join(paths)
    )
    try:
        # allow_read_dirs is how the Claude backend sees these (Read tool);
        # images is how the Codex backend sees them (-i flags, since Codex's
        # --add-dir grants write access, not read) — each backend uses the one
        # it understands and ignores the other.
        result = run_llm(cfg, prompt, allow_read_dirs=dirs, images=paths, timeout=900).text
    except Exception as e:
        log.warning("screenshot description failed: %s", e)
        return
    by_name = {}
    for line in result.splitlines():
        if ":" in line:
            fname, desc = line.split(":", 1)
            by_name[Path(fname.strip()).name] = desc.strip()
    for item in todo:
        desc = by_name.get(Path(item["path"]).name)
        if desc:
            store.set_item_summary(item["id"], desc)


def _render_digest(day: str, entries: list[tuple[str, str, str]]) -> str:
    """entries: (ts, source, entry_text). Grouped by source, source names sorted —
    the ordering within a source no longer matters for cap purposes since that's
    decided before this is called, so this only controls final readability."""
    parts = [f"# Work digest for {day}\n"]
    for src in sorted({src for _, src, _ in entries}):
        parts.append(f"\n## Source: {src}\n")
        parts.extend(text for ts, s, text in entries if s == src)
    return "\n".join(parts)


def build_digest(cfg, store, day: str) -> tuple[str, list[int]]:
    """Returns (digest markdown, item ids included)."""
    since_iso = window_start_iso(cfg)
    items = store.unused_items_since(since_iso)
    if not items:
        return "", []

    describe_screenshots(cfg, store, items)
    # re-read to pick up fresh summaries
    items = store.unused_items_since(since_iso)

    per_item_cap = int(cfg.get("pipeline.per_item_max_chars", 4000))
    total_cap = int(cfg.get("pipeline.digest_max_chars", 400000))

    window_start = datetime.fromisoformat(since_iso)
    entries: list[tuple[str, str, str]] = []
    ids: list[int] = []
    skipped_stale = 0
    activity_rows: list[tuple[str, str, str]] = []
    activity_sources: list[str] = []
    for item in items:
        ids.append(item["id"])
        src = item["source"]
        if item["kind"] in ("claude_jsonl", "codex_jsonl"):
            path = item["path"]
            compress = _compress_jsonl if item["kind"] == "claude_jsonl" else _compress_codex_jsonl
            narrative = compress(Path(path), since=window_start)
            if not narrative:
                skipped_stale += 1
                continue
            body = _summarize_session(cfg, narrative, per_item_cap)
            entry = f"### Coding session ({src})\nFull transcript: {path}\n{body}"
        elif item["kind"] == "screenshot":
            entry = f"- Screenshot: {item['summary'] or Path(item['path'] or '').name}"
        elif item["kind"] == "activity_log":
            activity_rows += _activity_rows(Path(item["path"] or ""), window_start)
            if src not in activity_sources:
                activity_sources.append(src)
            continue  # merged into one timeline after the loop
        elif item["kind"] == "transcript":
            if not (item["summary"] or "").strip() or item["summary"].startswith("["):
                continue
            meta = json.loads(item["meta_json"]) if item["meta_json"] else {}
            when = (item["ts"] or "")[11:16]
            entry = (f"### Spoken conversation {when} "
                     f"({meta.get('speech_seconds', '?')}s)\n{item['summary'][:per_item_cap]}")
        elif item["kind"] == "note":
            # ingest_dir stores dropped .txt/.md with no summary — the text is in
            # the file, so read it here rather than dropping the item silently.
            body = _read_note(Path(item["path"] or ""), per_item_cap) if item["path"] else ""
            if not body:
                body = (item["summary"] or "")[:per_item_cap]
            if not body.strip():
                continue
            entry = f"### Note ({Path(item['path'] or 'note').name})\n{body}"
        else:
            body = (item["summary"] or "")[:per_item_cap]
            if not body.strip():
                if item["kind"] == "file":
                    log.info("no renderer for %s item %s — not in the digest",
                             item["kind"], Path(item["path"] or "?").name)
                continue
            meta = json.loads(item["meta_json"]) if item["meta_json"] else {}
            prefix = f"[{meta.get('chat')}] " if meta.get("chat") else ""
            entry = f"- {prefix}{body}"
        entries.append((item["ts"] or "", src, entry))

    timeline = _activity_timeline(activity_rows, per_item_cap)
    if timeline:
        entries.append((min(ts for ts, _, _ in activity_rows),
                        ", ".join(activity_sources),
                        f"### What was on screen (non-coding activity)\n{timeline}"))

    if skipped_stale:
        log.info("skipped %d session file(s) touched in-window but with no messages in it",
                 skipped_stale)

    digest = _render_digest(day, entries)
    if len(digest) > total_cap:
        # A blind tail-slice here always dropped whichever source sorted last
        # alphabetically. Drop the least-fresh material first instead, regardless
        # of source — item ids are still returned in full below (mark_used treats
        # "considered this run" the same as "made the cut", matching the existing
        # skipped_stale items above).
        by_age = sorted(entries, key=lambda e: e[0])
        dropped = 0
        while by_age and len(_render_digest(day, by_age)) > total_cap:
            by_age.pop(0)
            dropped += 1
        digest = _render_digest(day, by_age)
        digest += f"\n\n[digest truncated at size cap: dropped {dropped} oldest item(s) of {len(entries)}]"
        log.warning("digest for %s hit the %d-char cap — dropped %d/%d item(s)",
                    day, total_cap, dropped, len(entries))
    return digest, ids
