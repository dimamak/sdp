"""Official 'Share on LinkedIn' client (w_member_social).

Token file (JSON, chmod 600): {access_token, expires_at, person_urn}.
Created by `python -m server.bot.linkedin_auth`. Never auto-publishes —
post() is only called by the bot after an explicit Approve tap.

Publishing goes through the versioned REST API (/rest/posts, /rest/images), not
the legacy /v2/ugcPosts, so text posts and image posts share one code path. That
migration brings one trap with it: `commentary` is 'little text', not plain text.
See escape_commentary below.

Probes that don't touch your feed:
    python -m server.bot.linkedin_client --dry-run "some post text"
    python -m server.bot.linkedin_client --check
    python -m server.bot.linkedin_client --test-post --delete-after
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

from ..util import get_logger

log = get_logger("bot.linkedin")

API = "https://api.linkedin.com"
REST = f"{API}/rest"

CONTENT_TYPE_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".png": "image/png", ".gif": "image/gif"}

# Reserved in LinkedIn's 'little text' format. Per the spec these must be escaped
# "even if those characters are not used in one of the supported elements".
_LI_RESERVED = set(r"\|{}@[]()<>#*_~")
# A hashtag must survive as a real hashtag, so it can't just be escaped — it goes
# through the HashtagTemplate instead, whose own {, | and } are syntax.
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)")


def _escape_run(s: str) -> str:
    # One pass, deliberately. Chained str.replace() double-escapes the backslash.
    return "".join(("\\" + c) if c in _LI_RESERVED else c for c in s)


def escape_commentary(text: str) -> str:
    """Plain post text -> /rest/posts `commentary`. Hashtags stay clickable."""
    out, pos = [], 0
    for m in _HASHTAG_RE.finditer(text):
        out.append(_escape_run(text[pos:m.start()]))
        out.append("{hashtag|\\#|" + m.group(1) + "}")
        pos = m.end()
    out.append(_escape_run(text[pos:]))
    return "".join(out)


def feed_url(urn: str) -> str:
    """Browsable permalink for a post URN.

    /rest/posts hands back urn:li:share:N (or urn:li:ugcPost:N), but the feed
    permalink resolves an *activity* URN — the wrapper LinkedIn uses to represent
    a share in the feed. Linking the share URN gives a URL that 404s even though
    the post is live. The numeric id is shared between the two, so swap the type.
    """
    n = urn.rpartition(":")[2]
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{n}/" if n.isdigit() else ""


def _fail(r: requests.Response, what: str) -> None:
    """Raise with LinkedIn's own explanation, which is usually specific."""
    try:
        j = r.json()
        detail = j.get("message") or j.get("error_description") or r.text[:400]
        code = j.get("serviceErrorCode")
    except ValueError:
        detail, code = r.text[:400], None
    hint = ""
    if code in (65600, 65601) or r.status_code == 401:
        hint = " — re-run: python -m server.bot.linkedin_auth"
    raise RuntimeError(
        f"{what} failed {r.status_code}{f' [{code}]' if code else ''}: {detail}{hint}")


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

    # ---- headers -------------------------------------------------------------

    def _rest_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()['access_token']}",
            "LinkedIn-Version": str(self.cfg.get("linkedin.api_version", "202506")),
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    # ---- image upload --------------------------------------------------------

    def _init_image_upload(self) -> tuple[str, str]:
        """Register an upload. Returns (upload_url, image_urn).

        Also the cheapest proof that this app has versioned-API access at all —
        see --check. An unused upload URL just expires.
        """
        r = requests.post(f"{REST}/images?action=initializeUpload",
                          headers=self._rest_headers(), timeout=30,
                          json={"initializeUploadRequest": {"owner": self._token()["person_urn"]}})
        if r.status_code >= 300:
            _fail(r, "image initializeUpload")
        value = r.json()["value"]
        return value["uploadUrl"], value["image"]

    def _put_image_bytes(self, upload_url: str, data: bytes, content_type: str) -> None:
        """Raw bytes to the upload host.

        Deliberately NOT _rest_headers(): this is not a /rest/ resource, and
        sending LinkedIn-Version / X-Restli-Protocol-Version / application/json
        here is a reliable way to lose an afternoon.
        """
        r = requests.put(upload_url, data=data, timeout=120, headers={
            "Authorization": f"Bearer {self._token()['access_token']}",
            "Content-Type": content_type,
        })
        if r.status_code >= 300:
            raise RuntimeError(f"image upload failed {r.status_code}: {r.text[:400]}")

    def upload_image(self, path: Path) -> str:
        """Register + upload one image, returning its urn:li:image: URN.

        Kept as a single call because the upload URL is short-lived — never
        register one and hold on to it.
        """
        path = Path(path)
        data = path.read_bytes()
        content_type = CONTENT_TYPE_BY_EXT.get(path.suffix.lower(), "image/jpeg")
        upload_url, image_urn = self._init_image_upload()
        self._put_image_bytes(upload_url, data, content_type)
        log.info("uploaded %s (%d bytes) -> %s", path.name, len(data), image_urn)
        return image_urn

    # ---- publishing ----------------------------------------------------------

    def _post_body(self, text: str, image_urn: str | None, alt_text: str | None) -> dict:
        commentary = (escape_commentary(text)
                      if self.cfg.get("linkedin.escape_commentary", True) else text)
        body = {
            "author": self._token()["person_urn"],
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_urn:
            # an empty altText is worse than a generic one for screen readers
            body["content"] = {"media": {
                "id": image_urn,
                "altText": (alt_text or "").strip()[:300] or "Illustration for this post",
            }}
        # note: no "content" key at all for text-only — {} or null is a 422
        return body

    def _post_v2(self, text: str) -> str:
        """Legacy /v2/ugcPosts, text only. Only reached by the fallback below."""
        body = {
            "author": self._token()["person_urn"],
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE",
            }},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        }
        headers = {"Authorization": f"Bearer {self._token()['access_token']}",
                   "X-Restli-Protocol-Version": "2.0.0",
                   "Content-Type": "application/json"}
        r = requests.post(f"{API}/v2/ugcPosts", headers=headers, json=body, timeout=30)
        if r.status_code >= 300:
            _fail(r, "ugcPosts post")
        return r.headers.get("x-restli-id") or r.json().get("id", "")

    def post(self, text: str, image_path: Path | str | None = None,
             alt_text: str | None = None, *, dry_run: bool = False) -> tuple[str, str | None]:
        """Publish a post, optionally with an image. Returns (post_urn, image_urn)."""
        if dry_run or self.cfg.get("linkedin.dry_run", False):
            body = self._post_body(text, "urn:li:image:DRYRUN" if image_path else None, alt_text)
            log.info("dry-run POST %s/posts\n%s", REST, json.dumps(body, indent=2, ensure_ascii=False))
            return "urn:li:share:DRYRUN", ("urn:li:image:DRYRUN" if image_path else None)

        image_urn = self.upload_image(Path(image_path)) if image_path else None
        r = requests.post(f"{REST}/posts", headers=self._rest_headers(), timeout=30,
                          json=self._post_body(text, image_urn, alt_text))

        if r.status_code in (403, 404) and image_urn is None \
                and self.cfg.get("linkedin.rest_fallback_v2", True):
            # Some apps hold w_member_social but were never provisioned for the
            # versioned APIs. Rather than go silent, fall back for text and shout.
            # Delete this branch once a real /rest/posts publish has succeeded.
            log.error("/rest/posts refused (%s) — falling back to /v2/ugcPosts. "
                      "This app needs versioned-API access: %s", r.status_code, r.text[:300])
            return self._post_v2(text), None

        if r.status_code >= 300:
            _fail(r, "post")
        urn = r.headers.get("x-restli-id") or r.json().get("id", "")
        log.info("posted: %s%s", urn, f" with {image_urn}" if image_urn else "")
        return urn, image_urn

    def delete_post(self, urn: str) -> None:
        """Only used by --test-post --delete-after."""
        r = requests.delete(f"{REST}/posts/{quote(urn, safe='')}",
                            headers=self._rest_headers(), timeout=30)
        if r.status_code >= 300 and r.status_code != 404:
            _fail(r, "delete")


