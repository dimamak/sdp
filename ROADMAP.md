# Roadmap

Phases from the original design (generic — instance details live in the
git-ignored `PLAN.local.md` / `config.yaml`).

## Phase 1 — MVP  ✅ implemented
- Source adapter registry with filter strategies (`all | sql | command | id_file`)
  for multi-user Claude Code hosts
- Laptop push (Claude JSONL + screenshots) via tar-over-ssh scheduled task
- Telegram (Telethon), Gmail (+ meeting-notetaker transcript emails), WhatsApp
  (WAHA, read-only webhook)
- Nightly digest → headless `claude -p` draft → Telegram Approve/Edit/Skip →
  official LinkedIn `w_member_social` post
- Interactive setup wizard (`python -m setup.wizard`) + `--doctor`

## Phase 2 — Audio  ✅ implemented
- Always-on office audio capture (`server/capture/audio.py`, Windows/macOS/Linux):
  continuous mic recording in short Opus segments, silence-swept locally so a
  quiet day never leaves the machine; a `PAUSED` flag file mutes it for private
  conversations, and nothing in the pipeline may delete that flag
- Server transcription (`server/pipeline/transcribe.py`): faster-whisper,
  INT8 `large-v3-turbo` by default; for Hebrew-only rooms the ivrit.ai
  fine-tune with `language: 'he'` (the fine-tune can't translate — transcribe
  Hebrew, let the draft LLM translate); configurable device/threads/beam size;
  raw audio is deleted once a transcript exists
- `transcribe_pending()` drains the audio queue before the digest step on
  every nightly run
- Still open: a phone post-call debrief habit (automation rule → recorder →
  file reaches the server via cloud-sync or Syncthing), a hotkey/manual WAV
  recorder for in-person conversations, and a Groq API fallback for
  overflow/English transcription

## Phase 3 — Images  ✅ implemented
- Approve is now two-stage: it draws an illustration for the post (Gemini API,
  `gemini-3-pro-image`) and sends it back for confirmation — nothing reaches
  LinkedIn until "Post with image" or "Post text-only"
- The image prompt and alt text are written by the SAME Claude session that wrote
  the post, so the brief comes from the day's story, not from the post's wording
- Steer by replying to the image ("more abstract, no people"); each reply is a new
  take, and regenerating costs no LinkedIn API calls
- Publishing migrated from `/v2/ugcPosts` to the versioned `/rest/posts` +
  `/rest/images` API — one code path for text-only and image posts

## Phase 4 — polish  ▢ ideas
- Weekly "best of the week" mode when daily posting is too much
- Edit-in-web-UI link instead of Telegram inline edit
- Harvested screenshots as post media (the original Phase 3 idea) — now a small
  addition on top of the image pipeline rather than a new integration

