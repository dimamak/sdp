#!/usr/bin/env bash
# Cron entrypoint: nice + single-instance lock. Installed by the setup wizard.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The lock must be PER INSTANCE and owned by the user that runs the pipeline:
# a shared /tmp lock made two instances scheduled at the same minute collide
# (the second silently exited), and a lock left behind by another user is
# unopenable, which looked identical to "already running".
CONFIG_PATH="${DAILYPOST_CONFIG:-$APP_DIR/config.yaml}"
LOCK="$(dirname "$CONFIG_PATH")/.nightly.lock"

if ! exec 9>"$LOCK"; then
    echo "cannot open lock file $LOCK — check ownership/permissions" >&2
    exit 1
fi
if ! flock -n 9; then
    echo "another run for $CONFIG_PATH is still in progress; skipping" >&2
    exit 0
fi

cd "$APP_DIR"
exec nice -n 19 "$APP_DIR/.venv/bin/python" -m server.pipeline.run_nightly "$@"
