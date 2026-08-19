"""Interactive setup wizard (server side): selects sources AND provisions them.

    python -m setup.wizard              # full guided setup (idempotent, re-runnable)
    python -m setup.wizard --source X   # re-run one step: base|claude|telegram|gmail|
                                        #   whatsapp|linkedin|bot|cron|systemd
    python -m setup.wizard --doctor     # health check of everything enabled

Writes config.yaml / .env next to the repo root; never touches code.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Default (single-user) layout. `--instance NAME` switches everything to
# instances/NAME/ so several people can share one server install: separate config,
# secrets, store, cron entry, bot service and WAHA container per person.
INSTANCE: str | None = None
CONFIG = REPO / "config.yaml"
ENV = REPO / ".env"


def use_instance(name: str | None) -> None:
    global INSTANCE, CONFIG, ENV
    INSTANCE = name
    if name:
        d = REPO / "instances" / name
        d.mkdir(parents=True, exist_ok=True)
        CONFIG, ENV = d / "config.yaml", d / ".env"
    else:
        CONFIG, ENV = REPO / "config.yaml", REPO / ".env"


def unit_name() -> str:
    return f"dailypost-bot@{INSTANCE}" if INSTANCE else "dailypost-bot"


def waha_container() -> str:
    # docker project/container names must be lowercase
    return f"dailypost-waha-{INSTANCE.lower()}" if INSTANCE else "dailypost-waha"


def ports_used_by_other_instances() -> set[int]:
    """Ports already claimed by other instances on this server, so a new one
    never defaults onto a neighbour's WAHA container or webhook listener."""
    used: set[int] = set()
    for other in [REPO / "config.yaml", *(REPO / "instances").glob("*/config.yaml")]:
        if not other.exists() or (CONFIG.exists() and other.resolve() == CONFIG.resolve()):
            continue
        try:
            d = yaml.safe_load(other.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for s in d.get("sources", []) or []:
            if s.get("type") != "whatsapp":
                continue
            if s.get("webhook_port"):
                used.add(int(s["webhook_port"]))
            url = str(s.get("waha_url", ""))
            if ":" in url.rsplit("/", 1)[-1]:
                try:
                    used.add(int(url.rsplit(":", 1)[1]))
                except ValueError:
                    pass
    return used

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def ok(msg):
    print(f"  {GREEN}✔{RESET} {msg}")


def bad(msg):
    print(f"  {RED}✘{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}!{RESET} {msg}")


def ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def yes(prompt: str, default: bool = True) -> bool:
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v.startswith("y")


# ---------- config/env file helpers ------------------------------------------

def load_data() -> dict:
    if CONFIG.exists():
        return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    # Fresh setup: the example is a documentation template, not a starting state.
    # Its `sources` carry placeholder paths AND enabled flags — inheriting them
    # silently enabled an unfiltered claude_projects_dir over a shared host.
    data = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8")) or {}
    data["sources"] = []
    for k in ("install_root", "store_dir", "ingest_dir", "logs_dir", "run_as_user"):
        data.pop(k, None)
    # Credential paths must never be inherited either. A second instance that
    # kept the example's linkedin.token_file pointed at the FIRST instance's
    # store: it posted to that person's account, and had its own OAuth ever
    # completed it would have overwritten their token.
    for section, key in (("linkedin", "token_file"), ("image", "token_file")):
        if isinstance(data.get(section), dict):
            data[section].pop(key, None)
    return data


def save_data(data: dict) -> None:
    CONFIG.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    ok(f"saved {CONFIG}")


def env_set(key: str, value: str) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass


def env_get(key: str) -> str | None:
    if not ENV.exists():
        return None
    for l in ENV.read_text().splitlines():
        if l.startswith(f"{key}="):
            return l.split("=", 1)[1].strip() or None
    return None


def fix_owner(data: dict, *paths) -> None:
    """When the wizard runs as root, hand provisioned files to the run-as user —
    otherwise the pipeline/bot (running as that user) can't read or write them."""
    user = data.get("run_as_user")
    if not user or os.geteuid() != 0:
        return
    import pwd
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return
    for p in paths:
        p = Path(os.path.expanduser(str(p)))
        if not p.exists():
            continue
        os.chown(p, pw.pw_uid, pw.pw_gid)
        if p.is_dir():  # dirs are created empty here, but be safe on re-runs
            for child in p.rglob("*"):
                try:
                    os.chown(child, pw.pw_uid, pw.pw_gid)
                except OSError:
                    pass


def crontab_cmd(data: dict, *args) -> list[str]:
    """Edit the run-as user's crontab, not root's, when the wizard runs as root."""
    user = data.get("run_as_user")
    if user and os.geteuid() == 0 and user != "root":
        return ["crontab", "-u", user, *args]
    return ["crontab", *args]


def get_source(data: dict, type_: str) -> dict | None:
    for s in data.get("sources", []):
        if s.get("type") == type_:
            return s
    return None


def upsert_source(data: dict, src: dict) -> None:
    sources = data.setdefault("sources", [])
    for i, s in enumerate(sources):
        if s.get("type") == src["type"] and s.get("name") == src.get("name"):
            sources[i] = src
            return
    sources.append(src)


# ---------- steps -------------------------------------------------------------

def step_base(data: dict) -> None:
    print("\n== Base ==")
    # Every instance needs its OWN store: a shared dailypost.db would mix two
    # people's items and drafts together.
    suffix = f"/{INSTANCE}" if INSTANCE else ""
    data["install_root"] = ask("install root", data.get("install_root") or f"/opt/dailypost{suffix}")
    root = data["install_root"]
    if INSTANCE and not root.rstrip("/").endswith(f"/{INSTANCE}"):
        warn(f"install root does not include the instance name — make sure it is not "
             f"shared with another instance")
    data["store_dir"] = ask("store dir", data.get("store_dir") or f"{root}/data")
    data["ingest_dir"] = ask("ingest dir", data.get("ingest_dir") or f"{root}/ingest")
    data["logs_dir"] = ask("logs dir", data.get("logs_dir") or f"{root}/logs")
    # Never default to root: this user owns the store, the credentials and the bot
    # process. On a server that already has an instance, reuse that user — it is
    # the one holding the Claude credentials the drafting step needs.
    default_user = data.get("run_as_user")
    if not default_user and (REPO / "config.yaml").exists():
        try:
            default_user = (yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
                            or {}).get("run_as_user")
        except Exception:
            default_user = None
    default_user = default_user or os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    if default_user in ("", "root"):
        default_user = "dailypost"
    data["run_as_user"] = ask("run-as user (non-root; owns the store and runs the bot)",
                              default_user)
    if data["run_as_user"] == "root":
        warn("running as root is not recommended — the store holds transcripts and tokens")
    pl = data.setdefault("pipeline", {})
    pl["timezone"] = ask("timezone", pl.get("timezone", "UTC"))
    pl["cron_utc"] = ask("nightly cron (UTC, crontab syntax)", pl.get("cron_utc", "30 0 * * *"))
    tags = ask("hashtags to always include on X posts, comma-separated, no '#' (blank = none)",
              ",".join(pl.get("always_hashtags", [])))
    pl["always_hashtags"] = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
    dirs = [data["store_dir"], f"{data['ingest_dir']}/laptop", data["logs_dir"]]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    # created as root when the wizard needs root for systemd; hand them to the
    # user that actually runs the pipeline, or nothing can write the store
    fix_owner(data, *dirs, data["install_root"], CONFIG, ENV)
    ok(f"directories created (owner: {data['run_as_user']})")
    venv = REPO / ".venv"
    if not venv.exists():
        print("  creating venv + installing requirements...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv / "bin" / "pip"), "install", "-q", "-r", str(REPO / "requirements.txt")], check=True)
    ok("venv ready")


def step_claude(data: dict) -> None:
    print("\n== Claude Code sessions ==")
    if yes("harvest a plain per-user projects dir on THIS machine?", False):
        d = ask("projects dir", "~/.claude/projects")
        upsert_source(data, {"type": "claude_projects_dir", "enabled": True,
                             "name": "claude-server-cli", "projects_dir": d})
    if yes("harvest sessions from a shared/multi-user host DB (filtered to you)?", False):
        projects_dir = ask("projects dir holding the JSONL files")
        strategy = ask("filter strategy (all|sql|command|id_file)", "sql")
        fcfg: dict = {"strategy": strategy}
        if strategy == "sql":
            fcfg["db_path"] = ask("path to the platform's SQLite DB")
            print("  Your SQL must return claude session-id rows. Named params allowed;")
            print("  $SINCE inside params is replaced with the window start timestamp.")
            fcfg["query"] = ask("SQL query")
            params = {}
            while True:
                kv = ask("param as key=value (empty when done)")
                if not kv:
                    break
                k, _, v = kv.partition("=")
                params[k.strip()] = v.strip()
            fcfg["params"] = params
            # live test
            try:
                from server.harvest.claude_sessions import resolve_session_ids
                ids = resolve_session_ids(fcfg, "1970-01-01 00:00:00")
                ok(f"filter test: matched {len(ids)} sessions total")
                if not yes("looks right?"):
                    return step_claude(data)
            except Exception as e:
                bad(f"filter test failed: {e}")
                if yes("retry?"):
                    return step_claude(data)
        elif strategy == "command":
            fcfg["command"] = ask("shell command printing one session id per line")
        elif strategy == "id_file":
            fcfg["path"] = ask("path to id file")
        upsert_source(data, {"type": "claude_sessions", "enabled": True, "name": "claude-ide",
                             "projects_dir": projects_dir, "filter": fcfg})
    # laptop ingest is on by default
    upsert_source(data, {"type": "ingest_dir", "enabled": True, "name": "laptop",
                         "path": f"{data['ingest_dir']}/laptop"})
    print("  Laptop side: clone the repo on the laptop and run setup\\wizard_laptop.ps1")


def step_telegram(data: dict) -> None:
    print("\n== Telegram (personal history via Telethon) ==")
    if not yes("enable Telegram harvesting?", get_source(data, "telegram") is not None):
        return
    print("  Get api_id/api_hash at https://my.telegram.org → API development tools")
    env_set("TG_API_ID", ask("TG_API_ID", env_get("TG_API_ID") or ""))
    env_set("TG_API_HASH", ask("TG_API_HASH", env_get("TG_API_HASH") or ""))
    session_file = ask("session file path", f"{data['store_dir']}/telethon.session")
    if Path(os.path.expanduser(session_file)).exists():
        warn(f"a Telethon session already exists at {session_file}")
        if yes("start a FRESH login instead (recommended if this is a new person)?", True):
            Path(os.path.expanduser(session_file)).unlink()
    upsert_source(data, {"type": "telegram", "enabled": True, "session_file": session_file,
                         "max_dialogs": 40, "max_messages_per_dialog": 300,
                         "require_my_participation": yes(
                             "only harvest chats you actively wrote in (recommended)?")})
    if yes("run interactive Telegram login now (SMS/2FA prompt)?"):
        from telethon.sync import TelegramClient
        # device_model/system_version show up in Telegram's active-sessions list
        # and in the "new login" alert — make it obvious what this is
        with TelegramClient(session_file, int(env_get("TG_API_ID")), env_get("TG_API_HASH"),
                            device_model="dailypost harvester",
                            system_version="read-only") as c:
            me = c.get_me()
            ok(f"logged in as {me.first_name} (id {me.id})")
        # An existing session file authorises silently — make a wrong account loud
        if not yes(f"is '{me.first_name}' the right account"
                   f"{f' for {INSTANCE}' if INSTANCE else ''}?", True):
            Path(os.path.expanduser(session_file)).unlink(missing_ok=True)
            bad("session discarded — re-run: python -m setup.wizard "
                f"{f'--instance {INSTANCE} ' if INSTANCE else ''}--source telegram")
            return
        fix_owner(data, session_file)


def step_bot(data: dict) -> None:
    print("\n== Telegram approval bot ==")
    print("  Create a bot with @BotFather if you haven't; paste its token.")
    token_in = ask("TG_BOT_TOKEN (empty = skip for now)", env_get("TG_BOT_TOKEN") or "")
    if not token_in:
        warn("skipped — re-run later with: python -m setup.wizard --source bot")
        return
    env_set("TG_BOT_TOKEN", token_in)
    import requests
    token = env_get("TG_BOT_TOKEN")
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not r.get("ok"):
        bad(f"getMe failed: {r}")
        return
    ok(f"bot @{r['result']['username']} verified")
    chat_id = env_get("TG_ALLOWED_CHAT_ID")
    if not chat_id or yes("(re)detect your chat id?", not chat_id):
        input(f"  Send any message to @{r['result']['username']} now, then press Enter... ")
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
        chats = {u["message"]["chat"]["id"] for u in upd.get("result", []) if "message" in u}
        if len(chats) == 1:
            chat_id = str(chats.pop())
            ok(f"chat id detected: {chat_id}")
        else:
            chat_id = ask("could not auto-detect; enter chat id manually")
        env_set("TG_ALLOWED_CHAT_ID", chat_id)
    data.setdefault("bot", {})["enabled"] = True


def step_gmail(data: dict) -> None:
    print("\n== Gmail ==")
    if not yes("enable Gmail digest?", get_source(data, "gmail") is not None):
        return
    token_file = ask("token file path", f"{data['store_dir']}/gmail-token.json")
    upsert_source(data, {"type": "gmail", "enabled": True, "token_file": token_file,
                         "credentials_file": f"{data['store_dir']}/gmail-oauth-client.json",
                         "transcript_senders": ["@fathom.video", "@tldv.io", "@tactiq.io"]})
    if Path(os.path.expanduser(token_file)).exists():
        ok("token file already present")
    else:
        print("  OAuth needs a browser. On your laptop (same repo):")
        print("    python -m setup.gmail_auth --client gmail-oauth-client.json --out gmail-token.json")
        print(f"  then copy gmail-token.json to this server at: {token_file}")


def step_whatsapp(data: dict) -> None:
    print("\n== WhatsApp (WAHA, read-only) ==")
    if not yes("enable WhatsApp capture via self-hosted WAHA?", get_source(data, "whatsapp") is not None):
        return
    if not shutil.which("docker"):
        bad("docker not found — install docker first")
        return
    key = env_get("WAHA_API_KEY") or secrets.token_hex(24)
    env_set("WAHA_API_KEY", key)

    taken = ports_used_by_other_instances()

    def first_free(start: int) -> int:
        import socket
        p = start
        while p < start + 100:
            if p not in taken:
                with socket.socket() as s:
                    try:
                        s.bind(("127.0.0.1", p))
                        return p
                    except OSError:
                        pass
            p += 1
        return start

    def current_waha_port() -> int | None:
        # if OUR container already runs, keep its port — otherwise a redo of this
        # step would see the port as "busy" (by WAHA itself) and drift to a new one
        out = subprocess.run(["docker", "port", waha_container(), "3000"],
                             capture_output=True, text=True)
        if out.returncode == 0 and ":" in out.stdout:
            return int(out.stdout.strip().rsplit(":", 1)[1])
        return None

    existing = get_source(data, "whatsapp") or {}
    port = ask("webhook port (localhost)",
               str(existing.get("webhook_port") or first_free(8477)))

    # One WAHA container can host several named sessions (verified on the Core
    # tier), so a second person does not need a second container — just their own
    # session inside the existing one. Each session gets its own webhook URL, so
    # messages still land in the right instance's store.
    running = subprocess.run(
        ["docker", "ps", "--filter", "name=dailypost-waha", "--format", "{{.Names}} {{.Ports}}"],
        capture_output=True, text=True).stdout.strip().splitlines()
    reuse = None
    if running and INSTANCE:
        print("  Existing WAHA container(s) on this server:")
        for line in running:
            print(f"    - {line}")
        if yes("reuse an existing container with your own session (recommended)?", True):
            reuse = running[0].split()[0] if len(running) == 1 else ask("container name")

    session_name = (INSTANCE or "default").lower()
    if reuse:
        out = subprocess.run(["docker", "port", reuse, "3000"], capture_output=True, text=True)
        waha_port = int(out.stdout.strip().rsplit(":", 1)[1])
        container = reuse
        # The shared container already runs with a key — read it from the
        # container's own environment rather than asking for it.
        found = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}", container],
            capture_output=True, text=True).stdout
        shared_key = next((l.split("=", 1)[1] for l in found.splitlines()
                           if l.startswith("WAHA_API_KEY=")), None)
        if shared_key:
            ok("read WAHA_API_KEY from the running container")
        else:
            warn("could not read the key from the container")
            shared_key = ask("WAHA_API_KEY of that container (see its instance .env)", key)
        key = shared_key
        env_set("WAHA_API_KEY", key)
        ok(f"reusing {container} on port {waha_port}, session '{session_name}'")
    else:
        container = waha_container()
        session_name = "default" if not INSTANCE else session_name
        default_port = current_waha_port() or first_free(3000)
        waha_port = int(ask("WAHA API host port (localhost)", str(default_port)))

    # The container reaches the host on the docker bridge gateway, so the webhook
    # receiver must bind there rather than on loopback.
    gw = subprocess.run(
        "ip -4 addr show docker0 | grep -oP 'inet \\K[\\d.]+'",
        shell=True, capture_output=True, text=True).stdout.strip() or "172.17.0.1"

    upsert_source(data, {"type": "whatsapp", "enabled": True,
                         "waha_url": f"http://127.0.0.1:{waha_port}",
                         "webhook_port": int(port), "webhook_host": gw,
                         "container_name": container, "session": session_name})
    if not reuse:
        compose = REPO / "server" / "docker" / "waha.compose.yml"
        env = {**os.environ, "WAHA_API_KEY": key, "WAHA_WEBHOOK_PORT": port,
               "WAHA_PORT": str(waha_port), "WAHA_CONTAINER": container,
               "COMPOSE_PROJECT_NAME": container}
        subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d"], check=True, env=env)
        ok(f"WAHA container up (bound to 127.0.0.1:{waha_port} only — not reachable from the internet)")
    print("  RULES: read-only. Never send through WAHA; don't bulk-backfill history.")
    if yes("pair WhatsApp now (QR code shown right here in the terminal)?"):
        step_pair(data)
    else:
        print("  Pair later with:  python -m setup.wizard --source pair")
        print("  (or via dashboard through an ssh tunnel:")
        print(f"     ssh -L {waha_port}:127.0.0.1:{waha_port} <server>  →  http://localhost:{waha_port}/dashboard")
        print(f"     login: admin / your WAHA_API_KEY from {ENV})")


