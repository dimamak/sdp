# dailypost — your daily work, turned into a LinkedIn draft

A self-hosted pipeline that harvests one day of your real work — Claude Code
sessions, WhatsApp/Telegram chats, Gmail, meeting-notetaker emails, screenshots,
(optionally) call debriefs and audio — asks an LLM to pick the day's best story,
and delivers a ready-to-publish LinkedIn draft to a private Telegram bot with
**Approve / Edit / Skip** buttons. On Approve it posts through the official
LinkedIn *Share on LinkedIn* API (`w_member_social`). Never auto-publishes.

Runs on any Linux server + optional laptop; nothing instance-specific is in the
code — everything lives in `config.yaml` / `.env` (both git-ignored).

## How it works

```
laptop ──(nightly tar-over-ssh)──▶ ingest/           screenshots, Claude JSONL, audio
server harvesters ───────────────▶ SQLite store      telegram, gmail, whatsapp, claude sessions
nightly cron: digest ─▶ claude -p ─▶ draft ─▶ Telegram bot ─▶ [Approve] ─▶ LinkedIn post
```

- **Claude Code sessions** are read from `~/.claude/projects/**/*.jsonl` — locally,
  and/or on a shared multi-user host through a *session filter* (a config-supplied
  SQL query / command / id-list that says which sessions are yours).
- **WhatsApp** is captured read-only via self-hosted [WAHA](https://waha.devlike.pro/)
  (unofficial client — low but nonzero ToS/ban risk; never send through it).
- **The LLM step** uses the `claude` CLI headlessly with your existing Claude Code
  credentials ($0 marginal), or `ANTHROPIC_API_KEY` if you prefer the API.

## Setup

Server:

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m setup.wizard        # interactive: picks sources AND provisions them
.venv/bin/python -m setup.wizard --doctor
server/run_nightly.sh --dry-run         # first harvest, no LLM/Telegram
```

Laptop (Windows, pushes Claude sessions + screenshots):

```powershell
git clone <this repo>
powershell -ExecutionPolicy Bypass -File setup\wizard_laptop.ps1
```

The wizard handles: directories, venv, cron, systemd bot service, Telegram login
(Telethon), bot chat-id detection, WAHA docker + QR pairing, Gmail OAuth guidance,
LinkedIn OAuth guidance. Every step is re-runnable: `python -m setup.wizard --source telegram`.

## Configuration

Copy `config.example.yaml` → `config.yaml` and `.env.example` → `.env` (the wizard
does this for you). Key ideas:

- `sources:` is a **list of adapter instances** — enable, disable, or duplicate
  freely. Types: `claude_projects_dir`, `claude_sessions` (with filter strategies
  `all | sql | command | id_file`), `ingest_dir`, `telegram`, `gmail`, `whatsapp`.
- `pipeline:` timezone, cron, size caps, model.
- Style/voice of the drafts: edit `server/pipeline/prompts/style-guide.md`.

## Privacy & safety notes

- Transcripts contain source code and secrets. The store stays on your server
  (`chmod 700`), is never exposed over HTTP, and raw files are pruned after
  `retention_days`.
- WhatsApp capture is read-only by design; documented bans overwhelmingly involve
  *sending*. If you're not comfortable with the residual risk, disable the source.
- Recording/processing other people's messages has privacy implications — don't
  quote anyone identifiably in public posts (the default style guide forbids it).

## License

MIT