TEST_POST_TEXT = (r"dailypost self-test (ignore) [1] {2} <3> #selftest *x* _y_ ~z~ @ | \ "
                  "— deleting this in a second.")


def main(argv=None) -> int:
    import argparse

    from ..config import Config

    ap = argparse.ArgumentParser(description="LinkedIn publish probes.")
    ap.add_argument("text", nargs="?", default=TEST_POST_TEXT)
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact /rest/posts body; no network at all")
    ap.add_argument("--image", default=None, help="attach this file (with --dry-run or --test-post)")
    ap.add_argument("--check", action="store_true",
                    help="prove versioned-API access via initializeUpload; publishes nothing")
    ap.add_argument("--test-post", action="store_true",
                    help="really publish (briefly visible on your feed)")
    ap.add_argument("--delete-after", action="store_true", help="delete it immediately")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    client = LinkedInClient(cfg)

    if args.dry_run:
        urn, image_urn = client.post(args.text, args.image, "smoke test alt text", dry_run=True)
        print(f"\nwould publish -> {urn} image={image_urn}")
        return 0

    if args.check:
        upload_url, image_urn = client._init_image_upload()
        print(f"rest/images OK (LinkedIn-Version {cfg.get('linkedin.api_version', '202506')})")
        print(f"  image urn : {image_urn}")
        print(f"  upload url: {upload_url[:80]}…  (discarded, it will just expire)")
        return 0

    if args.test_post:
        urn, image_urn = client.post(args.text, args.image, "self-test image")
        print(f"published {urn}  {feed_url(urn)}")
        if args.delete_after:
            client.delete_post(urn)
            print("deleted.")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
