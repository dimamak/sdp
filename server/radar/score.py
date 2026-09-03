"""Views-per-minute scoring for the X reply radar — see plan.md §5.

Reach is built on views-per-minute because it's available on 100% of posts at
first sighting, straight off the timeline DOM (an aria-label carries the exact
integer) or the API's public_metrics. Author baseline and cross-observation
velocity are optional bonus refinements: watchlist accounts accumulate them,
Lane A's long tail never will, and the score has to be valid either way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ScoreResult:
    score: float
    reason: str
    gate: str | None = None  # None means every gate passed


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def age_minutes(post: dict, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - _parse_ts(post["created_at"])).total_seconds() / 60.0)


def is_expired(post: dict, cfg, now: datetime | None = None) -> bool:
    """Whether a post has aged out of the reply window — used both as a score
    gate and, separately, to decide whether a delayed question-answer can
    still be drafted (plan.md §6 step 2b: a stale answer is saved regardless)."""
    return age_minutes(post, now) > float(cfg.get("radar.max_age_minutes", 25))


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_post(post: dict, cfg, *, relevance: float = 0.0,
               now: datetime | None = None) -> ScoreResult:
    """Gate, then score, one candidate post.

    `relevance` is the caller's corpus-match strength (see retrieve.py) — kept
    as a plain parameter rather than computed here so this stays testable
    without a Store, and so gating (cheap) can run before retrieval (a DB
    scan) for posts that would be discarded anyway.
    """
    if post.get("is_own"):
        return ScoreResult(0.0, "the user's own post", "own_post")
    if post.get("is_reply"):
        return ScoreResult(0.0, "post is itself a reply", "is_reply")
    if post.get("is_repost"):
        return ScoreResult(0.0, "post is a repost", "is_repost")

    age = age_minutes(post, now)
    min_age = float(cfg.get("radar.min_age_minutes", 3))
    max_age = float(cfg.get("radar.max_age_minutes", 25))
    if age < min_age:
        return ScoreResult(
            0.0, f"only {age:.1f}m old, views denominator hasn't settled", "too_new")
    if age > max_age:
        return ScoreResult(0.0, f"{age:.1f}m old, past the reply window", "too_old")

    views = float(post.get("views") or 0)
    min_views = float(cfg.get("radar.min_views", 500))
    if views < min_views:
        return ScoreResult(
            0.0, f"{views:.0f} views, below the {min_views:.0f} floor", "low_reach")

    replies = float(post.get("replies") or 0)
    max_replies = float(cfg.get("radar.max_existing_replies", 25))
    if replies > max_replies:
        return ScoreResult(0.0, f"{replies:.0f} replies already, the slot is gone", "crowded")

    reach_rate = views / age  # views/minute — available on every post, first sighting
    reach_cap = float(cfg.get("radar.reach_rate_cap", 500))
    reach_component = _clip01(math.log1p(reach_rate) / math.log1p(reach_cap))

    baseline = post.get("author_baseline_reach_rate")
    if baseline:
        baseline_component = _clip01(math.log1p(float(baseline)) / math.log1p(reach_cap))
        reach_component = (reach_component + baseline_component) / 2

    velocity = post.get("velocity_views_per_min")
    if velocity:
        velocity_component = _clip01(math.log1p(float(velocity)) / math.log1p(reach_cap))
        reach_component = (reach_component + velocity_component) / 2

    likes = float(post.get("likes") or 0)
    reposts = float(post.get("reposts") or 0)
    quality = _clip01((likes + reposts) / views) if views else 0.0
    crowdedness = _clip01(1 - replies / max_replies) if max_replies else 1.0
    relevance = _clip01(relevance)

    weights = cfg.get("radar.weights") or {}
    w_reach = float(weights.get("reach", 0.4))
    w_quality = float(weights.get("quality", 0.2))
    w_crowd = float(weights.get("crowdedness", 0.15))
    w_rel = float(weights.get("relevance", 0.25))

    score = (w_reach * reach_component + w_quality * quality
             + w_crowd * crowdedness + w_rel * relevance)
    reason = (f"reach={reach_component:.2f} quality={quality:.2f} "
              f"crowdedness={crowdedness:.2f} relevance={relevance:.2f} "
              f"({reach_rate:.0f} views/min at {age:.1f}m)")
    return ScoreResult(_clip01(score), reason, None)