def step_pair(data: dict) -> None:
    """Pair WhatsApp by rendering the QR in the terminal — no tunnel/browser needed."""
    print("\n== WhatsApp pairing ==")
    import time

    import requests

    src = get_source(data, "whatsapp")
    if not src:
        bad("whatsapp source not configured — run --source whatsapp first")
        return
    base = src["waha_url"]
    sess = src.get("session", "default")
    headers = {"X-Api-Key": env_get("WAHA_API_KEY") or ""}

    def session_status() -> str:
        r = requests.get(f"{base}/api/sessions/{sess}", headers=headers, timeout=10)
        if r.status_code == 404:
            requests.post(f"{base}/api/sessions", headers=headers,
                          json={"name": sess, "start": True}, timeout=30)
            return "STARTING"
        return r.json().get("status", "UNKNOWN")

    def ensure_webhook():
        """Register our receiver on the session itself. The container-level default
        (WHATSAPP_HOOK_URL) does not apply to sessions created via the dashboard,
        which silently leaves messages undelivered."""
        url = f"http://{src.get('webhook_host', '172.17.0.1')}:{src.get('webhook_port', 8477)}/waha"
        body = {"config": {"webhooks": [{"url": url, "events": ["message"]}]}}
        r = requests.put(f"{base}/api/sessions/{sess}", headers=headers, json=body, timeout=30)
        if r.ok:
            ok(f"webhook registered: {url}")
        else:
            bad(f"could not register webhook ({r.status_code}): {r.text[:200]}")

    st = session_status()
    if st == "WORKING":
        ensure_webhook()
        ok("already paired — session WORKING")
        return
    if st in ("STOPPED", "FAILED"):
        requests.post(f"{base}/api/sessions/{sess}/start", headers=headers, timeout=30)

    print("  Open WhatsApp on your phone → Settings → Linked devices → Link a device")
    print("  Waiting for QR (it refreshes automatically; Ctrl-C to abort)...")
    import qrcode
    shown = None
    for _ in range(120):  # ~4 minutes
        st = session_status()
        if st == "WORKING":
            ensure_webhook()
            ok("WhatsApp paired — session WORKING")
            return
        if st == "SCAN_QR_CODE":
            r = requests.get(f"{base}/api/{sess}/auth/qr?format=raw", headers=headers, timeout=10)
            value = r.json().get("value") if r.ok else None
            if value and value != shown:
                shown = value
                qr = qrcode.QRCode(border=1)
                qr.add_data(value)
                qr.make()
                qr.print_ascii(invert=True)
                print("  ↑ scan this; a fresh code is drawn automatically if it expires")
        time.sleep(2)
    warn("timed out — re-run: python -m setup.wizard --source pair")


