#!/usr/bin/env bash
# Keep a persistent SSH tunnel open to the server's radar local API, so the
# browser extension (talking to http://127.0.0.1:$RADAR_PORT on this laptop)
# can reach server/bot/localapi.py, which only binds to the server's loopback.
# Unlike push_daily.sh this must never exit: it loops and reconnects, because
# an all-day browsing session will outlive plenty of individual SSH drops.
#
# Usage: radar_tunnel.sh [-c /path/to/push.conf]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$SCRIPT_DIR/push.conf"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c) CONF="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -f "$CONF" ]] || { echo "config not found: $CONF (copy push.conf.example)"; exit 1; }

REMOTE="" RADAR_PORT=""
first_line=1
while IFS= read -r line; do
  # strip a UTF-8 BOM if an editor (or PowerShell's Set-Content) added one
  if [[ $first_line -eq 1 ]]; then line="${line#$'\xef\xbb\xbf'}"; first_line=0; fi
  line="${line%%#*}"; line="$(echo "$line" | xargs || true)"
  [[ -z "$line" ]] && continue
  case "$line" in
    REMOTE=*) REMOTE="${line#REMOTE=}";;
    RADAR_PORT=*) RADAR_PORT="${line#RADAR_PORT=}";;
  esac
done < "$CONF"
[[ -n "$REMOTE" && -n "$RADAR_PORT" ]] || { echo "REMOTE / RADAR_PORT missing in $CONF"; exit 1; }

echo "radar tunnel: $RADAR_PORT -> $REMOTE:127.0.0.1:$RADAR_PORT (Ctrl+C to stop)"
while true; do
  ssh -N \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "$RADAR_PORT:127.0.0.1:$RADAR_PORT" \
    "$REMOTE"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) tunnel dropped, reconnecting in 5s..."
  sleep 5
done
