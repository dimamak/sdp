# Server Ops

Rules for debugging or changing anything on the box running dailypost
(`/opt/dailypost/app`, the `dailypost-bot*` services, `dailypost.db`).

## Restarting bot services without sudo

`systemctl restart dailypost-bot.service` / `dailypost-bot@Anton.service` fails
with "Interactive authentication required" for the codeagent user. Instead:

```
systemctl show <service> -p MainPID
kill -TERM <pid>          # codeagent owns the process, no root needed
```

`Restart=always` (RestartSec=10) in both unit files relaunches it automatically
— same effect as `systemctl restart`. Verify with
`systemctl show <service> -p ActiveState -p SubState -p MainPID` and
`journalctl -u <service>` (fresh MainPID, `active`/`running`, clean startup
lines, no traceback). Needed after deploying — the bot services are
long-running and only pick up file changes on restart; nightly cron jobs
don't need this (fresh process every run).

## Rule 1: after a restart/deploy, suspect environment before code

If a bug appears right after a restart/deploy and the logic hasn't changed,
suspect environment drift first (`.env`, `config.yaml`, instance overrides
under `instances/<name>/`) before editing code.

## Rule 2: never rotate secrets as a debugging guess

Don't rotate/replace secrets (`TG_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, LinkedIn/X tokens) as a debugging guess — confirm
the current value is actually wrong before touching it.

## Rule 3: verify against the real flow, not process state

Verify a fix against the real failing flow (rerun the nightly job for that
day, check `journalctl`/`nightly.log`) — "service is active" is not
verification.

## Rule 4: diagnosis-only when asked to "check logs"

If the user asks to "check the logs" / "investigate" without asking for a
fix, that's diagnosis-only: report findings and ask before changing anything.

## Shell mechanics

- No `sqlite3` CLI on this server — query `dailypost.db` via
  `.venv/bin/python -c "import sqlite3; ..."` instead.