def step_linkedin(data: dict) -> None:
    print("\n== LinkedIn ==")
    have_id, have_secret = env_get("LINKEDIN_CLIENT_ID"), env_get("LINKEDIN_CLIENT_SECRET")
    if have_id and have_secret:
        # One app serves everyone: client id/secret identify the APPLICATION, while
        # the access token is issued per member. A second person does not need
        # their own app — only their own authorization.
        ok(f"using existing app credentials (client id {have_id[:6]}…)")
        print("  Nothing to create — you only authorize as yourself below.")
    else:
        print("  Create an app at https://developer.linkedin.com (products: 'Share on LinkedIn'")
        print("  + 'Sign In with LinkedIn using OpenID Connect'); add redirect URL")
        print("  http://localhost:8917/callback")
        print("  Sharing an app with another person on this server is fine — paste")
        print("  the SAME client id/secret; the token you get is still yours alone.")
        client_id = ask("LINKEDIN_CLIENT_ID (empty = skip for now)", have_id or "")
        if not client_id:
            warn("skipped — re-run later with: python -m setup.wizard --source linkedin")
            return
        env_set("LINKEDIN_CLIENT_ID", client_id)
        env_set("LINKEDIN_CLIENT_SECRET", ask("LINKEDIN_CLIENT_SECRET", have_secret or ""))
    # Refuse to offer a path outside this instance's store: that is how one
    # instance ends up authenticating — and posting — as another person.
    own_default = f"{data['store_dir']}/linkedin-token.json"
    current = data.get("linkedin", {}).get("token_file")
    if current and not str(current).startswith(str(data["store_dir"]).rstrip("/") + "/"):
        warn(f"configured token_file {current} is outside this instance's store — "
             f"defaulting to {own_default}")
        current = None
    data.setdefault("linkedin", {})["token_file"] = ask("token file path", current or own_default)
    if yes("run the OAuth now? (a URL is shown — open it in your laptop browser, approve,\n"
           "  then paste the localhost redirect URL from the address bar back here)"):
        save_data(data)  # linkedin_auth reads token_file path from config.yaml
        from server.bot.linkedin_auth import main as li_auth
        try:
            if li_auth(["--no-browser"]) == 0:
                fix_owner(data, data["linkedin"]["token_file"])
                ok("LinkedIn authorized")
        except Exception as e:
            bad(f"OAuth failed: {e} — retry with: python -m setup.wizard --source linkedin")
    else:
        print("  Later: python -m setup.wizard --source linkedin  (or python -m server.bot.linkedin_auth)")


