"""Official 'Share on LinkedIn' client (w_member_social).

Token file (JSON, chmod 600): {access_token, expires_at, person_urn}.
Created by `python -m server.bot.linkedin_auth`. Never auto-publishes —
post() is only called by the bot after an explicit Approve tap.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from ..util import get_logger

log = get_logger("bot.linkedin")

API = "https://api.linkedin.com"


class LinkedInClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token_file = cfg.path_of("linkedin.token_file")
        self._tok = None

    def _token(self) -> dict:
        if self._tok is None:
            if not (self.token_file and self.token_file.exists()):
                raise RuntimeError("LinkedIn token file missing — run: python -m server.bot.linkedin_auth")
            self._tok = json.loads(self.token_file.read_text())
        return self._tok

    def days_until_expiry(self) -> int | None:
        try:
            exp = self._token().get("expires_at")
            return int((exp - time.time()) / 86400) if exp else None
        except RuntimeError:
            return None

    def post(self, text: str) -> str:
        """Publish a text post; returns the post URN."""
        tok = self._token()
        headers = {
            "Authorization": f"Bearer {tok['access_token']}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        body = {
            "author": tok["person_urn"],
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        r = requests.post(f"{API}/v2/ugcPosts", headers=headers, json=body, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"LinkedIn post failed {r.status_code}: {r.text[:500]}")
        urn = r.headers.get("x-restli-id") or r.json().get("id", "")
        log.info("posted: %s", urn)
        return urn
