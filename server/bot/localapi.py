"""Local HTTP API for the X reply radar's browser extension (plan.md §3).

Started in its own daemon thread from the bot process, bound to **127.0.0.1
only** in both laptop and server mode — server mode reaches it through an SSH
tunnel (`ssh -N -L 8765:127.0.0.1:8765 ...`), never a public port. The
extension's service worker is the only client; the content script never talks
to this directly (Private Network Access rules make a direct https-page →
localhost fetch fragile, see plan.md §3 "Localhost access").

Verified empirically (plan.md §3, 2026-09-01): a bare, header-free JSON
endpoint answers service-worker fetches from an extension with the right
`host_permissions` with no preflight, no CORS headers, and no LNA prompt.
Adding CORS/PNA headers here would be cargo cult, not a fix —
test_radar_localapi.py asserts none are ever sent.

Read-only w.r.t. X: this module never imports server.bot.x_client and has no
path to XClient.post — the extension only ever reports what it already saw
on the page and reads back a suggestion.
"""
from __future__ import annotations

import hmac
import json
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request

from ..radar import pipeline, watchlist
from ..radar.hours import in_active_hours
from ..radar.score import age_minutes
from ..store import Store
from ..util import get_logger

log = get_logger("bot.localapi")

# A batch of scraped tweets is a few KB at most; this is generous headroom
# against a misbehaving or compromised page, not a real traffic limit.
MAX_BODY_BYTES = 64_000


async def _read_capped(request: Request) -> bytes:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(413, "payload too large")
    return body


def _ingest_one(cfg, store, raw: dict, now: datetime, notify) -> dict | None:
    """Score/draft one extension-sighted post, unless it's already known.

    X virtualizes the timeline (plan.md §5) — scrolling back over a post
    re-renders the same DOM node with the same counts, so a re-sighting is
    not a fresh observation and reprocessing it would just waste an LLM call
    (and eat into the daily/hourly caps) for nothing.
    """
    post_id = str(raw.get("id") or "")
    if not post_id or not raw.get("text") or not raw.get("created_at"):
        return None
    if store.get_radar_post(post_id) is not None:
        return None

    post = {
        "id": post_id,
        "author_handle": raw.get("author_handle", ""),
        "author_id": raw.get("author_id"),
        "text": raw.get("text", ""),
        "created_at": raw.get("created_at"),
        "views": int(raw.get("views") or 0),
        "likes": int(raw.get("likes") or 0),
        "reposts": int(raw.get("reposts") or 0),
        "replies": int(raw.get("replies") or 0),
        "is_own": bool(raw.get("is_own")),
        "is_reply": bool(raw.get("is_reply")),
        "is_repost": bool(raw.get("is_repost")),
    }
    author_row = store.get_radar_author(post["author_id"]) if post["author_id"] else None
    if author_row and author_row["baseline_reach_rate"]:
        post["author_baseline_reach_rate"] = author_row["baseline_reach_rate"]

    age = age_minutes(post, now)
    store.upsert_radar_post(
        post_id, author_id=post["author_id"], author_handle=post["author_handle"],
        text=post["text"], created_at=post["created_at"], lane="extension",
        first_seen_at=now.isoformat(), views=post["views"], replies=post["replies"],
        likes=post["likes"], reposts=post["reposts"],
        reach_rate=post["views"] / age if age else 0.0, state="seen",
    )
    result = pipeline.process(post, cfg, store, now=now)
    if notify and result["decision"] in ("draft", "ask"):
        notify(post, result)

    # Every author the extension sees feeds the watchlist's continuous
    # candidate evaluation (plan.md §2) — on-watchlist or not, scored or
    # discarded, it's still evidence of reach and topic fit.
    if post["author_id"]:
        watchlist.record_sighting(cfg, store, post, now)
        proposal = watchlist.evaluate(cfg, store, now)
        if notify and proposal is not None:
            notify(None, {"decision": f"watchlist_{proposal.kind}", "proposal": proposal})
            watchlist.mark_proposed(store, proposal.candidate["author_id"], now)

    return {"post_id": post_id, "decision": result["decision"]}


def build_app(cfg, store, notify=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def _authorized(authorization: str | None) -> bool:
        expected = cfg.secret("RADAR_LOCAL_TOKEN")
        if not expected:
            return False
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[len("Bearer "):]
        return hmac.compare_digest(provided, expected)

    @app.post("/radar/posts")
    async def radar_posts(request: Request, authorization: str | None = Header(default=None)):
        if not _authorized(authorization):
            raise HTTPException(403)
        body = await _read_capped(request)
        try:
            posts = json.loads(body) if body else []
        except json.JSONDecodeError as e:
            raise HTTPException(400, "invalid JSON") from e
        if not isinstance(posts, list):
            raise HTTPException(400, "expected a JSON array of posts")

        now = datetime.now(timezone.utc)
        results = []
        if in_active_hours(cfg, "extension", now):
            for raw in posts:
                if isinstance(raw, dict):
                    out = _ingest_one(cfg, store, raw, now, notify)
                    if out is not None:
                        results.append(out)
        return {"ok": True, "results": results}

    @app.get("/radar/suggestions")
    async def radar_suggestions(ids: str = "", authorization: str | None = Header(default=None)):
        if not _authorized(authorization):
            raise HTTPException(403)
        out = []
        for post_id in (i.strip() for i in ids.split(",")):
            if not post_id:
                continue
            row = store.get_radar_post(post_id)
            if row is None or row["state"] not in ("asking", "suggested"):
                continue
            item = {"post_id": post_id, "state": row["state"]}
            if row["state"] == "asking":
                item["question"] = row["pending_question"]
            else:
                rep = store.latest_radar_reply(post_id, status="ready")
                if rep is not None:
                    item["reply"] = rep["text"]
            out.append(item)
        return {"suggestions": out}

    @app.post("/radar/replied")
    async def radar_replied(request: Request, authorization: str | None = Header(default=None)):
        if not _authorized(authorization):
            raise HTTPException(403)
        body = await _read_capped(request)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            raise HTTPException(400, "invalid JSON") from e
        post_id = str(data.get("post_id") or "")
        row = store.get_radar_post(post_id) if post_id else None
        if row is not None:
            rep = store.latest_radar_reply(post_id)
            if rep is not None:
                store.update_radar_reply(rep["id"], status="replied")
            store.set_radar_post_state(post_id, "replied")
            if row["author_id"]:
                store.mark_radar_author_replied(row["author_id"])
        return {"ok": True}

    return app


def start(cfg, notify=None) -> None:
    """Start the extension's local API thread. No-op unless radar.enabled
    and radar.extension.enabled are both true (mirrors radar/scheduler.py's
    Lane B gate) — called unconditionally from server/bot/main.py."""
    if not (cfg.get("radar.enabled", False) and cfg.get("radar.extension.enabled", True)):
        return
    port = int(cfg.get("radar.extension.port", 8765))
    if not cfg.secret("RADAR_LOCAL_TOKEN"):
        log.warning("radar.extension.enabled but RADAR_LOCAL_TOKEN is not set — "
                    "the extension endpoint will reject every request")

    def run() -> None:
        import uvicorn
        store = Store(cfg.path_of("store_dir"))  # own Store: sqlite per-thread
        app = build_app(cfg, store, notify=notify)
        # Bound to loopback only, in both laptop and server mode (plan.md
        # §11) — server mode reaches this over an SSH tunnel, never a public
        # port. This host is intentionally not a config option.
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    threading.Thread(target=run, daemon=True, name="radar-localapi").start()
    log.info("radar local API listening on 127.0.0.1:%d", port)
