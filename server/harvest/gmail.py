"""Gmail digest harvester: last-24h sent+received subjects + snippets.

Meeting-notetaker emails (Fathom / tl;dv / Tactiq — configurable sender list)
are kept with a larger body excerpt since they carry meeting transcripts.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

from . import register
from ..util import day_of, get_logger

log = get_logger("harvest.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _client(token_file: Path, credentials_file: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload: dict, limit: int = 8000) -> str:
    """Best-effort plain-text extraction."""
    stack, out = [payload], []
    while stack and sum(len(t) for t in out) < limit:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data:
            out.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
        stack.extend(part.get("parts", []))
    return "\n".join(out)[:limit]


@register("gmail")
def collect(src, cfg, store, since) -> int:
    token_file = Path(str(src["token_file"])).expanduser()
    credentials_file = Path(str(src.get("credentials_file", ""))).expanduser()
    if not token_file.exists():
        log.warning("gmail: token file missing (%s) — run the setup wizard", token_file)
        return 0

    svc = _client(token_file, credentials_file)
    transcript_senders = [s.lower() for s in src.get("transcript_senders", [])]
    after = int(since.timestamp())
    count = 0
    page_token = None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=f"after:{after}", maxResults=100, pageToken=page_token
        ).execute()
        for ref in resp.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "To", "Date"],
            ).execute()
            sender = _header(msg, "From")
            subject = _header(msg, "Subject")
            ts = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
            is_transcript = any(t in sender.lower() for t in transcript_senders)
            summary = f"From: {sender}\nSubject: {subject}\n{msg.get('snippet', '')}"
            if is_transcript:
                full = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
                summary = f"MEETING TRANSCRIPT EMAIL\nFrom: {sender}\nSubject: {subject}\n" + _body_text(full.get("payload", {}))
            if store.add_item(
                source="gmail",
                external_id=ref["id"],
                day=day_of(ts, cfg),
                ts=ts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                kind="meeting_transcript" if is_transcript else "email",
                summary=summary,
                meta={"from": sender, "subject": subject},
            ):
                count += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return count
