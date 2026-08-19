#!/usr/bin/env bash
# Deploy the repo to a server over ssh (no git required on the server).
# Usage: server/deploy.sh <ssh-host> <remote-app-dir> [remote-user-to-own-files]
set -euo pipefail
HOST="${1:?usage: deploy.sh <ssh-host> <remote-app-dir> [owner]}"
DIR="${2:?usage: deploy.sh <ssh-host> <remote-app-dir> [owner]}"
OWNER="${3:-}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "deploying $SRC -> $HOST:$DIR"
tar czf - -C "$SRC" \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude data \
  --exclude logs --exclude config.yaml --exclude .env --exclude '*.session' \
  --exclude '*.local.md' --exclude push.conf --exclude .last_push \
  . | ssh "$HOST" "mkdir -p '$DIR' && tar xzf - -C '$DIR' && chmod +x '$DIR/server/run_nightly.sh'${OWNER:+ && chown -R $OWNER: '$DIR'}"
echo "done. On the server: cd $DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
echo "then: .venv/bin/python -m setup.wizard"
