"""Telegram personal-history harvester (Telethon / MTProto).

Requires a one-time interactive login (setup wizard) that creates the session
file. Nightly: walk recent dialogs, pull messages newer than the cursor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import register
from ..util import day_of, get_logger

log = get_logger("harvest.telegram")


@register("telegram")
def collect(src, cfg, store, since) -> int:
    from telethon.sync import TelegramClient  # imported lazily

    session_file = Path(str(src["session_file"])).expanduser()
    api_id = cfg.secret("TG_API_ID")
    api_hash = cfg.secret("TG_API_HASH")
    if not (api_id and api_hash):
        log.warning("telegram: TG_API_ID/TG_API_HASH not set — skipping")
        return 0
    if not session_file.exists():
        log.warning("telegram: session file missing (%s) — run the setup wizard", session_file)
        return 0

    cursor = store.get_cursor("telegram")
    floor = datetime.fromisoformat(cursor) if cursor else since
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=timezone.utc)

    max_dialogs = int(src.get("max_dialogs", 40))
    max_msgs = int(src.get("max_messages_per_dialog", 300))
    count = 0
    newest = floor

    with TelegramClient(str(session_file), int(api_id), api_hash) as client:
        me = client.get_me()
        for dialog in client.iter_dialogs(limit=max_dialogs):
            if dialog.date and dialog.date < floor:
                continue
            title = dialog.name or str(dialog.id)
            for msg in client.iter_messages(dialog.entity, limit=max_msgs):
                if msg.date is None or msg.date <= floor:
                    break
                if not msg.text:
                    continue
                ts = msg.date.astimezone(timezone.utc)
                newest = max(newest, ts)
                direction = "me" if msg.sender_id == me.id else "them"
                if store.add_item(
                    source="telegram",
                    external_id=f"{dialog.id}:{msg.id}",
                    day=day_of(ts, cfg),
                    ts=ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    kind="message",
                    summary=msg.text[:2000],
                    meta={"chat": title, "direction": direction},
                ):
                    count += 1

    store.set_cursor("telegram", newest.isoformat())
    return count