def step_x(data: dict) -> None:
    """Post the same day's story to X too, once the LinkedIn post has actually
    published. Optional, like images — skip it here and re-run later."""
    print("\n== X (Twitter) ==")
    print("  Once a LinkedIn post publishes, a separate short X-native rewrite is")
    print("  written and sent for its own approval — nothing here changes what")
    print("  already went to LinkedIn, even if the X step fails or is skipped.")
    x_keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    if all(env_get(k) for k in x_keys):
        ok("using existing X credentials")
    else:
        if env_get("X_API_KEY") and env_get("X_API_SECRET"):
            ok("using existing X_API_KEY / X_API_SECRET (shared App, like LinkedIn) — "
               "just need this account's own access token below")
        else:
            print("  Create an app at https://developer.x.com (a Project + App), or if")
            print("  someone else here already has one, ask them for its API Key/Secret —")
            print("  one App's keys can be shared the same way this project already")
            print("  shares one LinkedIn App across accounts.")
            print("  App settings -> User authentication set up -> App permissions:")
            print("  'Read and write'.")
            key = ask("X_API_KEY (empty = skip for now)", env_get("X_API_KEY") or "")
            if not key:
                data.setdefault("x", {})["enabled"] = False
                warn("skipped — re-run later with: python -m setup.wizard --source x")
                return
            env_set("X_API_KEY", key)
            env_set("X_API_SECRET", ask("X_API_SECRET", env_get("X_API_SECRET") or ""))

        if not (env_get("X_ACCESS_TOKEN") and env_get("X_ACCESS_TOKEN_SECRET")):
            print("  Need an access token for THIS account. Two ways to get one:")
            print("  1) PIN-based OAuth here (works for anyone, including a second")
            print("     person sharing someone else's App) — opens a URL, you sign in")
            print("     as this account and paste back a PIN.")
            print("  2) Paste one already generated in the developer portal — only")
            print("     works for the App's own owner, and ONLY after setting")
            print("     permissions to Read-and-write (an access token generated")
            print("     before that stays read-only forever and every post 403s).")
            if yes("run the PIN-based OAuth flow now? (recommended)"):
                save_data(data)  # x_auth reads X_API_KEY/SECRET from config.yaml's sibling .env
                from server.bot.x_auth import main as x_auth_main
                try:
                    if x_auth_main(["--config", str(CONFIG)]) == 0:
                        ok("X account authorized")
                    else:
                        warn("OAuth did not complete — retry with: python -m server.bot.x_auth")
                except Exception as e:
                    bad(f"OAuth failed: {e} — retry with: python -m server.bot.x_auth")
            else:
                env_set("X_ACCESS_TOKEN", ask("X_ACCESS_TOKEN", env_get("X_ACCESS_TOKEN") or ""))
                env_set("X_ACCESS_TOKEN_SECRET",
                       ask("X_ACCESS_TOKEN_SECRET", env_get("X_ACCESS_TOKEN_SECRET") or ""))

    x = data.setdefault("x", {})
    x["enabled"] = True
    x.setdefault("max_chars", 280)
    x.setdefault("max_rewrites", 5)
    x.setdefault("pending_hours", 12)

    if yes("verify the keys now via GET /2/users/me? (reads only, posts nothing)"):
        save_data(data)
        from server.config import Config
        from server.bot.x_client import XClient
        try:
            import requests
            client = XClient(Config.load(str(CONFIG)))
            r = requests.get("https://api.twitter.com/2/users/me", auth=client._auth(), timeout=30)
            r.raise_for_status()
            ok(f"authenticated as @{r.json()['data']['username']}")
        except Exception as e:
            bad(f"check failed: {e} — retry with: python -m setup.wizard --source x")
    else:
        print("  Later: python -m server.bot.x_client --check")


