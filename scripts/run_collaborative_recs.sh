#!/usr/bin/env bash
#
# Nightly collaborative-filtering recompute for the "people like you" feed
# rail (ACTIVITY_AND_FEED_AUDIT.md item B3).
#
# This wrapper is what the scheduler actually invokes — it keeps the cron /
# systemd entry to a single line and makes the run robust:
#   • resolves the backend directory from this script's own location
#     (no hardcoded deploy paths)
#   • prefers the project virtualenv's python, falling back to python3
#   • serializes runs with flock, so a slow night can't overlap the next
#     trigger and double-compute
#   • appends timestamped output to backend/logs/collaborative_recs.log
#
# Run by hand any time (also the easiest way to seed the table the first
# time, rather than waiting for the first scheduled run):
#
#     bash backend/scripts/run_collaborative_recs.sh
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

LOG_DIR="${CF_LOG_DIR:-$BACKEND_DIR/logs}"
LOG_FILE="$LOG_DIR/collaborative_recs.log"
LOCK_FILE="${CF_LOCK_FILE:-/tmp/collaborative_recs.lock}"
mkdir -p "$LOG_DIR"

echo "[$(date -Is)] starting build_collaborative_recs (py=$PY)" >> "$LOG_FILE"

# flock -n fails fast if a previous run still holds the lock; the scheduler
# just tries again on its next tick. Hosts without flock fall back to a plain
# run. The manage.py command itself sets DJANGO_SETTINGS_MODULE, so no env
# setup is needed here.
if command -v flock >/dev/null 2>&1; then
  if flock -n "$LOCK_FILE" "$PY" "$BACKEND_DIR/manage.py" build_collaborative_recs >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_collaborative_recs" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] skipped (already running) or failed — see log above" >> "$LOG_FILE"
  fi
else
  if "$PY" "$BACKEND_DIR/manage.py" build_collaborative_recs >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_collaborative_recs" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] failed — see log above" >> "$LOG_FILE"
  fi
fi
