"""Persists a first-sighting decision from cli.run()/run_answer() onto the row
the caller already upserted into radar_posts.

cli.py stays pure and fixture-driven on purpose (Phase 1) — it takes a post
dict and a Store for corpus retrieval only, and asserts nothing about
radar_posts/radar_replies. Both lanes (poll.py for Lane B, localapi.py for
Lane A) upsert the sighting into radar_posts themselves, where the
lane-specific metadata (author_id, lane, first_seen_at) is available; this
module is the one place that then moves the row through its state machine and
records the drafted reply or pending question, so Telegram delivery and the
extension's in-page card have something durable to point at.

It's also the one place the two account-safety caps from plan.md §11 are
enforced — max_suggestions_per_day (asks + drafts together) and
max_drafts_per_hour — since both need the durable suggested_at/created_at
timestamps this module is already responsible for writing. Hitting a cap
degrades the decision to 'discard'; the LLM call for that candidate may
already have run (cli.run/run_answer decide before the cap is checked), but
nothing capped is ever persisted as suggested or delivered to the user — the
caps protect what X sees the account doing, not the LLM bill.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import cli


def _day_start(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT00:00:00+00:00")


def _hour_ago(now: datetime) -> str:
    return (now - timedelta(hours=1)).isoformat()


def _daily_cap_reached(cfg, store, now: datetime) -> bool:
    cap = int(cfg.get("radar.max_suggestions_per_day", 12))
    return cap > 0 and store.radar_suggestions_since(_day_start(now)) >= cap


def _hourly_draft_cap_reached(cfg, store, now: datetime) -> bool:
    cap = int(cfg.get("radar.max_drafts_per_hour", 3))
    return cap > 0 and store.radar_drafts_since(_hour_ago(now)) >= cap


def process(post: dict, cfg, store, *, now=None) -> dict:
    """Run cli.run() and persist the outcome. Assumes upsert_radar_post() has
    already been called for this post_id."""
    now = now or datetime.now(timezone.utc)
    post_id = str(post["id"])

    if _daily_cap_reached(cfg, store, now):
        store.set_radar_post_state(post_id, "skipped",
                                   score_reason="daily suggestion cap reached")
        return {"decision": "discard", "gate": "daily_cap",
                "reason": "daily suggestion cap reached"}

    result = cli.run(post, cfg, store, now=now)
    decision = result["decision"]

    if decision in ("discard", "failed"):
        store.set_radar_post_state(post_id, "skipped", score=result.get("score") or 0.0,
                                   score_reason=result.get("reason"))
    elif decision == "ask":
        store.set_radar_post_state(post_id, "asking", score=result.get("score"),
                                   score_reason=result.get("reason"),
                                   pending_question=result["question"],
                                   suggested_at=now.isoformat())
    elif decision == "draft":
        if _hourly_draft_cap_reached(cfg, store, now):
            store.set_radar_post_state(post_id, "skipped", score=result.get("score"),
                                       score_reason="hourly draft cap reached")
            return {"decision": "discard", "gate": "hourly_cap",
                    "reason": "hourly draft cap reached"}
        store.set_radar_post_state(post_id, "suggested", score=result.get("score"),
                                   score_reason=result.get("reason"),
                                   suggested_at=now.isoformat())
        reply_id = store.add_radar_reply(
            post_id, text=result["reply"], status="ready", source="claude",
            evidence_json=json.dumps(result.get("evidence") or [], ensure_ascii=False))
        result["reply_id"] = reply_id
    return result


def process_answer(post: dict, question: str, answer: str, cfg, store, *, now=None) -> dict:
    """Same idea for the §6 ask-a-question follow-up: run_answer(), then persist.

    Doesn't re-check max_suggestions_per_day — the post was already counted
    when its question was first asked (see process() above); this only
    finishes that same suggestion. The hourly draft cap still applies, since
    an LLM call is still about to produce a deliverable reply.
    """
    now = now or datetime.now(timezone.utc)
    post_id = str(post["id"])
    result = cli.run_answer(post, question, answer, cfg, store, now=now)
    decision = result["decision"]

    if decision == "saved_stale":
        store.set_radar_post_state(post_id, "expired", pending_question=None)
    elif decision == "failed":
        store.set_radar_post_state(post_id, "skipped", pending_question=None,
                                   score_reason=result.get("reason"))
    elif decision == "draft":
        if _hourly_draft_cap_reached(cfg, store, now):
            store.set_radar_post_state(post_id, "skipped", pending_question=None,
                                       score_reason="hourly draft cap reached")
            return {"decision": "discard", "gate": "hourly_cap",
                    "reason": "hourly draft cap reached"}
        store.set_radar_post_state(post_id, "suggested", pending_question=None)
        reply_id = store.add_radar_reply(
            post_id, text=result["reply"], status="ready", source="qa",
            question=question, answer=answer,
            evidence_json=json.dumps(result.get("evidence") or [], ensure_ascii=False))
        result["reply_id"] = reply_id
    return result
