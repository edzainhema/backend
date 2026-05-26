#!/usr/bin/env bash
#
# Flush buffered feed impressions from Redis into the DB (ACTIVITY_AND_FEED_AUDIT.md
# item C1). Runs every minute via cron. Same robustness shape as the other
# runners (path resolution, venv, flock); flock means a slow run can't overlap
# the next minute's trigger.
#
# The command is quiet when there's nothing to drain, so this only appends to
# the log when it actually wrote something.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$BACKEND_DIR/venv/bin/python" ]]; then
  PY="$BACKEND_DIR/venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

LOG_DIR="${CF_LOG_DIR:-$BACKEND_DIR/logs}"
LOG_FILE="$LOG_DIR/drain_impressions.log"
LOCK_FILE="${DRAIN_LOCK_FILE:-/tmp/drain_impressions.lock}"
mkdir -p "$LOG_DIR"

if command -v flock >/dev/null 2>&1; then
  flock -n "$LOCK_FILE" "$PY" "$BACKEND_DIR/manage.py" drain_impressions >> "$LOG_FILE" 2>&1 || true
else
  "$PY" "$BACKEND_DIR/manage.py" drain_impressions >> "$LOG_FILE" 2>&1 || true
fi
