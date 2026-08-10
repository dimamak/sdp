"""Assemble the day's digest from unused store items.

- Claude JSONL transcripts are compressed to: user prompts, assistant text
  (truncated), and tool names — enough narrative for story selection.
- Screenshots without a summary get a one-line description via claude -p (vision
  through the Read tool), stored back on the item so it's done once.
- Everything is capped: per item and total.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .claude_cli import run_claude
from ..util import get_logger, window_start_iso

log = get_logger("pipeline.digest")


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


def _compress_jsonl(path: Path, per_item_cap: int, since: datetime | None = None) -> str:
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
                        elif role == "assistant" and block.get("type") == "tool_use":
                            lines_out.append(f"  [tool: {block.get('name')}]")
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
    return text[:per_item_cap]


def describe_screenshots(cfg, store, items) -> None:
    """One claude -p call describing all new screenshots; summaries saved on items."""
    todo = [i for i in items if i["kind"] == "screenshot" and not i["summary"] and i["path"] and Path(i["path"]).exists()]
    if not todo:
        return
    paths = [i["path"] for i in todo]
    dirs = sorted({str(Path(p).parent) for p in paths})
    prompt = (
        "For each screenshot file listed below, Read it and output exactly one line:\n"
        "<filename>: <one-sentence description of what the screenshot shows>\n"
        "No other text.\n\n" + "\n".join(paths)
    )
    try:
        result = run_claude(cfg, prompt, allow_read_dirs=dirs, timeout=900)
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
    sections: dict[str, list[str]] = {}
    ids: list[int] = []
    skipped_stale = 0
    for item in items:
        ids.append(item["id"])
        src = item["source"]
        if item["kind"] == "claude_jsonl":
            body = _compress_jsonl(Path(item["path"]), per_item_cap, since=window_start)
            if not body:
                skipped_stale += 1
                continue
            entry = f"### Coding session ({src})\n{body}"
        elif item["kind"] == "screenshot":
            entry = f"- Screenshot: {item['summary'] or Path(item['path'] or '').name}"
        else:
            body = (item["summary"] or "")[:per_item_cap]
            if not body.strip():
                continue
            meta = json.loads(item["meta_json"]) if item["meta_json"] else {}
            prefix = f"[{meta.get('chat')}] " if meta.get("chat") else ""
            entry = f"- {prefix}{body}"
        sections.setdefault(src, []).append(entry)

    parts = [f"# Work digest for {day}\n"]
    for src in sorted(sections):
        parts.append(f"\n## Source: {src}\n")
        parts.extend(sections[src])
    if skipped_stale:
        log.info("skipped %d session file(s) touched in-window but with no messages in it",
                 skipped_stale)
    digest = "\n".join(parts)
    if len(digest) > total_cap:
        digest = digest[:total_cap] + "\n\n[digest truncated at size cap]"
    return digest, ids
