# Deployment

`/opt/dailypost/app` is **not a git repo** — code only ever lands there as a
file copy, either manually via `server/deploy.sh`, or automatically via
`.github/workflows/deploy.yml` on every push to `main` (GitHub Actions SSHes
in and runs `server/ci_deploy.sh`, a forced command bound to a deploy-only
key in `~/.ssh/authorized_keys`, restricted so it can only run that one
script). Both paths update `.last-push-commit` (a plain text file holding the
last deployed commit hash) — now that auto-deploy exists this should track
`origin/main` closely, but a manual `deploy.sh` run or a failed Action can
still drift it, so treat it as advisory, not proof of sync.

## Rule: server fixes get mirrored to git in the same task

Any fix applied directly on the server **must be committed back to this repo
in the same task** — the next push to `main` auto-deploys and silently
reverts it. This matters more now than when deploys were manual: a stray
`main` push from anyone can wipe out an unmirrored server fix within minutes.

## Rule: restart after copying files

Code changes don't apply to already-running processes. `ci_deploy.sh` already
restarts `dailypost-bot*` services after every auto-deploy; if you're copying
files by hand outside that path (e.g. testing a fix before committing it),
restart manually — see [server-ops.md](server-ops.md) for how (no sudo
available).

## Auto-deploy internals

- `ci_deploy.sh` waits (up to 5 min) for any instance's `.nightly.lock` to
  clear before extracting, so it doesn't swap code out from under a running
  nightly job.
- It only ever adds/overwrites files from the incoming tree — it never
  deletes, so `instances/`, `data/`, `logs/`, `.venv`, `config.yaml`, `.env`
  are untouched (none of them are tracked in git, so they're never part of
  the payload in the first place).
- The deploy job is gated to `github.repository == 'dimamak/dailypost'` so
  forks don't get a red X from missing secrets on every push.
- Setting this up for a new server: see
  [../self-hosting/ci-deploy.md](../self-hosting/ci-deploy.md).
