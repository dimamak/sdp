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

## Phase 2 — Audio  ▢ planned
- Phone post-call debrief habit: automation rule ("call ended AND duration > 3 min
  → notification → tap opens recorder"), file reaches the server via a cloud-sync
  folder included in the laptop push list (or Syncthing phone→server)
- Laptop in-person recording (Buzz / hotkey WAV recorder) into a pushed folder
- Server transcription: faster-whisper, INT8 `large-v3-turbo`; for Hebrew use the
  ivrit.ai fine-tuned weights with `language='he'` (the fine-tune can't translate —
  transcribe Hebrew, let the draft LLM translate); configurable threads + nice level;
  optional Groq API fallback for English/overflow
- `transcribe.py` drains the audio queue before the digest step

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
