#!/usr/bin/env bash
# Cron entrypoint. Installed by the setup wizard. Single-instance locking is
# handled inside server/pipeline/run_nightly.py (server/pipeline/lock.py) so
# the same lock code covers both the server (cron) and laptop (in-process
# scheduler) paths — this script just nice-wraps the process.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
exec nice -n 19 "$APP_DIR/.venv/bin/python" -m server.pipeline.run_nightly "$@"
