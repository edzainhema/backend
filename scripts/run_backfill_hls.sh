#!/usr/bin/env bash
#
# Periodic self-heal for adaptive HLS: re-encodes any video post whose
# background HLS task failed or was lost, so every clip eventually gets its
# adaptive stream (see api/management/commands/backfill_hls.py).
#
# This wrapper is what the scheduler invokes — it keeps the cron / systemd
# entry to a single line and makes the run robust:
#   • resolves the backend directory from this script's own location
#   • prefers the project virtualenv's python, falling back to python3
#   • serializes runs with flock, so a slow drain can't overlap the next tick
#   • appends timestamped output to backend/logs/backfill_hls.log
#
# By default it ENQUEUES the encodes to the Celery worker (so it needs the
# worker + broker running). Pass --sync via BACKFILL_HLS_ARGS to encode inline.
#
# Run by hand any time:
#     bash backend/scripts/run_backfill_hls.sh
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Choose an interpreter: explicit $PYTHON, then the project venv, then python3.
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$BACKEND_DIR/venv/bin/python" ]]; then
  PY="$BACKEND_DIR/venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

LOG_DIR="${HLS_LOG_DIR:-$BACKEND_DIR/logs}"
LOG_FILE="$LOG_DIR/backfill_hls.log"
LOCK_FILE="${HLS_BACKFILL_LOCK_FILE:-/tmp/backfill_hls.lock}"
mkdir -p "$LOG_DIR"

# Extra args (e.g. "--limit 50" or "--sync"); empty by default.
ARGS="${BACKFILL_HLS_ARGS:-}"

echo "[$(date -Is)] starting backfill_hls (py=$PY, args=$ARGS)" >> "$LOG_FILE"

if command -v flock >/dev/null 2>&1; then
  if flock -n "$LOCK_FILE" "$PY" "$BACKEND_DIR/manage.py" backfill_hls $ARGS >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished backfill_hls" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] skipped (already running) or failed — see log above" >> "$LOG_FILE"
  fi
else
  if "$PY" "$BACKEND_DIR/manage.py" backfill_hls $ARGS >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished backfill_hls" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] failed — see log above" >> "$LOG_FILE"
  fi
fi
