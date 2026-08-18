"""One-time 3-legged OAuth 1.0a (PIN-based) flow for X (Twitter).

X's developer portal can auto-generate an access token, but only for the
account that owns the App — that shortcut is documented in x_client.py's
docstring and it's all a single-owner instance needs. This script is for
everyone else: it lets a SECOND (or third...) person post through the SAME
App as someone else, exactly like several people already share one LinkedIn
app in this project (server/bot/linkedin_auth.py) — one App's key pair,
several accounts each with their own token.

Needs this instance's own X_API_KEY / X_API_SECRET already in .env — those
are the App's shared keys (copy them from whoever set the App up first, or
from setup.wizard --source x). Writes X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET
into THIS instance's .env once you finish; they are per-account and must not
be copied to another instance.

    python -m server.bot.x_auth [--config PATH]

Run it in a browser signed in as the X account THIS instance should post for
— use a private window if a different account is currently active there.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from requests_oauthlib import OAuth1Session

from ..config import Config

REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"


def _env_set(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    api_key = cfg.secret("X_API_KEY")
    api_secret = cfg.secret("X_API_SECRET")
    if not (api_key and api_secret):
        print("Set X_API_KEY / X_API_SECRET in .env first — the App's own keys, "
              "shared across everyone posting through it (like LINKEDIN_CLIENT_ID).")
        return 1

    oauth = OAuth1Session(api_key, client_secret=api_secret, callback_uri="oob")
    try:
        fetch_response = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except Exception as e:
        print(f"request_token failed: {e}")
        return 1
    resource_owner_key = fetch_response["oauth_token"]
    resource_owner_secret = fetch_response["oauth_token_secret"]

    print(f"\nOpen this URL, sign in as the X account THIS instance should post for, "
          f"and approve the app:\n\n{oauth.authorization_url(AUTHORIZE_URL)}\n")
    pin = input("Paste the PIN X shows you: ").strip()
    if not pin:
        print("no PIN entered, aborting")
        return 1

    oauth = OAuth1Session(api_key, client_secret=api_secret,
                          resource_owner_key=resource_owner_key,
                          resource_owner_secret=resource_owner_secret,
                          verifier=pin)
    try:
        tokens = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception as e:
        print(f"access_token exchange failed: {e}")
        print("PINs are single-use and short-lived — rerun for a fresh one.")
        return 1

    access_token = tokens["oauth_token"]
    access_secret = tokens["oauth_token_secret"]
    screen_name = tokens.get("screen_name", "?")

    env_path = cfg.path.parent / ".env"
    _env_set(env_path, "X_ACCESS_TOKEN", access_token)
    _env_set(env_path, "X_ACCESS_TOKEN_SECRET", access_secret)
    print(f"\nSaved to {env_path} — authorized as @{screen_name}.")
    print("If that is not the account this instance should post for, sign out of "
          "X (or use a private window) and rerun this — a token for the wrong "
          "account is a silent mistake that only shows up later, as posts on "
          "someone else's timeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