def step_reddit(data: dict) -> None:
    """Reddit draft assist. Not auto-posting — Reddit closed self-service API
    access in late 2025 and this kind of server is commonly IP-blocked from
    oauth.reddit.com regardless of credentials, so once the X step resolves you
    get a prefilled Reddit submit link plus a copy block (the LinkedIn text
    reused verbatim, hashtags stripped, with a short generated title) and you
    tap Submit yourself. See README's "Also post to Reddit" for the why."""
    print("\n== Reddit (draft assist) ==")
    print("  This does NOT post to Reddit for you — once the X step resolves you get")
    print("  a prefilled submit link and a copy block in Telegram, and you tap Submit")
    print("  yourself in your browser. No credentials, no auth flow, no API calls.")
    sub = ask("subreddit to post to (without r/)",
             str(data.get("reddit", {}).get("subreddit", "") or "buildinpublic"))
    sub = sub.strip().removeprefix("r/").removeprefix("/r/").strip("/") or "buildinpublic"

    reddit = data.setdefault("reddit", {})
    reddit["enabled"] = True
    reddit["subreddit"] = sub
    reddit.setdefault("title_max", 300)
    reddit.setdefault("min_hours_between_posts", 48)
    reddit.setdefault("max_link_chars", 4000)
    ok(f"Reddit draft assist on for r/{sub}")


def step_image(data: dict) -> None:
    """Post illustrations via the Gemini API. Only needs an API key."""
    print("\n== Post images ==")
    print("  Approve stops publishing directly: it draws an image for the post and")
    print("  shows it to you, and nothing goes to LinkedIn until you confirm.")
    print("  Get a key at https://aistudio.google.com/apikey")
    print("  The image models need BILLING enabled on that key's project — a free")
    print("  key authenticates fine but returns 429 on every image request.")
    key = ask("GEMINI_API_KEY (empty = skip, images stay off)", env_get("GEMINI_API_KEY") or "")
    if not key:
        data.setdefault("image", {})["enabled"] = False
        warn("skipped — re-run later with: python -m setup.wizard --source image")
        return
    env_set("GEMINI_API_KEY", key)

    img = data.setdefault("image", {})
    img["enabled"] = True
    img["provider"] = "gemini"
    img["model"] = ask("model", img.get("model", "gemini-3-pro-image"))
    img["aspect_ratio"] = ask("aspect ratio (1:1, 4:5, 16:9…)", img.get("aspect_ratio", "1:1"))
    img["image_size"] = ask("image size (1K/2K/4K)", img.get("image_size", "2K"))

    if yes("render a test image now? (costs one generation)"):
        save_data(data)
        from server.config import Config
        from server.pipeline.image_gen import ImageGenError, generate_image
        out = Path(data["store_dir"]) / "images" / "_smoke.jpg"
        try:
            got = generate_image(Config.load(str(CONFIG)),
                                 "a flat editorial illustration of a paper plane over open water,"
                                 " restrained palette, no text",
                                 out_path=out)
            fix_owner(data, str(got.path))
            ok(f"rendered {got.path} ({got.size_bytes:,} bytes)")
        except ImageGenError as e:
            bad(f"render failed [{e.reason}]: {e}"
                + (f" — {e.detail[:200]}" if e.detail else ""))


def step_laptop(data: dict) -> None:
    """Authorize the person's laptop key and print exactly what to run there."""
    print("\n== Laptop access ==")
    user = data["run_as_user"]
    ingest = f"{data['ingest_dir']}/laptop"

    host = data.get("ssh_host")
    if not host:
        guess = subprocess.run("curl -s --max-time 5 ifconfig.me", shell=True,
                               capture_output=True, text=True).stdout.strip()
        host = ask("hostname or IP the laptop will ssh to", guess or "")
        data["ssh_host"] = host

    # show what's already trusted, so "already set up" is an informed skip
    try:
        import pwd
        keys_file = Path(pwd.getpwnam(user).pw_dir) / ".ssh" / "authorized_keys"
        entries = [l.split() for l in keys_file.read_text().splitlines()
                   if l.strip() and not l.startswith("#")] if keys_file.exists() else []
        if entries:
            print(f"  Keys already authorized for {user}:")
            for e in entries:
                label = e[2] if len(e) > 2 else "(no comment)"
                print(f"    - {e[0]}  …{e[1][-12:]}  {label}")
        else:
            print(f"  No keys authorized for {user} yet.")
    except (KeyError, PermissionError, OSError) as e:
        warn(f"cannot read authorized_keys ({e})")

    print("  Paste the laptop's SSH PUBLIC key (from `cat ~/.ssh/id_ed25519.pub`),")
    print("  or press Enter to skip if their key is already listed above.")
    print("  If they have no key yet, they run: ssh-keygen -t ed25519")
    pub = ask("public key (empty = skip)")
    if pub:
        if not pub.startswith(("ssh-", "ecdsa-")):
            bad("that doesn't look like a public key — skipping")
        else:
            try:
                import pwd
                pw = pwd.getpwnam(user)
                ssh_dir = Path(pw.pw_dir) / ".ssh"
                ssh_dir.mkdir(mode=0o700, exist_ok=True)
                keys = ssh_dir / "authorized_keys"
                existing = keys.read_text() if keys.exists() else ""
                if pub.split()[1] in existing:
                    ok("key already authorized")
                else:
                    keys.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n")
                                    + pub.strip() + "\n")
                    keys.chmod(0o600)
                    if os.geteuid() == 0:
                        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
                        os.chown(keys, pw.pw_uid, pw.pw_gid)
                    ok(f"key authorized for {user}@{host}")
            except PermissionError:
                bad(f"cannot write {user}'s authorized_keys — run this step as root")
            except KeyError:
                bad(f"no such user: {user}")

    print(f"""
  ---------------- run this ON THE LAPTOP ----------------
  1. Add to ~/.ssh/config (Git Bash: notepad ~/.ssh/config):

       Host dailypost
           HostName {host}
           User {user}

  2. Clone and run the laptop wizard:

       git clone <this repo> dailypost && cd dailypost
       powershell -ExecutionPolicy Bypass -File setup\\wizard_laptop.ps1

  3. Answer it with:
       ssh host alias .............. dailypost
       remote ingest dir ........... {ingest}
       user that runs dailypost .... {user}
       screenshots folder .......... (their screenshot folder, or empty)
  --------------------------------------------------------""")


