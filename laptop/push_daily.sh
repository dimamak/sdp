#!/usr/bin/env bash
# Push new work artifacts (Claude JSONL, screenshots, audio) to the server's
# ingest dir over ssh. Designed for Git Bash on Windows (no rsync needed):
# one tar stream per run, incremental via a stamp file.
#
# Usage: push_daily.sh [-c /path/to/push.conf] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$SCRIPT_DIR/push.conf"
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c) CONF="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -f "$CONF" ]] || { echo "config not found: $CONF (copy push.conf.example)"; exit 1; }

REMOTE="" REMOTE_DIR="" REMOTE_POST_CMD=""
declare -a NAMES DIRS GLOBS
first_line=1
while IFS= read -r line; do
  # strip a UTF-8 BOM if an editor (or PowerShell's Set-Content) added one
  if [[ $first_line -eq 1 ]]; then line="${line#$'\xef\xbb\xbf'}"; first_line=0; fi
  line="${line%%#*}"; line="$(echo "$line" | xargs || true)"
  [[ -z "$line" ]] && continue
  case "$line" in
    REMOTE=*) REMOTE="${line#REMOTE=}";;
    REMOTE_DIR=*) REMOTE_DIR="${line#REMOTE_DIR=}";;
    REMOTE_POST_CMD=*) REMOTE_POST_CMD="${line#REMOTE_POST_CMD=}";;
    PUSH_PATH=*)
      spec="${line#PUSH_PATH=}"
      IFS='|' read -r n d g <<< "$spec"
      NAMES+=("$n"); DIRS+=("$(eval echo "$d")"); GLOBS+=("${g:-*}");;
  esac
done < "$CONF"
[[ -n "$REMOTE" && -n "$REMOTE_DIR" ]] || { echo "REMOTE / REMOTE_DIR missing in $CONF"; exit 1; }

STAMP="$SCRIPT_DIR/.last_push"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

total=0
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; dir="${DIRS[$i]}"; glob="${GLOBS[$i]}"
  [[ -d "$dir" ]] || { echo "skip $name: $dir missing"; continue; }
  mkdir -p "$STAGE/$name"
  if [[ -f "$STAMP" ]]; then
    finder=(find "$dir" -type f -name "$glob" -newer "$STAMP")
  else
    finder=(find "$dir" -type f -name "$glob" -mtime -1)
  fi
  while IFS= read -r f; do
    rel="${f#"$dir"/}"
    mkdir -p "$STAGE/$name/$(dirname "$rel")"
    cp -p "$f" "$STAGE/$name/$rel"
    total=$((total+1))
  done < <("${finder[@]}" 2>/dev/null)
done

# Heartbeat: tells the server this laptop has checked in, so the nightly can wait
# for a machine that was asleep at 23:00 instead of drafting without its data.
# Written even when there is nothing new — "nothing to send" is still a check-in.
heartbeat() {
  local dir host
  dir="$(dirname "$REMOTE_DIR")"
  host="$(hostname | tr -cd '[:alnum:]._-')"
  ssh -o BatchMode=yes "$REMOTE" \
    "mkdir -p '$dir' && date -u +%Y-%m-%dT%H:%M:%SZ > '$dir/.heartbeat-$host'" 2>/dev/null || true
}

if [[ $total -eq 0 ]]; then
  echo "nothing new to push"
  [[ $DRY_RUN -eq 1 ]] || heartbeat
  touch "$STAMP"
  exit 0
fi

echo "pushing $total files to $REMOTE:$REMOTE_DIR"
if [[ $DRY_RUN -eq 1 ]]; then
  (cd "$STAGE" && find . -type f)
  exit 0
fi

remote_cmd="mkdir -p '$REMOTE_DIR' && tar xzf - -C '$REMOTE_DIR'"
[[ -n "$REMOTE_POST_CMD" ]] && remote_cmd="$remote_cmd && $REMOTE_POST_CMD"
tar czf - -C "$STAGE" . | ssh -o BatchMode=yes "$REMOTE" "$remote_cmd"
heartbeat
touch "$STAMP"
echo "done"
