"""Lane B — watchlist search poller (plan.md §9).

Read-only: the only network calls here are GET /2/users/by/username and
GET /2/tweets/search/recent. Deliberately does NOT import server.bot.x_client
(see tests/test_radar_never_posts.py) — a read-only radar module reusing the
exact client class that exposes .post() is one careless refactor away from
being able to call it. OAuth1 is re-derived here from the same four secrets
instead.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1

from ..util import get_logger
from . import spend, watchlist
from .score import age_minutes

log = get_logger("radar.poll")

API = "https://api.twitter.com"
SECRET_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


def configured(cfg) -> bool:
    return all(cfg.secret(k) for k in SECRET_KEYS)


def _auth(cfg) -> OAuth1:
    return OAuth1(
        cfg.secret("X_API_KEY"), client_secret=cfg.secret("X_API_SECRET"),
        resource_owner_key=cfg.secret("X_ACCESS_TOKEN"),
        resource_owner_secret=cfg.secret("X_ACCESS_TOKEN_SECRET"),
    )


def build_query(handles: list[str]) -> str:
    ors = " OR ".join(f"from:{h.lstrip('@')}" for h in handles)
    return f"({ors}) -is:reply -is:retweet"


def resolve_author_id(cfg, store, handle: str) -> str | None:
    """handle -> author_id, cached in radar_authors after the first (billed)
    lookup — plan.md §9: no per-poll expansions, resolved once per account."""
    handle = handle.lstrip("@")
    cached = store.get_radar_author_by_handle(handle)
    if cached is not None:
        return cached["author_id"]
    r = requests.get(f"{API}/2/users/by/username/{handle}", auth=_auth(cfg), timeout=30)
    if r.status_code >= 300:
        log.warning("could not resolve @%s: %s %s", handle, r.status_code, r.text[:200])
        return None
    author_id = r.json()["data"]["id"]
    spend.record(store, "user_read", note=f"resolve @{handle}")
    store.upsert_radar_author(author_id, handle=handle)
    return author_id


def poll_once(cfg, store, now: datetime | None = None) -> list[dict]:
    """One Lane B poll. Returns freshly-sighted posts, shaped like the fixture
    posts score.py/reply.py already expect. Empty and free whenever the
    watchlist is empty, the budget is exhausted, or credentials aren't set —
    Lane B degrades, it never breaks the radar (plan.md §11)."""
    now = now or datetime.now(timezone.utc)
    handles = watchlist.effective_watchlist(cfg, store)
    if not handles or not configured(cfg) or spend.status(cfg, store, now).blocked:
        return []

    author_by_id = {}
    for h in handles:
        aid = resolve_author_id(cfg, store, h)
        if aid:
            author_by_id[aid] = h
    if not author_by_id:
        return []

    since_id = store.get_cursor("radar_x_search")
    params = {
        "query": build_query(handles),
        "max_results": 25,
        "tweet.fields": "created_at,public_metrics,conversation_id,author_id",
    }
    if since_id:
        params["since_id"] = since_id
    r = requests.get(f"{API}/2/tweets/search/recent", auth=_auth(cfg), params=params, timeout=30)
    if r.status_code >= 300:
        log.warning("search/recent failed %s: %s", r.status_code, r.text[:300])
        return []
    tweets = r.json().get("data") or []
    if not tweets:
        return []

    spend.record(store, "post_read", units=len(tweets), note="search/recent")
    store.set_cursor("radar_x_search", str(max(int(t["id"]) for t in tweets)))

    posts = []
    for t in tweets:
        author = author_by_id.get(t.get("author_id"))
        if author is None:
            continue  # matched the query but isn't one of our resolved handles
        metrics = t.get("public_metrics") or {}
        views = int(metrics.get("impression_count", 0))
        post = {
            "id": t["id"], "author_handle": author, "author_id": t["author_id"],
            "text": t.get("text", ""), "created_at": t["created_at"],
            "views": views, "likes": int(metrics.get("like_count", 0)),
            "reposts": int(metrics.get("retweet_count", 0)),
            "replies": int(metrics.get("reply_count", 0)),
            "is_own": False, "is_reply": False, "is_repost": False,
        }
        author_row = store.get_radar_author(t["author_id"])
        if author_row and author_row["baseline_reach_rate"]:
            post["author_baseline_reach_rate"] = author_row["baseline_reach_rate"]
        store.upsert_radar_post(
            t["id"], author_id=t["author_id"], author_handle=author, text=post["text"],
            created_at=t["created_at"], lane="api", first_seen_at=now.isoformat(),
            views=views, replies=post["replies"], likes=post["likes"], reposts=post["reposts"],
            reach_rate=views / age_minutes(post, now) if age_minutes(post, now) else 0.0,
            state="seen",
        )
        # Keeps a watchlist incumbent's own stats fresh from its guaranteed
        # Lane B sightings, so a fading incumbent's demotion signal (plan.md
        # §2) is visible even between opportunistic Lane A sightings.
        watchlist.record_sighting(cfg, store, post, now)
        posts.append(post)
    return posts
