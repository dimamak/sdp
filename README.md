# dailypost — your daily work, turned into a LinkedIn draft

A self-hosted pipeline that harvests one day of your real work — Claude Code
sessions, WhatsApp/Telegram chats, Gmail, meeting-notetaker emails, screenshots,
(optionally) call debriefs and audio — asks an LLM to pick the day's best story,
and delivers a ready-to-publish LinkedIn draft to a private Telegram bot with
**Approve / Edit / Skip** buttons. Approve draws an illustration for the post and
shows it to you first — **nothing reaches LinkedIn until a second tap**. Publishing
goes through the official *Share on LinkedIn* API (`w_member_social`).
Never auto-publishes.

Once a LinkedIn post actually publishes, it can optionally also go to X
(Twitter): the same day's Claude session writes a separate, shorter X-native
rewrite (not a truncation — X's limit is 280 chars against LinkedIn's
600–1,200), and that gets its own **Post to X / Rewrite / Replace text / Skip X**
buttons. Nothing reaches X until that second tap either, and nothing in that
step can affect the LinkedIn post already made. See [Also post to X](#also-post-to-x).

Runs on any Linux server + optional laptop; nothing instance-specific is in the
code — everything lives in `config.yaml` / `.env` (both git-ignored).

## How it works

```
laptop ──(nightly tar-over-ssh)──▶ ingest/           screenshots, Claude JSONL, audio
server harvesters ───────────────▶ SQLite store      telegram, gmail, whatsapp, claude sessions
nightly cron: digest ─▶ claude -p ─▶ draft ─▶ Telegram bot
                                             └▶ [Approve] ─▶ image ─▶ [Post] ─▶ LinkedIn
                                                                          └▶ rewrite ─▶ [Post to X] ─▶ X
```

- **Claude Code sessions** are read from `~/.claude/projects/**/*.jsonl` — locally,
  and/or on a shared multi-user host through a *session filter* (a config-supplied
  SQL query / command / id-list that says which sessions are yours).
- **WhatsApp** is captured read-only via self-hosted [WAHA](https://waha.devlike.pro/)
  (unofficial client — low but nonzero ToS/ban risk; never send through it).
- **The LLM step** uses the `claude` CLI headlessly with your existing Claude Code
  credentials ($0 marginal), or `ANTHROPIC_API_KEY` if you prefer the API.

## Images

Tapping **Approve** no longer publishes. It asks the *same Claude session that wrote
the post* for an image brief — so the picture comes from the day's story, not from a
re-reading of the post's wording — renders it with the Gemini API, and sends it back
with **Post with image · Regenerate · Post text-only · Cancel**.

While an image is on the table, plain messages steer it ("more abstract, no people",
"colder palette") and each reply is a new take. `/talk <message>` goes back to
discussing the words instead. Regenerating never touches LinkedIn; only the final
tap does.

- Needs `GEMINI_API_KEY` in `.env` — get one at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey). **The image
  models require billing enabled on that key's project**; a free-tier key
  authenticates fine and then returns 429 on every render.
- Roughly $0.13–0.24 per render at `gemini-3-pro-image`, so a few dollars a month
  at one post a day. `image.model: gemini-3.1-flash-image` is ~3× cheaper.
- Turn it off with `image.enabled: false` — Approve then publishes in one tap, as
  it used to. Any render failure also offers *Post text-only*, so you are never
  stuck with an approved draft you can't publish.
- Images live in `<store_dir>/images/<day>/`. Published ones are kept forever
  (LinkedIn won't give them back); unused takes are pruned after
  `image.retention_days`.

Set it up with `.venv/bin/python -m setup.wizard --source image`.

## Also post to X

Turned on with `x.enabled: true`. Once a draft actually publishes to LinkedIn —
not before — the same day's Claude session writes a separate X-native rewrite of
the same fact (the LinkedIn style guide targets 600–1,200 characters; X's default
cap is 280, so this is a genuine rewrite, not a truncation). It arrives in
Telegram with its own **Post to X · Rewrite · Replace text · Skip X** buttons,
reusing the same illustration if one was posted with the LinkedIn version.
Failing or skipping this step never touches the LinkedIn post already made.

- Needs an X (Twitter) developer App with OAuth 1.0a keys in `.env`:
  `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`. Unlike
  LinkedIn's OAuth2 flow, these don't expire on their own — no token file, no
  refresh step. **Generate the access token after** setting the App's
  permissions to "Read and write", or it stays read-only and every post 403s.
- One App can post for several people the same way one LinkedIn App already
  can (see [Several people on one server](#several-people-on-one-server)): the
  owner generates `X_ACCESS_TOKEN`/`X_ACCESS_TOKEN_SECRET` for themselves
  directly in [developer.x.com](https://developer.x.com); everyone else shares
  the owner's `X_API_KEY`/`X_API_SECRET` and gets their own access token via
  `python -m server.bot.x_auth` — a PIN-based OAuth flow that opens a URL,
  signs in as their own account, and writes the pair into their own instance's
  `.env`.
- The X API Free tier caps writes at roughly 500 posts/month per App — plenty
  for one post a day.
- `x.max_chars` (default 280) is a soft guard the rewrite targets; X's own API
  response is the real authority on length. Raise it only if the posting
  account has X Premium/Premium+.
- If the bot restarts between a LinkedIn publish and the X step starting, `/x`
  in Telegram recovers it.

Set it up with `.venv/bin/python -m setup.wizard --source x`. Probes that don't
touch your timeline: `python -m server.bot.x_client --dry-run "text"` and
`--check`.

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
LinkedIn OAuth guidance, Gemini image key. Every step is re-runnable:
`python -m setup.wizard --source telegram`.

Want `git push` to `main` to auto-deploy to your server? See
[docs/self-hosting/ci-deploy.md](docs/self-hosting/ci-deploy.md) — optional,
`server/deploy.sh` still works for manual deploys.

## Several people on one server

Each person gets their own instance: separate config, secrets, store, Telegram
bot, LinkedIn token, cron entry, bot service and WAHA container. Shared code and
venv, no interference.

On the server, for each additional person:

```bash
.venv/bin/python -m setup.wizard --instance alice
```

Everything lands in `instances/alice/` and the wizard installs
`dailypost-bot@alice` plus a cron line carrying `DAILYPOST_CONFIG`. Health check
and single steps take the same flag:

```bash
.venv/bin/python -m setup.wizard --instance alice --doctor
```

On a shared multi-user coding host, each instance's `claude_sessions` filter
selects only that person's sessions (their own username in the SQL filter), and
each person runs the laptop wizard on their own machine pointing at their own
ingest dir. Note that instances share the run-as user, so anyone with that user's
shell can read every instance's store — separate Linux users if that matters.

## Configuration

Copy `config.example.yaml` → `config.yaml` and `.env.example` → `.env` (the wizard
does this for you). Key ideas:

- `sources:` is a **list of adapter instances** — enable, disable, or duplicate
  freely. Types: `claude_projects_dir`, `claude_sessions` (with filter strategies
  `all | sql | command | id_file`), `ingest_dir`, `telegram`, `gmail`, `whatsapp`.
- `pipeline:` timezone, cron, size caps, model, `always_hashtags` (asked
  interactively by the wizard) — tags included verbatim on every X rewrite (not
  the LinkedIn draft); the X rewrite is additionally asked to add a couple more
  tags relevant to that specific post.
- `image:` illustration model, aspect ratio, size, regeneration cap — or
  `enabled: false` to skip the image step entirely.
- `x:` char limit, rewrite cap, how long a candidate stays steerable — `enabled:
  false` (the default) skips the X step entirely; see [Also post to X](#also-post-to-x).
- Style/voice of the drafts: edit `server/pipeline/prompts/style-guide.md`; the
  look of the images: `image.style_suffix` in `config.yaml`.

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