## Phase 5 — also post to X  ✅ implemented
- After a LinkedIn draft actually publishes, the same day's Claude session
  writes a separate X-native rewrite (not a truncation — LinkedIn targets
  600–1,200 characters, X's default cap is 280) and delivers it with its own
  Post to X / Rewrite / Replace text / Skip X buttons; nothing reaches X on a
  single tap, and nothing in this step can affect the LinkedIn post already made
- Auth is OAuth 1.0a with four static keys generated once in the X developer
  portal (`server/bot/x_client.py`) — no token file or refresh loop, unlike
  LinkedIn's OAuth2 flow
- One App's keys can be shared across several people's instances, same as this
  project already shares one LinkedIn App: the owner auto-generates their own
  access token in the portal, everyone else runs the PIN-based OAuth flow in
  `server/bot/x_auth.py` (offered inline by `setup.wizard --source x`) to get
  their own token for the same shared App
- Reuses the same illustration posted to LinkedIn, uploaded separately to X's
  media endpoint; a failed media upload still posts the text rather than
  losing the post
- `x.enabled: false` by default; `/x` in Telegram recovers the step if the bot
  restarts between a LinkedIn publish and the X candidate being written
- `pipeline.always_hashtags` (asked by the setup wizard) is included verbatim on
  every X rewrite only, not the LinkedIn draft; the X rewrite prompt separately
  asks the agent to add 0-2 more tags relevant to that specific post, since X
  leans on hashtags for discovery more than LinkedIn does

## Phase 6 — Reddit draft assist  ✅ implemented
- Reddit closed self-serve API access to new apps (the Devvit path is a dead
  end for a simple cross-poster) and this server's IP is separately blocked
  by `oauth.reddit.com` — so unlike X, this step is *assisted, not automated*:
  it never submits anything. After the LinkedIn publish (and X, if enabled)
  resolves, Telegram delivers a prefilled `reddit.com/r/<sub>/submit` link plus
  copy-paste title/body blocks, and a real human tap in a real browser is what
  actually posts
- The same day's Claude session writes only a short Reddit-appropriate title;
  the body reuses the LinkedIn text verbatim (hashtags stripped) rather than a
  full Reddit-native rewrite — X already proved the rewrite pattern works, but
  a second full rewrite step wasn't justified until there's evidence
  r/buildinpublic-style audiences actually react badly to LinkedIn-voice copy.
  If that turns out to be false, the next step is a `reddit_rewrite()`
  alongside `x_rewrite()`, not a bigger version of this feature
- `reddit.min_hours_between_posts` adds a cadence nudge (never a block) to the
  delivery message; `reddit.enabled: false` by default; `/reddit` in Telegram
  recovers the step if the bot restarts before the link is sent
- No OAuth client, token file, or API credentials of any kind — the contingency
  plan for a real posting integration (once Reddit's API situation changes) is
  kept spec'd but deliberately unbuilt

## Phase 7 — Laptop-only mode  ✅ implemented
- `mode: laptop` replaces cron + systemd with an in-process scheduler thread
  inside the already-running Telegram bot (`server/bot/scheduler.py`) — it
  wakes periodically and catches up on the missed slot if the machine was
  asleep at the scheduled time, since a single laptop can't rely on cron
  firing exactly on time
- Cross-platform single-instance lock (`server/pipeline/lock.py`): `fcntl`
  advisory locking on POSIX, `msvcrt` on Windows, same non-blocking semantics
  either way
- Setup wizard asks laptop-vs-server up front and branches its whole step
  order and defaults (home-directory paths instead of `/opt`, no run-as user)
- Both recorders run as threads inside that same bot process, so laptop mode
  captures audio and screen activity with nothing else to install or start —
  and `setup/autostart.py` registers the process with the OS's own session
  manager (systemd `--user`, launchd, Task Scheduler logon task) so it returns
  at login. The bot writes a heartbeat the doctor checks, because "the bot
  isn't running" used to look exactly like a healthy install
- No separate server needed for the main single-person use case; the old
  Windows-laptop-pushes-to-a-remote-server architecture (Phase 1) still works
  unchanged for people who want an always-on shared box instead

## Phase 8 — Codex / ChatGPT support  ✅ implemented
- New `codex_sessions` harvest adapter reads `$CODEX_HOME/sessions/` — the
  Codex CLI, its IDE extension, and the ChatGPT desktop app (merged into
  Codex on 2026-07-09) all write to the same local store, so one adapter
  covers both "I use Codex" and "I use ChatGPT"
- `pipeline.backend: claude | codex` picks which CLI drafts posts, briefs
  images, and rewrites for X/Reddit — an `LLMResult` abstraction
  (`server/pipeline/llm.py`) hides the difference from the rest of the
  pipeline
- Codex has no way to pre-assign a session id the way `claude -p
  --session-id` does; the id is only discoverable from the `thread.started`
  event after the first call, and resumed with `codex exec resume <id>` — the
  pipeline's mint-then-record pattern was inverted to call-then-record to
  match
- A `backend` column on stored sessions stops a Claude session from being
  mistakenly resumed under the Codex backend (or vice versa) if you switch
  backends between runs — falls back to a fresh one-shot call instead
- Does not cover Codex Cloud tasks or the plain ChatGPT web/desktop Chat tab
  — see the README's [Limitations](README.md#limitations--non-goals)

## Phase 9 — acting on a LinkedIn performance analysis  ✅ implemented
Prompted by a manual export of `linkedin.com/in/<id>/recent-activity/` covering
17 posts (2026-08-10 to 2026-08-30): every post fell in a narrow 04:30–11:01
UTC band because post time = review time, and a five-day zero-reaction trough
was at least partly confounded with an unrelated prompt change the same week
— see `.claude/plans/plan.md` for the full evidence check, including which
parts of the original analysis read cleaner than they actually are.
- **Publish queue** (landed): `publish.window`/`publish.days` gate when an
  approved draft actually reaches LinkedIn, separate from when you tapped
  Approve. Empty/unset window keeps immediate publishing (the previous, and
  default, behaviour) — opt-in for this instance, unchanged for everyone else.
  A `server/pipeline/publish_window.py` pure-function module computes slots;
  `Bot.publish_or_queue` decides now-vs-queued, an asyncio task started from
  `post_init` polls `Store.due_drafts()` every `publish.poll_seconds`, and a
  single **Post now anyway** button bypasses the queue for one post. A queued
  draft older than `publish.max_age_days` expires unposted rather than landing
  stale. `pipeline.max_drafts` dropped from 4 to 2 to match a narrowed
  `publish.days` cadence without drafting fewer days — only publishing is gated.
- **Generator** (landed): replaced the fixed 1,100–1,600 character target with
  a variance requirement (`style-guide.md`/`draft-prompt.md`), shows the
  drafter its own recent shapes (`drafts.shape`, `Store.recent_shapes`/
  `days_since_shape`, rendered as `{RECENT_SHAPES}`/`{DAYS_SINCE_ASK}` in
  `write_drafts`) so it stops repeating the same finding → number →
  why-it-matters structure, adds an `ask` candidate class for open,
  first-person questions (soft-capped at one a week via `days_since_shape`),
  rebalanced how often posts end on a real question (one in three → one in
  two), and narrowed the "never name our stack" rule to "What stays private" —
  clients, partners, and vendors whose name reveals the method stay hidden;
  public tools (the AI coding agents, models, hosting, datasets) are named
  plainly.
- **Images** (landed): dropped the brief's "no text in images" rule (wrong on
  the evidence — the best-performing post had two words of clean display type
  in it) from `IMAGE_BRIEF_PROMPT` and the `image.style_suffix` default, and
  added a post-render check instead. `server/pipeline/image_check.py`'s
  `check_image_text` runs one `run_llm` vision call (same Claude/Codex path
  `digest.py`'s `describe_screenshots` already uses, not a separate vision
  API) for malformed/nonsense text and legible dashboard-style content — the
  actual failure mode. `Bot.start_image` auto re-renders once on a flagged
  take without spending the user's `max_regenerations` budget (they never saw
  the bad one); if the retry is still bad, it delivers anyway with a warning
  in the caption. A checker failure (timeout, bad JSON) is logged and treated
  as a pass — a broken checker must never cost a post. New `image.text_check`
  / `image.text_check_model` / `image.text_check_retries` config keys.
- Deliberately not building: an automated metrics loop (LinkedIn exposes no
  member-post impressions via any API scope; the only source is the manual
  export), a draft lint pass, or dropping images by default.
