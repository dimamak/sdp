#!/usr/bin/env bash
# Cron entrypoint: nice + single-instance lock. Installed by the setup wizard.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${TMPDIR:-/tmp}/dailypost-nightly.lock"

exec 9>"$LOCK"
flock -n 9 || { echo "nightly already running"; exit 0; }

cd "$APP_DIR"
exec nice -n 19 "$APP_DIR/.venv/bin/python" -m server.pipeline.run_nightly "$@"
