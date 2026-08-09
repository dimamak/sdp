"""WAHA webhook receiver: WhatsApp messages → store. Localhost only.

Mounted inside the bot service process (see main.py). WAHA is configured to
POST `message` events to http://127.0.0.1:<webhook_port>/waha.
Read-only capture: we never send anything through WAHA.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request

from ..util import day_of, get_logger

log = get_logger("bot.waha")


def build_app(cfg, store) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    expected_key = cfg.secret("WAHA_API_KEY")

    @app.post("/waha")
    async def waha(request: Request, x_api_key: str | None = Header(default=None)):
        if expected_key and x_api_key != expected_key:
            raise HTTPException(403)
        data = await request.json()
        if data.get("event") != "message":
            return {"ok": True}
        p = data.get("payload") or {}
        body = p.get("body") or ""
        if not body.strip():
            return {"ok": True}
        ts = datetime.fromtimestamp(int(p.get("timestamp", 0)) or 0, tz=timezone.utc)
        direction = "me" if p.get("fromMe") else "them"
        chat = p.get("from") if not p.get("fromMe") else p.get("to")
        inserted = store.add_item(
            source="whatsapp",
            external_id=str(p.get("id") or f"{chat}:{p.get('timestamp')}"),
            day=day_of(ts, cfg),
            ts=ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            kind="message",
            summary=body[:2000],
            meta={"chat": str(chat), "direction": direction,
                  "notify_name": p.get("_data", {}).get("notifyName")},
        )
        if inserted:
            log.info("whatsapp message stored (%s)", direction)
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
