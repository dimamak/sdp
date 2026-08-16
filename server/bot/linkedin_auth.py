"""One-time 3-legged OAuth for Share on LinkedIn.

Run on a machine with a browser (e.g. the laptop, with the repo cloned and the
same .env), or on the server with --no-browser (prints the URL; paste the
redirect URL back). Saves the token file configured at linkedin.token_file.

    python -m server.bot.linkedin_auth [--port 8917] [--no-browser] [--config PATH]

LinkedIn app requirements: products "Share on LinkedIn" + "Sign In with LinkedIn
using OpenID Connect"; redirect URL http://localhost:<port>/callback registered.
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from ..config import Config

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPES = "openid profile w_member_social"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8917)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--config", default=None)
    # The interactive paste-back assumes the person authorizing is sitting at
    # this terminal. When they are not — a teammate on their own laptop, or the
    # app already registered so only their consent is missing — these two split
    # it into steps that can be sent and answered separately.
    ap.add_argument("--print-url", action="store_true",
                    help="print the authorization URL and exit (send it to whoever "
                         "is authorizing; they need no app of their own)")
    ap.add_argument("--exchange", metavar="REDIRECT_URL_OR_CODE", default=None,
                    help="finish auth from the pasted redirect URL (or a bare code)")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    client_id = cfg.secret("LINKEDIN_CLIENT_ID")
    client_secret = cfg.secret("LINKEDIN_CLIENT_SECRET")
    if not (client_id and client_secret):
        print("Set LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET in .env first.")
        return 1

    redirect_uri = f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "state": state, "scope": SCOPES,
    })

    token_file = cfg.path_of("linkedin.token_file")

    if args.print_url:
        print(url)
        print()
        print(f"Open it signed in as the person this instance posts for "
              f"(use a private window if another account is active).")
        print("Approve, then copy the whole localhost URL from the address bar "
              "— it will not load, that is expected — and run:")
        print(f'  python -m server.bot.linkedin_auth --exchange "<that URL>"')
        print(f"Token will be written to: {token_file}")
        return 0

    code_holder = {}
    if args.exchange:
        pasted = args.exchange.strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        if "error" in q:
            print(f"authorization was refused: {q.get('error_description', q['error'])[0]}")
            return 1
        # accept a bare code too, so pasting just the value works
        code_holder["code"] = q["code"][0] if "code" in q else pasted
    elif args.no_browser:
        print(f"\nOpen this URL in any browser:\n\n{url}\n")
        pasted = input("Paste the FULL redirect URL you were sent to: ").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code_holder["code"] = q["code"][0]
    else:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in q and q.get("state", [""])[0] == state:
                    code_holder["code"] = q["code"][0]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"dailypost: LinkedIn authorized. You can close this tab.")
                else:
                    self.send_response(400)
                    self.end_headers()
            def log_message(self, *a):  # quiet
                pass

        srv = http.server.HTTPServer(("localhost", args.port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Opening browser for LinkedIn consent (redirect on localhost:{args.port})...")
        webbrowser.open(url)
        while "code" not in code_holder:
            time.sleep(0.5)
        srv.shutdown()

    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code_holder["code"],
        "redirect_uri": redirect_uri, "client_id": client_id, "client_secret": client_secret,
    }, timeout=30)
    if r.status_code >= 300:
        # by far the most common cause: the code was already used, or expired
        print(f"token exchange failed {r.status_code}: {r.text[:300]}")
        print("Authorization codes are single-use and short-lived — "
              "run --print-url again for a fresh one.")
        return 1
    tok = r.json()

    ui = requests.get("https://api.linkedin.com/v2/userinfo",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    ui.raise_for_status()
    sub = ui.json()["sub"]

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({
        "access_token": tok["access_token"],
        "expires_at": time.time() + tok.get("expires_in", 0),
        "refresh_token": tok.get("refresh_token"),
        "person_urn": f"urn:li:person:{sub}",
    }))
    token_file.chmod(0o600)
    print(f"Token saved to {token_file} (expires in {tok.get('expires_in', 0)//86400} days). "
          f"Author: urn:li:person:{sub}")

    # Authorizing while signed in as the wrong person is silent and only shows up
    # later, as posts on someone else's feed. Say whose account this is.
    for other in sorted(Path("/opt/dailypost").glob("*/linkedin-token.json")):
        if other.resolve() == token_file.resolve():
            continue
        try:
            them = json.loads(other.read_text()).get("person_urn")
        except Exception:
            continue
        if them == f"urn:li:person:{sub}":
            print(f"WARNING: this is the SAME account as {other} — you were signed "
                  "in as that person. Sign out (or use a private window) and redo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