def step_cron(data: dict) -> None:
    print("\n== Cron ==")
    tag = f"# dailypost-nightly{'-' + INSTANCE if INSTANCE else ''}"
    env = f"DAILYPOST_CONFIG={CONFIG} " if INSTANCE else ""
    line = (f"{data['pipeline']['cron_utc']} {env}{REPO}/server/run_nightly.sh "
            f">> {data['logs_dir']}/nightly.log 2>&1 {tag}")
    cur = subprocess.run(crontab_cmd(data, "-l"), capture_output=True, text=True)
    lines = [l for l in (cur.stdout.splitlines() if cur.returncode == 0 else []) if tag not in l]
    lines.append(line)
    subprocess.run(crontab_cmd(data, "-"), input="\n".join(lines) + "\n", text=True, check=True)
    os.chmod(REPO / "server" / "run_nightly.sh", 0o755)
    ok(f"crontab installed for {data['run_as_user']}: {line}")


def step_systemd(data: dict) -> None:
    print("\n== Bot service (systemd) ==")
    src_name = "dailypost-bot@.service" if INSTANCE else "dailypost-bot.service"
    unit_src = (REPO / "server" / "systemd" / src_name).read_text()
    unit = unit_src.replace("__APP_DIR__", str(REPO)).replace("__USER__", data["run_as_user"])
    target = Path(f"/etc/systemd/system/{src_name}")
    try:
        if os.geteuid() == 0:
            target.write_text(unit)
        else:
            subprocess.run(["sudo", "tee", str(target)], input=unit, text=True,
                           check=True, stdout=subprocess.DEVNULL)
        sysctl = ["systemctl"] if os.geteuid() == 0 else ["sudo", "systemctl"]
        subprocess.run([*sysctl, "daemon-reload"], check=True)
        subprocess.run([*sysctl, "enable", "--now", unit_name()], check=True)
        ok(f"{unit_name()} service enabled + started")
    except Exception as e:
        bad(f"systemd install failed ({e}); unit content written to {REPO / 'dailypost-bot.service.generated'}")
        (REPO / "dailypost-bot.service.generated").write_text(unit)


# ---------- doctor -------------------------------------------------------------

