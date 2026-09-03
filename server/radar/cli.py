"""Fixture-driven CLI for the X reply radar (plan.md Phase 1).

Runs one candidate post through score -> retrieve -> reply, offline except for
the LLM call itself. Lets scoring thresholds and prompts be tuned before Lane
A (extension) or Lane B (API poller) exist.

Usage:
    python -m server.radar.cli path/to/post.json [--config PATH] [--store DIR]

Fixture JSON shape:
    {"id": "...", "author_handle": "...", "text": "...",
     "created_at": "2026-09-01T12:00:00+00:00",
     "views": 5000, "likes": 40, "reposts": 5, "replies": 3,
     "is_own": false, "is_reply": false, "is_repost": false}

To simulate answering a previously-asked question, add:
    {"_question": "the question that was asked", "answer": "the user's reply"}
to the same fixture — the CLI saves the answer into the corpus and, unless the
post has since expired, drafts a reply from it (plan.md §6 step 2b).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..config import Config
from ..store import Store
from ..util import target_day
from . import reply, retrieve, score


def run(post: dict, cfg, store, *, now=None) -> dict:
    """First-sighting decision: discard / ask / draft / failed."""
    gate_check = score.score_post(post, cfg, now=now)
    if gate_check.gate:
        return {"decision": "discard", "gate": gate_check.gate, "reason": gate_check.reason}

    matches = retrieve.retrieve(store, post.get("text", ""), limit=8)
    relevance = retrieve.relevance_score(matches)
    scored = score.score_post(post, cfg, relevance=relevance, now=now)

    if not matches:
        # low relevance on its own routes to the question flow rather than a
        # discard (plan.md §5 gate 6) — asking is how the corpus fills the gap
        q = reply.draft_question(cfg, post, now=now)
        if q.status != "ready":
            return {"decision": "failed", "reason": q.error, "score": scored.score}
        return {"decision": "ask", "question": q.text, "score": scored.score,
                "reason": scored.reason}

    min_score = float(cfg.get("radar.min_score", 0.55))
    if scored.score < min_score:
        return {"decision": "discard", "gate": "low_score", "reason": scored.reason,
                "score": scored.score}

    r = reply.draft_reply(cfg, post, matches, now=now)
    if r.status != "ready":
        return {"decision": "failed", "reason": r.error, "score": scored.score}
    return {"decision": "draft", "reply": r.text, "score": scored.score,
            "reason": scored.reason, "evidence": [m["summary"] for m in matches]}


def run_answer(post: dict, question: str, answer: str, cfg, store, *, now=None) -> dict:
    """The user answered a previously-asked question — save it, and draft
    from it unless the post has gone cold in the meantime."""
    day = target_day(cfg)
    reply.save_answer(store, post, question, answer, day)
    if score.is_expired(post, cfg, now=now):
        return {"decision": "saved_stale", "note": "answer saved; post has expired, no draft"}
    matches = retrieve.retrieve(store, post.get("text", ""), limit=8)
    r = reply.draft_reply(cfg, post, matches, now=now)
    if r.status != "ready":
        return {"decision": "failed", "reason": r.error}
    return {"decision": "draft", "reply": r.text, "evidence": [m["summary"] for m in matches]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fixture", type=Path)
    ap.add_argument("--config", default=None)
    ap.add_argument("--store", default=None, help="override store_dir for this run")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    store_dir = args.store or cfg.path_of("store_dir") or Path("./dailypost-data")
    store = Store(store_dir)

    post = json.loads(args.fixture.read_text(encoding="utf-8"))
    question = post.pop("_question", None)
    answer = post.pop("answer", None)

    out = run_answer(post, question or "(question not recorded)", answer, cfg, store) \
        if answer else run(post, cfg, store)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
