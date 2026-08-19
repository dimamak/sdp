#!/usr/bin/env bash
# Forced-command target for CI-triggered deploys (see .github/workflows/deploy.yml).
# Bound to a dedicated deploy-only SSH key via authorized_keys `command=`, which
# supplies <app-dir> as $1 - this script never hardcodes a path, so any
# self-hoster can point their own authorized_keys entry at their own layout.
#
# Protocol: caller pipes `tar czf -` of the repo tree on stdin, with one added
# top-level file `.ci-deploy-commit` holding the commit SHA being deployed.
set -euo pipefail
APP_DIR="${1:?usage: ci_deploy.sh <app-dir>}"
mkdir -p "$APP_DIR"

# Nightly runs are fresh processes that read files as they go; swapping code
# out from under one mid-run can break it. Wait for any instance's lock
# (same file run_nightly.sh flocks) to clear before extracting.
for lock in "$APP_DIR"/.nightly.lock "$APP_DIR"/instances/*/.nightly.lock; do
    [ -e "$lock" ] || continue
    waited=0
    while ! ( exec 9>"$lock"; flock -n 9 ); do
        waited=$((waited + 10))
        if [ "$waited" -ge 300 ]; then
            echo "ci_deploy: $lock still held after 5m, aborting deploy" >&2
            exit 1
        fi
        sleep 10
    done
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar xzf - -C "$TMP"

COMMIT="$(cat "$TMP/.ci-deploy-commit" 2>/dev/null || echo unknown)"
rm -f "$TMP/.ci-deploy-commit"

cp -a "$TMP"/. "$APP_DIR"/
# Safety net for exec-bit drift (see commit 605c1fb) - tar/cp -a normally
# preserve mode, this just guards against future untracked regressions.
chmod +x "$APP_DIR/server/run_nightly.sh" "$APP_DIR/server/ci_deploy.sh" 2>/dev/null || true
echo "$COMMIT" > "$APP_DIR/.last-push-commit"

# Long-running services only pick up file changes on restart; nightly cron
# jobs are fresh processes each run and don't need this. Restart=always in
# the unit files relaunches automatically after the TERM.
for svc in $(systemctl list-units --all --plain --no-legend 'dailypost-bot*' 2>/dev/null | awk '{print $1}'); do
    pid="$(systemctl show "$svc" -p MainPID --value 2>/dev/null || true)"
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        kill -TERM "$pid" 2>/dev/null || true
        echo "ci_deploy: restarted $svc (was pid $pid)"
    fi
done

echo "ci_deploy: deployed $COMMIT to $APP_DIR"
