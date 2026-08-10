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
    # never harvest our own approval bot's chat: its drafts would flow back into
    # tomorrow's digest and the model would start writing about its own output
    exclude = [str(x).lower() for x in (src.get("exclude_chats") or [])]
    # only keep a chat's messages if I wrote something there within the window —
    # skips lurked channels/groups where I never participate
    require_participation = bool(src.get("require_my_participation", True))
    count = 0
    newest = floor

    # identify ourselves clearly in Telegram's "active sessions" list, so a
    # new-login alert from this harvester is recognisable rather than alarming
    with TelegramClient(str(session_file), int(api_id), api_hash,
                        device_model=str(src.get("device_label", "dailypost harvester")),
                        system_version="read-only") as client:
        me = client.get_me()
        for dialog in client.iter_dialogs(limit=max_dialogs):
            if dialog.date and dialog.date < floor:
                continue
            title = dialog.name or str(dialog.id)
            if any(x in title.lower() for x in exclude):
                log.debug("skip excluded chat %s", title)
                continue
            batch = []
            i_participated = False
            for msg in client.iter_messages(dialog.entity, limit=max_msgs):
                if msg.date is None or msg.date <= floor:
                    break
                if not msg.text:
                    continue
                ts = msg.date.astimezone(timezone.utc)
                newest = max(newest, ts)
                mine = bool(msg.out) or msg.sender_id == me.id
                i_participated = i_participated or mine
                batch.append((msg.id, ts, msg.text, "me" if mine else "them"))
            if require_participation and not i_participated:
                log.debug("skip %s: no messages of mine in window", title)
                continue
            for msg_id, ts, text, direction in batch:
                if store.add_item(
                    source="telegram",
                    external_id=f"{dialog.id}:{msg_id}",
                    day=day_of(ts, cfg),
                    ts=ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    kind="message",
                    summary=text[:2000],
                    meta={"chat": title, "direction": direction},
                ):
                    count += 1

    store.set_cursor("telegram", newest.isoformat())
    return count