def doctor() -> int:
    print("== dailypost doctor ==")
    failures = 0

    def check(name, fn):
        nonlocal failures
        try:
            msg = fn()
            ok(f"{name}: {msg or 'ok'}")
        except Exception as e:
            bad(f"{name}: {e}")
            failures += 1

    if not CONFIG.exists():
        bad("config.yaml missing — run the wizard")
        return 1
    from server.config import Config
    cfg = Config.load(CONFIG)

    check("store", lambda: (__import__('server.store', fromlist=['Store']).Store(cfg.path_of('store_dir')) and
                            f"writable at {cfg.path_of('store_dir')}"))
    check("claude CLI", lambda: subprocess.run(
        [str(cfg.get("pipeline.claude_bin", "claude")), "--version"],
        capture_output=True, text=True, check=True).stdout.strip())

    def _claude_auth():
        """Actually call the model. `--version` passes on a revoked token, and so
        does `auth status` — it reports the local credentials file, not whether
        the server still honours it. Only a real request tells the truth."""
        from server.pipeline.claude_cli import run_claude
        out = run_claude(cfg, "Reply with exactly: OK", timeout=120)
        if "OK" not in out:
            raise RuntimeError(f"unexpected reply: {out[:120]!r}")
        return "authenticated (test prompt answered)"
    check("claude auth", _claude_auth)

    def _store_isolation():
        """Two instances sharing a store would write into one dailypost.db and
        mix their items, drafts and sessions together."""
        mine = Path(os.path.expanduser(str(cfg.get("store_dir")))).resolve()
        others = []
        for other in [REPO / "config.yaml", *(REPO / "instances").glob("*/config.yaml")]:
            if not other.exists() or other.resolve() == CONFIG.resolve():
                continue
            try:
                d = yaml.safe_load(other.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if d.get("store_dir") and Path(os.path.expanduser(d["store_dir"])).resolve() == mine:
                others.append(str(other))
        if others:
            raise RuntimeError(f"store_dir {mine} is also used by {others} — give this "
                               "instance its own store_dir")
        return f"private store at {mine}"
    check("store isolation", _store_isolation)

    def _overlap():
        from server.harvest import unsafe_sources
        bad = unsafe_sources(cfg)
        if bad:
            raise RuntimeError(
                f"source(s) {bad} harvest a shared projects dir WITHOUT an ownership "
                "filter — other users' sessions would be captured. Disable them.")
        return "no unfiltered source over a shared dir"
    check("source isolation", _overlap)

    for src in cfg.sources():
        t = src["type"]
        name = src.get("name", t)
        if t in ("claude_projects_dir",):
            check(name, lambda s=src: f"dir exists" if Path(os.path.expanduser(s['projects_dir'])).exists()
                  else (_ for _ in ()).throw(RuntimeError("projects dir missing")))
        elif t == "claude_sessions":
            def _cs(s=src):
                from server.harvest.claude_sessions import resolve_session_ids
                ids = resolve_session_ids(s.get("filter"), "1970-01-01 00:00:00")
                return f"filter matches {len(ids) if ids is not None else 'ALL'} sessions"
            check(name, _cs)
        elif t == "ingest_dir":
            check(name, lambda s=src: "ok" if Path(s["path"]).exists()
                  else (_ for _ in ()).throw(RuntimeError("ingest dir missing")))
        elif t == "telegram":
            check(name, lambda s=src: "session file present" if Path(os.path.expanduser(s["session_file"])).exists()
                  else (_ for _ in ()).throw(RuntimeError("session file missing — run wizard --source telegram")))
        elif t == "gmail":
            def _gm(s=src):
                from server.harvest.gmail import _client
                _client(Path(os.path.expanduser(s["token_file"])), Path(""))
                return "token valid"
            check(name, _gm)
        elif t == "whatsapp":
            def _wa(s=src):
                import requests
                r = requests.get(f"{s['waha_url']}/api/sessions/{s.get('session', 'default')}",
                                 headers={"X-Api-Key": cfg.secret("WAHA_API_KEY") or ""}, timeout=10)
                r.raise_for_status()
                d = r.json()
                if d.get("status") != "WORKING":
                    raise RuntimeError(f"session status {d.get('status')}")
                # a WORKING session with no webhook captures nothing, silently
                hooks = (d.get("config") or {}).get("webhooks") or []
                want = f":{s.get('webhook_port', 8477)}/waha"
                if not any(want in h.get("url", "") for h in hooks):
                    raise RuntimeError("session WORKING but our webhook is not registered "
                                       "— run: python -m setup.wizard --source pair")
                return "session WORKING, webhook registered"
            check(name, _wa)

            def _wa_reach(s=src):
                """WAHA runs in a container; if it can't reach our host-side receiver,
                messages are dropped with no error anywhere."""
                url = hooks_url = f"http://{s.get('webhook_host', '127.0.0.1')}:{s.get('webhook_port', 8477)}/health"
                out = subprocess.run(
                    ["docker", "exec", s.get("container_name", "dailypost-waha"), "sh", "-c",
                     f"wget -qO- --timeout=5 {url.replace('127.0.0.1', 'host.docker.internal')}"],
                    capture_output=True, text=True, timeout=20)
                if "ok" not in out.stdout:
                    raise RuntimeError(f"WAHA container cannot reach {hooks_url} — set "
                                       "webhook_host to the docker bridge gateway (172.17.0.1)")
                return "container can reach the webhook"
            check(f"{name} delivery", _wa_reach)

    def _bot():
        import requests
        token = cfg.secret("TG_BOT_TOKEN")
        if not token:
            raise RuntimeError("TG_BOT_TOKEN not set")
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if not r.get("ok"):
            raise RuntimeError(str(r))
        return f"@{r['result']['username']}"
    check("telegram bot", _bot)

    def _li():
        from server.bot.linkedin_client import LinkedInClient
        # A token borrowed from another instance authenticates fine and posts to
        # SOMEONE ELSE'S account — the failure only shows up on their feed, so
        # check ownership before reporting the token healthy.
        tf = cfg.path_of("linkedin.token_file")
        store_dir = cfg.path_of("store_dir")
        if tf and store_dir and store_dir.resolve() not in tf.resolve().parents:
            raise RuntimeError(
                f"token_file {tf} is outside this instance's store {store_dir} — "
                "it belongs to another instance and would post to their account")
        d = LinkedInClient(cfg).days_until_expiry()
        if d is None:
            raise RuntimeError("no token — run linkedin_auth")
        if d < 7:
            raise RuntimeError(f"token expires in {d} days")
        return f"token ok, {d} days left"
    check("linkedin", _li)

    def _li_rest():
        """Proves versioned-API access, the LinkedIn-Version header and the person
        URN, without creating a post. The registered upload URL just expires."""
        from server.bot.linkedin_client import LinkedInClient
        LinkedInClient(cfg)._init_image_upload()
        return f"rest/images ok (version {cfg.get('linkedin.api_version', '202506')})"
    check("linkedin rest", _li_rest)

    def _x():
        if not cfg.get("x.enabled", False):
            return "disabled"
        from server.bot.x_client import XClient, _fail
        client = XClient(cfg)
        if not client.configured():
            raise RuntimeError("X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET not set")
        import requests
        r = requests.get("https://api.twitter.com/2/users/me", auth=client._auth(), timeout=30)
        if r.status_code >= 300:
            _fail(r, "users/me")
        return f"authenticated as @{r.json()['data']['username']}"
    check("x", _x)

    def _gemini():
        if not cfg.get("image.enabled", True):
            return "disabled"
        if not cfg.secret("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY not set")
        from google import genai
        model = str(cfg.get("image.model", "gemini-3-pro-image"))
        client = genai.Client(api_key=cfg.secret("GEMINI_API_KEY"))
        client.models.get(model=model)          # validates key + model, spends nothing
        # deliberately not a render: this proves the key and the model name, NOT
        # that the project has image quota. A free-tier key passes this and still
        # 429s on every generation — `--source image` offers a real render.
        return f"{model} reachable (quota untested)"
    check("gemini image", _gemini)

    def _cron_window():
        """The retry slots must all fall before local noon.

        target_day() reports 'yesterday' only until 12:00 local; a slot at or
        after that flips to today and drafts a SECOND set for a day already
        posted. The deadline must also land inside the window, or a laptop that
        never checks in is never drafted for at all."""
        import re as _re
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone as _tz
        sched = str(cfg.get("pipeline.cron_utc", ""))
        m = _re.match(r"^(\d+)\s+([0-9,\-*/]+)", sched)
        if not m:
            return f"schedule {sched!r} not parsed — check it by hand"
        minute, hours = int(m.group(1)), m.group(2)
        last_utc = int(hours.split("-")[-1].split(",")[-1]) if hours != "*" else 23
        tzname = str(cfg.get("pipeline.timezone", "UTC"))
        off = datetime.now(_tz.utc).astimezone(ZoneInfo(tzname)).utcoffset().total_seconds() / 3600
        last_local = (last_utc + off) % 24
        deadline = int(cfg.get("pipeline.wait_deadline_hour", 12))
        if last_local >= 12:
            raise RuntimeError(
                f"last cron slot is {int(last_local):02d}:{minute:02d} local — at or after "
                "noon target_day() moves to today, so it drafts a second set for a "
                "day already handled. End the range before local noon.")
        if cfg.get("pipeline.wait_for_laptop", True) and deadline > last_local:
            raise RuntimeError(
                f"wait_deadline_hour {deadline} is after the last slot "
                f"({int(last_local):02d}:{minute:02d} local) — a laptop that never checks "
                "in would never be drafted for")
        return f"slots end {int(last_local):02d}:{minute:02d} local, deadline {deadline}:00"
    check("cron window", _cron_window)

    def _cron():
        out = subprocess.run(crontab_cmd(cfg.data, "-l"), capture_output=True, text=True)
        tag = f"dailypost-nightly{'-' + INSTANCE if INSTANCE else ''}"
        if tag not in out.stdout:
            raise RuntimeError("nightly cron entry missing")
        return "installed"
    check("cron", _cron)

    def _svc():
        out = subprocess.run(["systemctl", "is-active", unit_name()], capture_output=True, text=True)
        if out.stdout.strip() != "active":
            raise RuntimeError(out.stdout.strip() or "not installed")
        return "active"
    check("bot service", _svc)

    print(f"\n{failures} problem(s)" if failures else f"\n{GREEN}all green{RESET}")
    return 1 if failures else 0


STEPS = {
    "base": step_base, "claude": step_claude, "telegram": step_telegram,
    "bot": step_bot, "gmail": step_gmail, "whatsapp": step_whatsapp,
    "pair": step_pair, "linkedin": step_linkedin, "x": step_x, "reddit": step_reddit,
    "image": step_image, "laptop": step_laptop, "cron": step_cron, "systemd": step_systemd,
}


# ---------- completion probes: full runs continue from current state -----------

def _done_base(data):
    if CONFIG.exists() and data.get("store_dir") and Path(data["store_dir"]).exists() \
            and (REPO / ".venv").exists():
        return f"dirs + venv ready, store at {data['store_dir']}"


def _done_claude(data):
    types = {s.get("type") for s in data.get("sources", []) if s.get("enabled")}
    if types & {"claude_projects_dir", "claude_sessions"}:
        return "claude source(s) configured"


def _done_telegram(data):
    src = get_source(data, "telegram")
    if src and src.get("enabled") and Path(os.path.expanduser(src["session_file"])).exists():
        return "logged in (session file present)"


def _done_bot(data):
    if env_get("TG_BOT_TOKEN") and env_get("TG_ALLOWED_CHAT_ID"):
        return "token + chat id set"


def _done_gmail(data):
    src = get_source(data, "gmail")
    if src and src.get("enabled") and Path(os.path.expanduser(src["token_file"])).exists():
        return "token present"


def _done_whatsapp(data):
    src = get_source(data, "whatsapp")
    if src and src.get("enabled") and env_get("WAHA_API_KEY"):
        return f"configured at {src['waha_url']}"


def _done_pair(data):
    src = get_source(data, "whatsapp")
    if not (src and src.get("enabled")):
        return "n/a (whatsapp disabled)"
    try:
        import requests
        r = requests.get(f"{src['waha_url']}/api/sessions/{src.get('session', 'default')}",
                         headers={"X-Api-Key": env_get("WAHA_API_KEY") or ""}, timeout=5)
        if r.ok and r.json().get("status") == "WORKING":
            return "session WORKING"
    except Exception:
        pass


def _done_linkedin(data):
    tf = data.get("linkedin", {}).get("token_file")
    if tf and Path(os.path.expanduser(tf)).exists():
        return "token present"


def _done_x(data):
    x_keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    if data.get("x", {}).get("enabled") and all(env_get(k) for k in x_keys):
        return "configured"


def _done_reddit(data):
    r = data.get("reddit", {})
    if r.get("enabled") and r.get("subreddit"):
        return f"r/{r['subreddit']}"


def _done_image(data):
    if env_get("GEMINI_API_KEY") and data.get("image", {}).get("enabled"):
        return f"images on ({data['image'].get('model', 'gemini-3-pro-image')})"


def _done_cron(data):
    out = subprocess.run(crontab_cmd(data, "-l"), capture_output=True, text=True)
    if "dailypost-nightly" in out.stdout:
        return "nightly entry installed"


def _done_systemd(data):
    out = subprocess.run(["systemctl", "is-active", "dailypost-bot"], capture_output=True, text=True)
    if out.stdout.strip() == "active":
        return "service active"


def _done_laptop(data):
    return None  # always offer it: it prints the laptop instructions


DONE_PROBES = {
    "base": _done_base, "claude": _done_claude, "telegram": _done_telegram,
    "bot": _done_bot, "gmail": _done_gmail, "whatsapp": _done_whatsapp,
    "pair": _done_pair, "linkedin": _done_linkedin, "x": _done_x, "reddit": _done_reddit,
    "image": _done_image, "laptop": _done_laptop, "cron": _done_cron, "systemd": _done_systemd,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--source", choices=sorted(STEPS), help="run a single step")
    ap.add_argument("--list-instances", action="store_true",
                    help="print 'name<TAB>ingest_dir<TAB>run_as_user' per instance "
                         "(used by the laptop wizard to offer the right target)")
    ap.add_argument("--instance", default=os.environ.get("DAILYPOST_INSTANCE"),
                    help="name a separate instance (own config, store, bot, cron) "
                         "so several people can share one server install")
    args = ap.parse_args(argv)

    if args.list_instances:
        for cfg_file in [REPO / "config.yaml", *sorted((REPO / "instances").glob("*/config.yaml"))]:
            if not cfg_file.exists():
                continue
            try:
                d = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            name = cfg_file.parent.name if cfg_file.parent.name != REPO.name else "default"
            print(f"{name}\t{d.get('ingest_dir', '')}/laptop\t{d.get('run_as_user', '')}")
        return 0

    instance = args.instance
    if instance is None and not args.doctor and not args.source:
        # several people can share one server install — each gets their own
        # config, store, bot, cron and WAHA container under instances/<name>/
        print("=== dailypost setup wizard ===")
        existing = sorted(p.name for p in (REPO / "instances").glob("*")
                          if (p / "config.yaml").exists()) if (REPO / "instances").exists() else []
        if existing:
            print(f"existing instances: {', '.join(existing)}")
        instance = ask("who is this setup for? (short name, e.g. alice; "
                       "empty = this server's default instance)") or None
    use_instance(instance)
    if instance:
        print(f"instance: {instance}  ({CONFIG})")

    if args.doctor:
        return doctor()

    data = load_data()
    if args.source:
        STEPS[args.source](data)
        save_data(data)
        return 0

    print("(steps already completed are skipped — answer y to redo one)")
    failed = []
    for name in ("base", "claude", "telegram", "bot", "gmail", "whatsapp", "pair",
                 "linkedin", "laptop", "cron", "systemd"):
        done = None
        try:
            done = DONE_PROBES[name](data)
        except Exception:
            pass
        if done:
            if not yes(f"[{name}] already set up ({done}) — redo?", False):
                continue
        try:
            STEPS[name](data)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # One failing step must not abandon the rest: a crash here used to
            # skip cron and the bot service entirely, leaving a silent half-setup.
            bad(f"[{name}] failed: {e}")
            failed.append(name)
        save_data(data)
    inst = f" --instance {INSTANCE}" if INSTANCE else ""
    print("\n" + "=" * 60)
    if failed:
        bad(f"{len(failed)} step(s) failed: {', '.join(failed)}")
        for f in failed:
            print(f"  retry: python -m setup.wizard{inst} --source {f}")
        print("")
    print(f"Server setup {'finished with errors' if failed else 'complete'}"
          f"{f' for {INSTANCE}' if INSTANCE else ''}.")
    print(f"  health check:    python -m setup.wizard{inst} --doctor")
    if INSTANCE:
        print(f"  manual run:      DAILYPOST_CONFIG={CONFIG} server/run_nightly.sh --dry-run")
        print(f"  bot service:     systemctl status {unit_name()}")
    else:
        print("  manual run:      server/run_nightly.sh --dry-run")
    print(f"\nNow finish on the laptop — see the 'run this ON THE LAPTOP' block above,")
    print(f"or re-print it any time with:")
    print(f"  python -m setup.wizard{inst} --source laptop")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
