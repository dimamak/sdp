# Deployment

`/opt/dailypost/app` is **not a git repo** — no CI, no auto-pull. Deployment is
a manual file copy, loosely tracked by `.last-push-commit` (a plain text file
holding the last deployed commit hash; nothing updates it automatically, so
treat it as advisory, not proof of sync — diff the actual files against the
repo at HEAD to be sure).

## Rule: server fixes get mirrored to git in the same task

Any fix applied directly on the server **must be committed back to this repo
in the same task** — otherwise the next manual deploy from this repo silently
reverts it.

## Rule: restart after copying files

Code changes don't apply to already-running processes — after copying
changed files, restart the affected service. See
[server-ops.md](server-ops.md) for how (no sudo available).
