"""One-time Gmail OAuth (run where a browser is available, e.g. the laptop).

    python -m setup.gmail_auth --client /path/to/gmail-oauth-client.json --out gmail-token.json

Then copy the token file to the path configured at sources[gmail].token_file
on the server (scp). Scope is read-only.
"""
from __future__ import annotations

import argparse
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, help="OAuth client JSON downloaded from Google Cloud Console")
    ap.add_argument("--out", default="gmail-token.json")
    args = ap.parse_args(argv)

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(args.client, SCOPES)
    creds = flow.run_local_server(port=0)
    out = Path(args.out)
    out.write_text(creds.to_json())
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(f"token saved to {out} — copy it to the server path configured in config.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
