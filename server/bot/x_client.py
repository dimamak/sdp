"""X (Twitter) publish client — OAuth 1.0a user context.

Unlike LinkedIn, X needs no token file and no expiry tracking: the four keys
below are generated once and pasted into .env. They don't rotate on their
own; only a manual "revoke" in the portal kills them.

    X_API_KEY / X_API_SECRET             — the App's key pair
    X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET — this account's access token pair,
        generated AFTER the App's permissions are set to "Read and write" —
        an access token generated before that stays read-only forever and
        every post 403s (see _fail's hint below).

Two ways to get the access token pair, same pattern as LinkedIn's shared-App
setup (server/bot/linkedin_auth.py): the App owner can auto-generate one
directly in the developer portal for their own account, for free; anyone
else posting through the SAME App (a second person sharing the App owner's
server instance) needs their own token for their own account, gotten via the
PIN-based OAuth flow in server/bot/x_auth.py.

POST /2/tweets accepts OAuth 1.0a user context, and the v1.1 media/upload
endpoint effectively requires it, so this one credential type covers both
text and image posts — no separate OAuth2 dance the way LinkedIn needs one.

Never auto-publishes — post() is only called by the bot after an explicit
"Post to X" tap, itself only reachable after the LinkedIn post already went
out (see server/bot/main.py Bot.start_x/publish_x).

Probes that don't touch your timeline:
    python -m server.bot.x_client --dry-run "some post text"
    python -m server.bot.x_client --check
    python -m server.bot.x_client --test-post --delete-after
"""
from __future__ import annotations

import json
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

from ..util import get_logger

log = get_logger("bot.x")

API = "https://api.twitter.com"
UPLOAD_API = "https://upload.twitter.com"

CONTENT_TYPE_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".png": "image/png", ".gif": "image/gif"}

SECRET_KEYS = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


def tweet_url(tweet_id: str) -> str:
    tweet_id = (tweet_id or "").strip()
    return f"https://x.com/i/status/{tweet_id}" if tweet_id and tweet_id != "DRYRUN" else ""


def _fail(r: requests.Response, what: str) -> None:
    """Raise with X's own explanation, which is usually specific."""
    try:
        j = r.json()
        detail = (j.get("detail") or j.get("title")
                 or (j.get("errors") or [{}])[0].get("message") or r.text[:400])
    except ValueError:
        detail = r.text[:400]
    hint = ""
    if r.status_code == 401:
        hint = " — check X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET in .env"
    elif r.status_code == 403:
        hint = (" — the access token may be read-only: regenerate it in the developer portal "
               "AFTER setting App permissions to 'Read and write'")
    raise RuntimeError(f"{what} failed {r.status_code}: {detail}{hint}")


class XClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def configured(self) -> bool:
        return all(self.cfg.secret(k) for k in SECRET_KEYS)

    def _auth(self) -> OAuth1:
        if not self.configured():
            raise RuntimeError(
                f"{', '.join(SECRET_KEYS)} not set in .env — run: python -m setup.wizard --source x")
        return OAuth1(
            self.cfg.secret("X_API_KEY"),
            client_secret=self.cfg.secret("X_API_SECRET"),
            resource_owner_key=self.cfg.secret("X_ACCESS_TOKEN"),
            resource_owner_secret=self.cfg.secret("X_ACCESS_TOKEN_SECRET"),
        )

    # ---- media upload ---------------------------------------------------------

    def upload_media(self, path: Path, alt_text: str | None = None) -> str:
        """Simple (non-chunked) v1.1 upload, good for the small illustrations this
        pipeline renders. Returns the media_id_string for POST /2/tweets."""
        path = Path(path)
        data = path.read_bytes()
        content_type = CONTENT_TYPE_BY_EXT.get(path.suffix.lower(), "image/jpeg")
        r = requests.post(f"{UPLOAD_API}/1.1/media/upload.json", auth=self._auth(), timeout=120,
                          files={"media": (path.name, data, content_type)})
        if r.status_code >= 300:
            _fail(r, "media upload")
        media_id = r.json()["media_id_string"]
        if alt_text:
            # best-effort: alt text failing is not worth losing the post over
            meta = requests.post(f"{UPLOAD_API}/1.1/media/metadata/create.json",
                                 auth=self._auth(), timeout=30,
                                 json={"media_id": media_id,
                                       "alt_text": {"text": alt_text.strip()[:1000]}})
            if meta.status_code >= 300:
                log.warning("media alt-text metadata failed %s: %s",
                           meta.status_code, meta.text[:200])
        log.info("uploaded %s (%d bytes) -> %s", path.name, len(data), media_id)
        return media_id

    # ---- publishing ------------------------------------------------------------

    def post(self, text: str, image_path: Path | str | None = None,
             alt_text: str | None = None, *, dry_run: bool = False) -> tuple[str, str | None]:
        """Publish a tweet, optionally with one image. Returns (tweet_id, media_id)."""
        if dry_run or self.cfg.get("x.dry_run", False):
            body = {"text": text}
            media_id = "urn:x:media:DRYRUN" if image_path else None
            if media_id:
                body["media"] = {"media_ids": [media_id]}
            log.info("dry-run POST %s/2/tweets\n%s", API, json.dumps(body, indent=2, ensure_ascii=False))
            return "DRYRUN", media_id

        media_id = None
        if image_path:
            try:
                media_id = self.upload_media(Path(image_path), alt_text)
            except Exception as e:
                # a media hiccup is not worth losing the post over
                log.warning("X media upload failed (%s) — posting text-only", e)
        body = {"text": text}
        if media_id:
            body["media"] = {"media_ids": [media_id]}
        r = requests.post(f"{API}/2/tweets", auth=self._auth(), json=body, timeout=30)
        if r.status_code >= 300:
            _fail(r, "post")
        tweet_id = r.json()["data"]["id"]
        log.info("posted: %s%s", tweet_id, f" with {media_id}" if media_id else "")
        return tweet_id, media_id

    def delete_post(self, tweet_id: str) -> None:
        """Only used by --test-post --delete-after."""
        r = requests.delete(f"{API}/2/tweets/{tweet_id}", auth=self._auth(), timeout=30)
        if r.status_code >= 300 and r.status_code != 404:
            _fail(r, "delete")

    # ---- subscription status ---------------------------------------------------

    def subscription_type(self) -> str | None:
        """The posting account's X Premium tier ("Basic", "Premium", "PremiumPlus"),
        or None if it has no paid subscription. Requires the `subscription_type`
        user field, which only returns anything under user-context auth (which is
        what this client already uses)."""
        r = requests.get(f"{API}/2/users/me", auth=self._auth(), timeout=30,
                         params={"user.fields": "subscription_type"})
        if r.status_code >= 300:
            _fail(r, "users/me")
        return r.json()["data"].get("subscription_type")


TEST_POST_TEXT = "Social Daily Poster self-test (ignore) — deleting this in a second."


def main(argv=None) -> int:
    import argparse

    from ..config import Config

    ap = argparse.ArgumentParser(description="X (Twitter) publish probes.")
    ap.add_argument("text", nargs="?", default=TEST_POST_TEXT)
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact /2/tweets body; no network at all")
    ap.add_argument("--image", default=None, help="attach this file (with --dry-run or --test-post)")
    ap.add_argument("--check", action="store_true",
                    help="prove the keys authenticate via GET /2/users/me; posts nothing")
    ap.add_argument("--test-post", action="store_true",
                    help="really publish (briefly visible on your timeline)")
    ap.add_argument("--delete-after", action="store_true", help="delete it immediately")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    client = XClient(cfg)

    if not client.configured():
        print(f"{', '.join(SECRET_KEYS)} not set in .env — run: python -m setup.wizard --source x")
        return 1

    if args.dry_run:
        tweet_id, media_id = client.post(args.text, args.image, "smoke test alt text", dry_run=True)
        print(f"\nwould publish -> {tweet_id} media={media_id}")
        return 0

    if args.check:
        r = requests.get(f"{API}/2/users/me", auth=client._auth(), timeout=30)
        if r.status_code >= 300:
            _fail(r, "users/me")
        me = r.json()["data"]
        sub = client.subscription_type()
        print(f"authenticated as @{me['username']} ({me['id']})")
        print(f"subscription: {sub or 'none (free tier, 280-char limit applies)'}")
        return 0

    if args.test_post:
        tweet_id, media_id = client.post(args.text, args.image, "self-test image")
        print(f"published {tweet_id}  {tweet_url(tweet_id)}")
        if args.delete_after:
            client.delete_post(tweet_id)
            print("deleted.")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
