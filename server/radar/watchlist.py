"""Watchlist growth — plan.md §2: the watchlist grows one Telegram prompt at
a time from authors the extension actually sees, not a batch-imported list
and not an observation week.

Only Lane A (the extension) ever discovers *new* candidates — Lane B's
poll.py only ever queries handles already on the watchlist (plan.md §9), so
`record_sighting` is called from server/bot/localapi.py for every author an
extension-sighted post carries, on-watchlist or not. poll.py also calls it
for watchlist authors, so an incumbent's own stats stay fresh from its
guaranteed Lane B sightings rather than only from opportunistic Lane A ones —
that freshness is what lets a fading incumbent's "demotion signal" (plan.md
§2) actually show up in `strength`.

This module never touches Telegram. `evaluate()` returns a plain `Proposal`
(or None); server/bot/main.py turns that into a message and, once it's
actually sent, calls `mark_proposed` — separating "a candidate cleared the
bar" from "the user was actually pinged" keeps the anti-nag counter accurate
even if evaluate() is called speculatively (e.g. in a test) with nothing sent.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import retrieve
from .score import age_minutes

# plan.md §2: "roughly $0.30/account/month" is what watchlist_max is derived
# from, and what an "Add" prompt quotes back to the user.
COST_PER_ACCOUNT_USD = 0.30

# Capped rolling window of {reach_rate, replies, topic_score} sightings kept
# per author — enough to median over without the column growing forever.
MAX_OBSERVATIONS = 20

# A post "counts" as topically overlapping above this retrieve.py relevance
# score; topic_overlap is stored as the fraction of observations at/above it.
OVERLAP_SCORE_FLOOR = 0.15


def _cfg_growth(cfg, key: str, default):
    return cfg.get(f"radar.growth.{key}", default)


def watchlist_max(cfg) -> int:
    """plan.md §10: null (the default) derives the cap from budget; a manual
    override always wins."""
    configured = cfg.get("radar.watchlist_max", None)
    if configured is not None:
        return int(configured)
    budget = float(cfg.get("radar.monthly_budget_usd", 5.0) or 0.0)
    return max(0, math.floor(budget / COST_PER_ACCOUNT_USD + 1e-9))


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record_sighting(cfg, store, post: dict, now: datetime | None = None) -> None:
    """Fold one freshly-sighted post into its author's rolling stats.

    Callers (poll.py, localapi.py) only reach this for genuinely new posts —
    they've already deduped virtualized-timeline re-sightings before this
    point — so `times_seen` tracks distinct posts, not re-renders.
    """
    author_id = post.get("author_id")
    if not author_id:
        return
    now = now or datetime.now(timezone.utc)
    handle = (post.get("author_handle") or "").lstrip("@")
    row = store.get_radar_author(author_id)
    observations = json.loads(row["observations_json"]) if row and row["observations_json"] else []

    age = age_minutes(post, now)
    reach_rate = (float(post.get("views") or 0) / age) if age else 0.0
    topic_score = retrieve.relevance_score(retrieve.retrieve(store, post.get("text", "")))
    observations.append({
        "reach_rate": reach_rate, "replies": int(post.get("replies") or 0),
        "topic_score": topic_score,
    })
    observations = observations[-MAX_OBSERVATIONS:]

    overlap_hits = sum(1 for o in observations if o["topic_score"] >= OVERLAP_SCORE_FLOOR)
    store.upsert_radar_author(
        author_id,
        handle=handle or (row["handle"] if row else None),
        times_seen=(row["times_seen"] if row else 0) + 1,
        observations=len(observations),
        baseline_reach_rate=statistics.median(o["reach_rate"] for o in observations),
        median_replies_at_sighting=statistics.median(o["replies"] for o in observations),
        topic_overlap=overlap_hits / len(observations),
        observations_json=json.dumps(observations),
    )


def strength(author_row) -> float:
    """The single number plan.md §2's prompts sort/compare on: median reach
    rate. (Its examples list sightings and topic matches too, but only reach
    rate is ever what "stronger"/"weaker" refers to.)"""
    return float(author_row["baseline_reach_rate"] or 0.0)


def overlap_count(author_row) -> int:
    """Sightings-with-overlap count for the "N of their posts overlap..."
    prompt line, back-derived from the stored fraction."""
    return round(float(author_row["topic_overlap"] or 0.0) * int(author_row["observations"] or 0))


def _is_candidate(cfg, author_row, now: datetime) -> bool:
    if author_row["on_watchlist"] or author_row["never"]:
        return False
    min_obs = int(_cfg_growth(cfg, "min_observations", 3))
    if int(author_row["observations"] or 0) < min_obs:
        return False
    min_reach = float(_cfg_growth(cfg, "min_reach_rate", 200))
    if float(author_row["baseline_reach_rate"] or 0.0) < min_reach:
        return False
    min_overlap = float(_cfg_growth(cfg, "min_topic_overlap", 0.3))
    if float(author_row["topic_overlap"] or 0.0) < min_overlap:
        return False
    last_proposed = author_row["last_proposed_at"]
    if last_proposed:
        repropose_days = int(_cfg_growth(cfg, "repropose_days", 30))
        if now - _parse_dt(last_proposed) < timedelta(days=repropose_days):
            return False
    return True


@dataclass
class Proposal:
    kind: str  # "add" | "replace"
    candidate: object  # sqlite3.Row
    incumbent: object | None = None
    watchlist_size: int = 0
    watchlist_cap: int = 0
    cost_usd: float = COST_PER_ACCOUNT_USD


def evaluate(cfg, store, now: datetime | None = None) -> Proposal | None:
    """Look for at most one thing worth proposing right now. Callers only
    message the user when this returns non-None, and must call
    `mark_proposed` if they actually send it."""
    now = now or datetime.now(timezone.utc)
    max_per_day = int(_cfg_growth(cfg, "max_watchlist_prompts_per_day", 1))
    if store.radar_watchlist_prompts_on(now.strftime("%Y-%m-%d")) >= max_per_day:
        return None

    candidates = [r for r in store.radar_candidate_authors() if _is_candidate(cfg, r, now)]
    if not candidates:
        return None
    candidates.sort(key=strength, reverse=True)
    best = candidates[0]

    cap = watchlist_max(cfg)
    current = store.radar_watchlist_authors()

    if len(current) < cap:
        budget = float(cfg.get("radar.monthly_budget_usd", 5.0) or 0.0)
        projected = (len(current) + 1) * COST_PER_ACCOUNT_USD
        if budget > 0 and projected > budget:
            return None  # a manually-raised watchlist_max can't outrun the budget
        return Proposal(kind="add", candidate=best, watchlist_size=len(current), watchlist_cap=cap)

    incumbents = sorted(current, key=strength)
    weakest = incumbents[0]

    grace_days = int(_cfg_growth(cfg, "grace_days", 14))
    min_obs = int(_cfg_growth(cfg, "min_observations", 3))
    added_at = weakest["watchlist_added_at"]
    in_grace = bool(added_at) and (now - _parse_dt(added_at)) < timedelta(days=grace_days)
    if in_grace or int(weakest["observations"] or 0) < min_obs:
        return None  # grace period protects a new/undertested incumbent

    swap_margin = float(_cfg_growth(cfg, "swap_margin", 0.25))
    weakest_strength = strength(weakest)
    if strength(best) < weakest_strength * (1 + swap_margin):
        return None  # hysteresis: must clearly beat the incumbent, not just tie

    return Proposal(kind="replace", candidate=best, incumbent=weakest,
                     watchlist_size=len(current), watchlist_cap=cap)


def mark_proposed(store, author_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    store.set_radar_author(author_id, last_proposed_at=now.isoformat())


def add_to_watchlist(store, author_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    store.set_radar_author(author_id, on_watchlist=1, watchlist_added_at=now.isoformat())


def remove_from_watchlist(store, author_id: str) -> None:
    store.set_radar_author(author_id, on_watchlist=0, watchlist_added_at=None)


def never(store, author_id: str) -> None:
    store.set_radar_author(author_id, never=1)


def effective_watchlist(cfg, store) -> list[str]:
    """Handles Lane B actually polls: the config seed list (wizard time) plus
    whatever growth.py has since persisted to radar_authors.on_watchlist.
    Config is a load-once-per-process singleton (server/config.py) with no
    write-back, so growth can't rewrite config.yaml the way the wizard does —
    radar_authors is the durable, live store instead (plan.md §2)."""
    seeds = [h.lstrip("@") for h in (cfg.get("radar.watchlist", []) or [])]
    grown = [r["handle"] for r in store.radar_watchlist_authors() if r["handle"]]
    out: list[str] = []
    for h in seeds + grown:
        if h not in out:
            out.append(h)
    return out


def _print_row(row) -> None:
    reach = float(row["baseline_reach_rate"] or 0.0)
    print(f"@{row['handle']}  {reach:.0f} views/min median  "
          f"{row['times_seen']} sightings  "
          f"{overlap_count(row)}/{row['observations'] or 0} topic overlap")


def main(argv: list[str] | None = None) -> None:
    """`python -m server.radar.watchlist --add/--remove/--list/--suggest`
    (plan.md §2) — the manual override for a user who'd rather review the
    ranked candidate table on demand than be asked one at a time."""
    import argparse

    from ..config import Config
    from ..store import Store

    parser = argparse.ArgumentParser(prog="python -m server.radar.watchlist")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", metavar="HANDLE")
    group.add_argument("--remove", metavar="HANDLE")
    group.add_argument("--list", action="store_true", help="current watchlist")
    group.add_argument("--suggest", action="store_true", help="ranked candidate table")
    args = parser.parse_args(argv)

    cfg = Config.load()
    store = Store(cfg.path_of("store_dir"))

    if args.list:
        for row in store.radar_watchlist_authors():
            _print_row(row)
    elif args.suggest:
        now = datetime.now(timezone.utc)
        candidates = [r for r in store.radar_candidate_authors() if _is_candidate(cfg, r, now)]
        candidates.sort(key=strength, reverse=True)
        for row in candidates:
            _print_row(row)
    elif args.add:
        handle = args.add.lstrip("@")
        row = store.get_radar_author_by_handle(handle)
        if row is None:
            print(f"@{handle}: not seen yet — nothing to add until the extension sights them")
            return
        add_to_watchlist(store, row["author_id"])
        print(f"@{handle}: added")
    elif args.remove:
        handle = args.remove.lstrip("@")
        row = store.get_radar_author_by_handle(handle)
        if row is None:
            print(f"@{handle}: unknown")
            return
        remove_from_watchlist(store, row["author_id"])
        print(f"@{handle}: removed")


if __name__ == "__main__":
    main()
